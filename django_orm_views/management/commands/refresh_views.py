from django.core.management import BaseCommand

from ...constants import LOG
from ...sync import refresh_materialized_views


class Command(BaseCommand):
    help = 'Refreshes materialized postgres views using the django_orm_views framework'

    def add_arguments(self, parser):
        parser.add_argument(
            'positional_args',
            nargs='*',
            help=(
                'Optional positional arguments. '
                'If none provided, refreshes all materialized views. '
                'If 1 provided (app name), refreshes all materialized views in that app. '
                'If 2+ provided (app name + view names), refreshes specific materialized views in that app.'
            ),
        )
        parser.add_argument(
            '--no-concurrent',
            action='store_true',
            dest='no_concurrent',
            default=False,
            help='Disable concurrent refresh of materialized views (default: concurrent is enabled)',
        )

    def handle(self, *_, **options):
        positional_args = options.get('positional_args', [])
        no_concurrent = options.get('no_concurrent')
        concurrent = not no_concurrent

        logger = LOG.getChild('refresh_views')

        try:
            # Parse positional arguments
            app_name = None
            view_names = None

            if len(positional_args) == 1:
                # Single argument: app name
                app_name = positional_args[0]
                msg = f'Refreshing all materialized views in app: {app_name}'
            elif len(positional_args) > 1:
                # Multiple arguments: app name + view names
                app_name = positional_args[0]
                view_names = positional_args[1:]
                msg = f'Refreshing materialized views {view_names} in app {app_name}'
            else:
                # No arguments: refresh all materialized views
                msg = 'Refreshing all materialized views'

            logger.info(msg)
            self.stdout.write(msg)

            refresh_materialized_views(
                app_name=app_name,
                view_names=view_names,
                concurrent=concurrent,
            )
            msg = f'Successfully refreshed materialized views (concurrent={concurrent})'
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except ValueError as e:
            error_msg = f'Error: {str(e)}'
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(error_msg))
