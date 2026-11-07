"""Route introspection helpers.

FastAPI 0.140 includes routers **lazily**: ``app.include_router(router)`` stores an
``_IncludedRouter`` wrapper in ``app.routes`` instead of copying the routes, and that
wrapper has no ``.path``. Anything that inspects ``app.routes`` to find out which
paths the app serves (the dashboard/api coverage tests, smoke checks, registration
order checks) therefore sees only the routes declared directly on the app and silently
loses every modular route.

``iter_routes``/``route_paths`` unwrap those wrappers so callers see the real set of
routes, and reaching for them is preferable to poking at ``app.routes`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from starlette.routing import BaseRoute


def iter_routes(app: FastAPI) -> Iterator[BaseRoute]:
    """Yield every route the app actually serves, unwrapping included routers."""
    for route in app.routes:
        if getattr(route, "path", None) is not None:
            yield route
            continue
        # FastAPI >= 0.140: the real APIRouter is kept on the wrapper.
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from getattr(original_router, "routes", [])


def route_paths(app: FastAPI) -> list[str]:
    """Return the paths the app serves, in registration order."""
    return [route.path for route in iter_routes(app)]
