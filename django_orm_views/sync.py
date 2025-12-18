from graphlib import TopologicalSorter, CycleError

from typing import Optional, List, Set

from django.db import connections, transaction

from .exceptions import CyclicDependencyError
from .constants import SUB_SCHEMA_NAME, LOG, DEFAULT_DATABASE_LABEL
from .register import registry, register_all_views
from .views import PostgresMaterialisedViewMixin


def get_app_name_from_view(view_class) -> str:
    """Extract the Django app name from a view class's module path.

    Examples:
        claimsagent_xcc.models.summaries -> claimsagent_xcc
        myapp.postgres_views -> myapp
    """
    module = view_class.__module__
    return module.split('.')[0]


def get_views_by_app(
    database: str = DEFAULT_DATABASE_LABEL, app_name: Optional[str] = None
) -> List:
    """Get views from the registry, optionally filtered by app name.

    Args:
        database: Database label to get views from
        app_name: Optional app name to filter by. If None, returns all views.

    Returns:
        List of view classes, filtered by app if specified.
    """
    views_in_db = registry.get(database, set())

    if app_name is None:
        return list(views_in_db)

    filtered_views = [
        view for view in views_in_db if get_app_name_from_view(view) == app_name
    ]

    return filtered_views


def get_specific_views_by_app(
    view_names: List[str],
    database: str = DEFAULT_DATABASE_LABEL,
    app_name: Optional[str] = None,
) -> List:
    """Get specific views by name, optionally filtered by app.

    Args:
        view_names: List of view names to retrieve
        database: Database label to get views from
        app_name: Optional app name to filter by

    Returns:
        List of view classes matching the criteria

    Raises:
        ValueError: If any view names are not found
    """
    views_in_db = registry.get(database, set())

    # Build a mapping of view name to view class
    view_name_map = {view.name: view for view in views_in_db}

    # If app_name is specified, filter the mapping
    if app_name is not None:
        view_name_map = {
            name: view
            for name, view in view_name_map.items()
            if get_app_name_from_view(view) == app_name
        }

    # Validate that all requested view names exist
    missing_views = set(view_names) - set(view_name_map.keys())
    if missing_views:
        raise ValueError(
            f"The following view names were not found in registry: {missing_views}"
        )

    # Get the view objects for the requested names
    views_to_sync = [view_name_map[name] for name in view_names]

    return views_to_sync


def topological_sort_views(list_of_views):
    """Implements a topological sort to build the views based on their dependencies.  This
    is because the SQL needs to be executed in the correct order.

    Returns an ordered list of views
    Raises CyclicDependencyError if there is a cyclic dependency between the views.
    """
    sorter = TopologicalSorter()

    # Add all views and their dependencies to the sorter
    for view in list_of_views:
        sorter.add(
            view,
            *[dep for dep in view.view_dependencies if dep in list_of_views],
        )

    try:
        # Get the topologically sorted order
        return list(sorter.static_order())
    except CycleError as e:
        raise CyclicDependencyError(f'A Cyclic dependency exists: {e}') from e


def sync_views(
    app_name: Optional[str] = None,
    view_names: Optional[List[str]] = None,
    database: str = DEFAULT_DATABASE_LABEL,
    grant_select_permissions_to_user: Optional[str] = None,
    recreate_schema: bool = False,
):
    """Sync views with optional filtering by app and/or specific view names.

    Args:
        app_name: Optional Django app name to filter views by
        view_names: Optional list of specific view names to sync (within the app if specified)
        database: Database label to sync views for (default: 'default')
        grant_select_permissions_to_user: User to grant select permissions to
        recreate_schema: If True, drop entire schema; if False, only drop selected views

    Behavior:
        - No args: sync all registered views
        - app_name only: sync all views in that app
        - app_name + view_names: sync specific views in that app
        - view_names only: sync specific views from any app
        All views are topologically sorted by dependencies before syncing.

    Raises:
        ValueError: If any view names are not found in the registry
    """
    logger = LOG.getChild('sync')

    logger.info(
        'Syncing views for database %s (app=%s, view_names=%s)',
        database,
        app_name,
        view_names,
    )

    register_all_views()

    # Determine which views to sync based on arguments
    if view_names:
        # Sync specific views (optionally filtered by app)
        views_to_generate = get_specific_views_by_app(
            view_names=view_names, database=database, app_name=app_name
        )
    else:
        # Sync all views (optionally filtered by app)
        views_to_generate = get_views_by_app(database=database, app_name=app_name)

    # Always topologically sort the views by dependencies
    views_to_generate = topological_sort_views(views_to_generate)

    with connections[database].cursor() as cursor:
        with transaction.atomic():
            if recreate_schema:
                # Drop the entire schema and recreate it
                logger.info('Dropping and recreating entire schema %s', SUB_SCHEMA_NAME)
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS {SUB_SCHEMA_NAME} CASCADE; CREATE SCHEMA {SUB_SCHEMA_NAME};'
                )
            else:
                # Drop only the selected views
                logger.info('Dropping %d specific views', len(views_to_generate))
                for view in views_to_generate:
                    # Check if it's a materialized view
                    if hasattr(view, 'get_refresh_sql'):
                        cursor.execute(
                            f'DROP MATERIALIZED VIEW IF EXISTS {view.name_with_schema} CASCADE;'
                        )
                    else:
                        cursor.execute(
                            f'DROP VIEW IF EXISTS {view.name_with_schema} CASCADE;'
                        )

            # Execute each SQL statement from the views
            for view in views_to_generate:
                logger.info("generating view %s", view.name)
                cursor.execute(view.creation_sql.sql, params=view.creation_sql.params)

            # Re-grant permissions.
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

    logger.info(
        'Successfully sync\'d %d views for %s database',
        len(views_to_generate),
        database,
    )


def refresh_materialized_view(
    view: PostgresMaterialisedViewMixin, concurrently: bool = False
):
    """Refresh the given materialized view."""
    with connections[view.database].cursor() as cursor:
        cursor.execute(view.get_refresh_sql(concurrently))


def refresh_materialized_views(
    app_name: Optional[str] = None,
    view_names: Optional[List[str]] = None,
    database: str = DEFAULT_DATABASE_LABEL,
    concurrent: bool = True,
):
    """Refresh materialized views with optional filtering by app and/or specific view names.

    Args:
        app_name: Optional Django app name to filter views by
        view_names: Optional list of specific view names to refresh (within the app if specified)
        database: Database label to refresh views for (default: 'default')
        concurrent: If True, refresh views concurrently (default: True)

    Behavior:
        - No args: refresh all registered materialized views
        - app_name only: refresh all materialized views in that app
        - app_name + view_names: refresh specific materialized views in that app
        - view_names only: refresh specific materialized views from any app
        All views are topologically sorted by dependencies before refreshing.

    Raises:
        ValueError: If any view names are not found in the registry
        ValueError: If trying to refresh concurrently a view without pk_fields
    """
    logger = LOG.getChild('refresh_materialized_views')

    logger.info(
        'Refreshing materialized views for database %s (app=%s, view_names=%s, concurrent=%s)',
        database,
        app_name,
        view_names,
        concurrent,
    )

    register_all_views()

    views_in_db = registry.get(database, set())

    # Filter to only materialized views (views that have get_refresh_sql method)
    materialized_views = [v for v in views_in_db if hasattr(v, 'get_refresh_sql')]

    # Determine which views to refresh based on arguments
    if view_names:
        # Refresh specific views (optionally filtered by app)
        views_to_refresh = []
        view_name_map = {view.name: view for view in materialized_views}

        # If app_name is specified, filter the mapping
        if app_name is not None:
            view_name_map = {
                name: view
                for name, view in view_name_map.items()
                if get_app_name_from_view(view) == app_name
            }

        # Validate that all requested view names exist
        missing_views = set(view_names) - set(view_name_map.keys())
        if missing_views:
            raise ValueError(
                f"The following materialized view names were not found in registry: {missing_views}"
            )

        views_to_refresh = [view_name_map[name] for name in view_names]
    else:
        # Refresh all views (optionally filtered by app)
        if app_name is not None:
            views_to_refresh = [
                v for v in materialized_views if get_app_name_from_view(v) == app_name
            ]
        else:
            views_to_refresh = materialized_views

    # Sort views topologically by dependencies
    views_to_refresh_sorted = topological_sort_views(views_to_refresh)

    logger.info(
        'Refreshing %d materialized views for database %s (concurrent=%s)',
        len(views_to_refresh_sorted),
        database,
        concurrent,
    )

    for view in views_to_refresh_sorted:
        try:
            logger.info(
                "refreshing materialized view %s (concurrent=%s)", view.name, concurrent
            )
            refresh_materialized_view(view, concurrently=concurrent)
        except ValueError as e:
            if concurrent and "pk_fields" in str(e):
                logger.warning(
                    "Cannot refresh view %s concurrently without pk_fields, falling back to non-concurrent refresh",
                    view.name,
                )
                refresh_materialized_view(view, concurrently=False)
            else:
                raise

    logger.info(
        'Successfully refreshed %d materialized views', len(views_to_refresh_sorted)
    )
