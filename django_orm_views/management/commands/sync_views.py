from django.core.management import BaseCommand

from ...constants import LOG
from ...sync import sync_views, sync_specific_views, refresh_materialized_views


class Command(BaseCommand):
    help = 'Syncs postgres views using the django_orm_views framework'

    def add_arguments(self, parser):
        parser.add_argument(
            'view_names',
            nargs='*',
            help='Specific view names to sync. If not provided, syncs all views.',
        )
        parser.add_argument(
            '--grant-select-permissions-to-user',
            action='store',
            dest='grant_select_to_user',
            help='Grant SELECT permissions on synced views to the specified user',
        )
        parser.add_argument(
            '--recreate',
            action='store_true',
            dest='recreate_schema',
            default=False,
            help='Drop and recreate entire views schema (only applies when syncing specific views)',
        )
        parser.add_argument(
            '--refresh',
            action='store_true',
            dest='refresh_views',
            default=False,
            help='Refresh materialized views after syncing',
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
        grant_select_to_user = options.get('grant_select_to_user')
        recreate_schema = options.get('recreate_schema')
        refresh_views = options.get('refresh_views')
        no_concurrent = options.get('no_concurrent')
        concurrent = not no_concurrent

        logger = LOG.getChild('sync_views')

        try:
            # Sync views
            if view_names:
                # Sync specific views
                sync_specific_views(
                    view_names=view_names,
                    grant_select_permissions_to_user=grant_select_to_user,
                    recreate_schema=recreate_schema,
                )
                msg = f'Successfully sync\'d {len(view_names)} view(s): {", ".join(view_names)}'
            else:
                # Sync all views
                sync_views(
                    grant_select_permissions_to_user=grant_select_to_user
                )
                msg = 'Successfully sync\'d all views using django_orm_views'

            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

            # Refresh materialized views if requested
            if refresh_views:
                refresh_materialized_views(
                    view_names=view_names if view_names else None,
                    concurrent=concurrent,
                )
                refresh_msg = f'Successfully refreshed materialized view(s) (concurrent={concurrent})'
                logger.info(refresh_msg)
                self.stdout.write(self.style.SUCCESS(refresh_msg))

        except ValueError as e:
            error_msg = f'Error: {str(e)}'
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(error_msg))
