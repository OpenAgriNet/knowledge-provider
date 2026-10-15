"""Flatten a FastAPI app's route table for tests that audit every endpoint.

FastAPI 0.140+ keeps an included router as one lazy wrapper object inside
``app.routes`` (it holds the real routes under ``original_router``). A plain
``for route in app.routes`` therefore never sees router-mounted endpoints, and
an audit written that way silently skips them. Walk through this module
instead. It refuses route types it does not know, so a FastAPI change cannot
make endpoints vanish from an audit without a test failing.
"""

from fastapi.routing import APIRoute
from starlette.routing import Route

IGNORED_METHODS = {"HEAD", "OPTIONS"}


def iter_api_routes(router, prefix: str = ""):
    """Yield ``(path, route)`` for every ``APIRoute``, descending into included routers."""
    for route in router.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from iter_api_routes(inner, prefix + (getattr(context, "prefix", "") or ""))
        elif isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif type(route) is Route:
            continue  # framework-owned docs/schema routes (/docs, /openapi.json, ...)
        else:
            raise TypeError(
                f"route_inventory does not know how to walk {type(route).__name__}; "
                "teach it, or the auth audit and route snapshot will silently skip routes"
            )


def application_routes(app):
    """Yield ``(METHOD, path, route)`` for every application endpoint."""
    for path, route in iter_api_routes(app):
        for method in sorted(route.methods - IGNORED_METHODS):
            yield method, path, route
