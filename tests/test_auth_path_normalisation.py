"""The control-plane check must not depend on how a path is spelled.

Regression guard for an access-control decision taken on the raw ASGI path: a request written as
``/health/../api/devices`` resolved, through the router, to a protected route while the middleware
compared the raw string and let it through. The decision now happens on the canonical form.

Verified against the real middleware, not a copy of its logic.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from core.config import SecurityConfig
from core.security import ControlPlaneAuthMiddleware

SECRET = "correct-horse-battery"  # noqa: S105 - a test fixture, not a credential
OK = 200
BAD_REQUEST = 400
UNAUTHORIZED = 401
NOT_FOUND = 404


async def _protected(request):  # noqa: ANN001, ARG001
    return PlainTextResponse("control plane")


async def _public(request):  # noqa: ANN001, ARG001
    return PlainTextResponse("public")


def _client(password: str) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/devices", _protected),
            Route("/health", _public),
        ]
    )
    cfg = SecurityConfig(auth_enabled=True, password=password)
    app.add_middleware(ControlPlaneAuthMiddleware, get_security_config=lambda: cfg)
    return TestClient(app)


class TestPathIsCanonicalisedBeforeTheCheck:
    def test_a_normalised_path_still_needs_credentials(self):
        """The core regression: spelling a protected route differently must not skip the check."""
        client = _client(SECRET)

        response = client.get("/health/../api/devices")

        assert response.status_code in (UNAUTHORIZED, NOT_FOUND), (
            "un percorso che risolve in /api/* non deve passare senza credenziali"
        )

    def test_the_dotted_form_without_credentials_is_rejected(self):
        client = _client(SECRET)

        assert client.get("/api/../api/devices").status_code in (UNAUTHORIZED, NOT_FOUND)

    def test_valid_credentials_still_work(self):
        """The normal path must keep working: this is a hardening change, not a lockout."""
        client = _client(SECRET)

        response = client.get("/api/devices", headers={"X-API-Key": SECRET})

        assert response.status_code == OK
        assert response.text == "control plane"

    def test_a_backslash_path_is_rejected(self):
        client = _client(SECRET)

        assert client.get("/api\\devices").status_code == BAD_REQUEST

    def test_public_routes_are_untouched(self):
        client = _client(SECRET)

        assert client.get("/health").status_code == OK
        assert client.get("/health").text == "public"

    def test_bare_api_prefix_is_protected_too(self):
        """`/api` alone is not a route today, but it must not be a hole either."""
        client = _client(SECRET)

        response = client.get("/api")

        assert response.status_code in (UNAUTHORIZED, NOT_FOUND)
