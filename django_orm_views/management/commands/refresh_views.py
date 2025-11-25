from django.core.management import BaseCommand

from ...constants import LOG
from ...sync import refresh_materialized_views


class Command(BaseCommand):
    help = 'Refreshes materialized postgres views using the django_orm_views framework'

    def add_arguments(self, parser):
        parser.add_argument(
            'view_names',
            nargs='*',
            help='Specific materialized view names to refresh. If not provided, refreshes all materialized views.',
        )
        parser.add_argument(
            '--no-concurrent',
            action='store_true',
            dest='no_concurrent',
            default=False,
            help='Disable concurrent refresh of materialized views (default: concurrent is enabled)',
        )

    def handle(self, *_, **options):
        view_names = options.get('view_names')
        no_concurrent = options.get('no_concurrent')
        concurrent = not no_concurrent

        logger = LOG.getChild('refresh_views')

        try:
            refresh_materialized_views(
                view_names=view_names if view_names else None,
                concurrent=concurrent,
            )
            if view_names:
                msg = f'Successfully refreshed {len(view_names)} materialized view(s): {", ".join(view_names)} (concurrent={concurrent})'
            else:
                msg = f'Successfully refreshed all materialized views (concurrent={concurrent})'
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except ValueError as e:
            error_msg = f'Error: {str(e)}'
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(error_msg))
