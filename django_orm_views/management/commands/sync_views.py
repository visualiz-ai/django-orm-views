from django.core.management import BaseCommand

from ...constants import LOG
from ...sync import sync_views


class Command(BaseCommand):
    help = 'Syncs postgres views using the django_orm_views framework'

    def add_arguments(self, parser):
        parser.add_argument(
            'positional_args',
            nargs='*',
            help=(
                'Optional positional arguments. '
                'If none provided, syncs all views. '
                'If 1 provided (app name), syncs all views in that app. '
                'If 2+ provided (app name + view names), syncs specific views in that app.'
            ),
        )
        parser.add_argument(
            '--grant-select-permissions-to-user',
            action='store',
            dest='grant_select_to_user',
            help='Grant SELECT permissions on synced views to the specified user',
        )
        parser.add_argument(
            '--no-recreate',
            action='store_true',
            dest='no_recreate',
            default=False,
            help='Only drop specific views instead of recreating entire schema',
        )

    def handle(self, *_, **options):
        positional_args = options.get('positional_args', [])
        grant_select_to_user = options.get('grant_select_to_user')
        no_recreate = options.get('no_recreate', False)
        recreate_schema = not no_recreate

        logger = LOG.getChild('sync_views')

        try:
            # Parse positional arguments
            app_name = None
            view_names = None

            if len(positional_args) == 1:
                # Single argument: app name
                app_name = positional_args[0]
                msg = f'Syncing all views in app: {app_name}'
            elif len(positional_args) > 1:
                # Multiple arguments: app name + view names
                app_name = positional_args[0]
                view_names = positional_args[1:]
                msg = f'Syncing views {view_names} in app {app_name}'
            else:
                # No arguments: sync all views
                msg = 'Syncing all views'

            logger.info(msg)
            self.stdout.write(msg)

            # Sync views
            sync_views(
                app_name=app_name,
                view_names=view_names,
                grant_select_permissions_to_user=grant_select_to_user,
                recreate_schema=recreate_schema,
            )
            msg = f'Successfully sync\'d views using django_orm_views'

            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except ValueError as e:
            error_msg = f'Error: {str(e)}'
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(error_msg))
