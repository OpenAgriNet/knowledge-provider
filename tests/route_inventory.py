"""Flatten a FastAPI app's route table for tests that audit every endpoint.

FastAPI 0.140+ keeps an included router as one lazy wrapper object inside
``app.routes`` (it holds the real routes under ``original_router``). A plain
``for route in app.routes`` therefore never sees router-mounted endpoints, and
an audit written that way silently skips them. Walk through this module
instead.
"""

from fastapi.routing import APIRoute

# Framework-provided routes (docs / schema) — owned by FastAPI, not by us.
FRAMEWORK_PATHS = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}

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


def application_routes(app):
    """Yield ``(METHOD, path, route)`` for every application endpoint, framework docs excluded."""
    for path, route in iter_api_routes(app):
        if path in FRAMEWORK_PATHS:
            continue
        for method in sorted(route.methods - IGNORED_METHODS):
            yield method, path, route
