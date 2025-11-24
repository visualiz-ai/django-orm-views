from graphlib import TopologicalSorter, CycleError

from typing import Optional, List

from django.db import connections, transaction

from .exceptions import CyclicDependencyError
from .constants import SUB_SCHEMA_NAME, LOG, DEFAULT_DATABASE_LABEL
from .register import registry, register_all_views
from .views import PostgresMaterialisedViewMixin


def topological_sort_views(list_of_views):
    """Implements a topological sort to build the views based on their dependencies.  This
    is because the SQL needs to be executed in the correct order.

    Returns an ordered list of views
    Raises CyclicDependencyError if there is a cyclic dependency between the views.
    """
    sorter = TopologicalSorter()

    # Add all views and their dependencies to the sorter
    for view in list_of_views:
        sorter.add(view, *view.view_dependencies)

    try:
        # Get the topologically sorted order
        return list(sorter.static_order())
    except CycleError as e:
        raise CyclicDependencyError(f'A Cyclic dependency exists: {e}') from e


def sync_views(
        grant_select_permissions_to_user: Optional[str] = None
):
    """This function syncs all the views in the registry.

    This effectively destroys + recreates all views within a transaction. Views live under a separate schema
    so that we can tear them down/recreate them simply.

    Implements topological sorting in order to analyse interdependencies and execute the SQL in the correct order.

    Note, it assumes that the registry has been built (i.e. depending on the AppConfig of this app calling ready).
    """
    logger = LOG.getChild('sync')

    logger.info('Syncing view registry for databases %s', list(registry.keys()))

    register_all_views()

    for database, views in registry.items():
        views_to_generate = topological_sort_views(views)
        with connections[database].cursor() as cursor:
            with transaction.atomic():
                # Drop the view schema and recreate it
                cursor.execute(f'DROP SCHEMA IF EXISTS {SUB_SCHEMA_NAME} CASCADE; CREATE SCHEMA {SUB_SCHEMA_NAME};')

                # Execute each SQL statement from the views
                for view in views_to_generate:
                    LOG.info("generating view %s", view.name)
                    cursor.execute(view.creation_sql.sql, params=view.creation_sql.params)

                # Re-grant permissions.
                if grant_select_permissions_to_user is not None:
                    cursor.execute(
                        f'GRANT USAGE ON SCHEMA {SUB_SCHEMA_NAME} TO {grant_select_permissions_to_user};'
                    )
                for view in views_to_generate:
                    if view.hidden or grant_select_permissions_to_user is None:
                        continue
                    cursor.execute(
                        f'GRANT SELECT ON {SUB_SCHEMA_NAME}.{view.name} TO {grant_select_permissions_to_user};'
                    )
        LOG.info('Successfully sync\'d %s views for %s database', len(views_to_generate), database)

    LOG.info('Successfully sync\'d %s views', len(registry))


def refresh_materialized_view(
    view: PostgresMaterialisedViewMixin, concurrently: bool = False
):
    """Refresh the given materialized view."""
    with connections[view.database].cursor() as cursor:
        cursor.execute(view.get_refresh_sql(concurrently))


def sync_specific_views(
        view_names: List[str],
        database: str = DEFAULT_DATABASE_LABEL,
        grant_select_permissions_to_user: Optional[str] = None,
        recreate_schema: bool = False,
):
    """Sync specific views by name.

    Args:
        view_names: List of view names to sync
        database: Database label to sync views for (default: 'default')
        grant_select_permissions_to_user: User to grant select permissions to
        recreate_schema: If True, drop entire schema; if False, only drop selected views

    Raises:
        ValueError: If any view names are not found in the registry
    """
    logger = LOG.getChild('sync_specific_views')

    register_all_views()

    views_in_db = registry.get(database, set())

    # Build a mapping of view name to view class
    view_name_map = {view.name: view for view in views_in_db}

    # Validate that all requested view names exist
    missing_views = set(view_names) - set(view_name_map.keys())
    if missing_views:
        raise ValueError(
            f"The following view names were not found in registry: {missing_views}"
        )

    # Get the view objects for the requested names
    views_to_sync = [view_name_map[name] for name in view_names]

    # Use the views as-is; assume dependencies already exist
    views_to_generate = views_to_sync

    logger.info(
        'Syncing %d specific views for database %s',
        len(views_to_generate),
        database
    )

    with connections[database].cursor() as cursor:
        with transaction.atomic():
            if recreate_schema:
                # Drop entire schema and recreate
                logger.info('Dropping entire schema %s', SUB_SCHEMA_NAME)
                cursor.execute(f'DROP SCHEMA IF EXISTS {SUB_SCHEMA_NAME} CASCADE; CREATE SCHEMA {SUB_SCHEMA_NAME};')
            else:
                # Drop only the selected views
                logger.info('Dropping %d specific views', len(views_to_generate))
                for view in views_to_generate:
                    # Check if it's a materialized view
                    if hasattr(view, 'get_refresh_sql'):
                        cursor.execute(f'DROP MATERIALIZED VIEW IF EXISTS {view.name_with_schema} CASCADE;')
                    else:
                        cursor.execute(f'DROP VIEW IF EXISTS {view.name_with_schema} CASCADE;')

            # Create/recreate the views
            for view in views_to_generate:
                logger.info("generating view %s", view.name)
                cursor.execute(view.creation_sql.sql, params=view.creation_sql.params)

            # Re-grant permissions if needed
            if grant_select_permissions_to_user is not None:
                cursor.execute(
                    f'GRANT USAGE ON SCHEMA {SUB_SCHEMA_NAME} TO {grant_select_permissions_to_user};'
                )
                for view in views_to_generate:
                    if view.hidden:
                        continue
                    cursor.execute(
                        f'GRANT SELECT ON {SUB_SCHEMA_NAME}.{view.name} TO {grant_select_permissions_to_user};'
                    )

    logger.info('Successfully sync\'d %d views for %s database', len(views_to_generate), database)


def refresh_materialized_views(
        view_names: Optional[List[str]] = None,
        database: str = DEFAULT_DATABASE_LABEL,
        concurrent: bool = True,
):
    """Refresh materialized views.

    Args:
        view_names: List of specific view names to refresh. If None, refresh all materialized views.
        database: Database label to refresh views for (default: 'default')
        concurrent: If True, refresh views concurrently (default: True)

    Raises:
        ValueError: If any view names are not found in the registry
        ValueError: If trying to refresh concurrently a view without pk_fields
    """
    logger = LOG.getChild('refresh_materialized_views')

    register_all_views()

    views_in_db = registry.get(database, set())

    # Filter to only materialized views (views that have get_refresh_sql method)
    materialized_views = [
        v for v in views_in_db
        if hasattr(v, 'get_refresh_sql')
    ]

    if view_names is None:
        views_to_refresh = materialized_views
    else:
        # Build a mapping of view name to view class
        view_name_map = {view.name: view for view in materialized_views}

        # Validate that all requested view names exist
        missing_views = set(view_names) - set(view_name_map.keys())
        if missing_views:
            raise ValueError(
                f"The following materialized view names were not found in registry: {missing_views}"
            )

        views_to_refresh = [view_name_map[name] for name in view_names]

    logger.info(
        'Refreshing %d materialized views for database %s (concurrent=%s)',
        len(views_to_refresh),
        database,
        concurrent
    )

    for view in views_to_refresh:
        try:
            logger.info("refreshing materialized view %s (concurrent=%s)", view.name, concurrent)
            refresh_materialized_view(view, concurrently=concurrent)
        except ValueError as e:
            if concurrent and "pk_fields" in str(e):
                logger.warning(
                    "Cannot refresh view %s concurrently without pk_fields, falling back to non-concurrent refresh",
                    view.name
                )
                refresh_materialized_view(view, concurrently=False)
            else:
                raise

    logger.info('Successfully refreshed %d materialized views', len(views_to_refresh))
