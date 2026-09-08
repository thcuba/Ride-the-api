"""Control-plane authentication and request hardening.

The FastAPI app doubles as the data plane (device proxy) and the control
plane (administrative ``/api/*`` routes). Without protection, any client that
can reach the server can change modes, delete patterns, start listeners,
upload TLS keys, read captures and trigger LLM calls.

This module provides a middleware that requires an API key for every
``/api/*`` route. Read-only methods (GET/HEAD) accept either the read-only key
or the admin key; every other method requires the admin key. Keys are accepted
via the ``X-API-Key`` header or ``Authorization: Bearer <key>``.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import TYPE_CHECKING, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from core.config import SecurityConfig

logger = logging.getLogger(__name__)

# Paths that must remain reachable without credentials (device bootstrap).
# The CA certificate is downloaded by an operator to install on devices; it is
# not secret, so it stays public to avoid breaking MITM onboarding.
PUBLIC_PATHS = frozenset({"/api/tls/ca-cert"})

_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _safe_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to avoid timing side channels."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _extract_key(request: Request) -> str:
    """Return the API key from the X-API-Key header or Authorization bearer."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


class ControlPlaneAuthMiddleware(BaseHTTPMiddleware):
    """Require an API key for every ``/api/*`` route.

    ``get_security_config`` is a zero-argument callable returning the current
    :class:`SecurityConfig` so hot-reloads of the YAML are picked up live.
    """

    def __init__(self, app, get_security_config: Callable[[], "SecurityConfig"]) -> None:
        super().__init__(app)
        self._get_security_config = get_security_config
        self._generated_admin_key: str | None = None
        self._generated_readonly_key: str | None = None
        self._logged = False

    def _effective_keys(self, cfg: "SecurityConfig") -> tuple[str, str]:
        """Return (admin_key, readonly_key), generating random ones if unset."""
        admin = cfg.admin_api_key or self._generated_admin_key
        if not admin:
            self._generated_admin_key = secrets.token_urlsafe(32)
            admin = self._generated_admin_key
        readonly = cfg.readonly_api_key or self._generated_readonly_key
        if not readonly:
            self._generated_readonly_key = secrets.token_urlsafe(32)
            readonly = self._generated_readonly_key
        if not self._logged:
            self._logged = True
            logger.warning(
                "Control-plane auth enabled. No API keys configured in config.yaml; "
                "generated ephemeral keys for this run. "
                "ADMIN key: %s  READ-ONLY key: %s  "
                "(set security.admin_api_key / security.readonly_api_key to make them stable)",
                admin,
                readonly,
            )
        return admin, readonly

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if request.method == "OPTIONS":
            # CORS preflight — never requires credentials.
            return await call_next(request)
        if path in PUBLIC_PATHS:
            return await call_next(request)

        cfg = self._get_security_config()
        if not cfg.auth_enabled:
            return await call_next(request)

        admin_key, readonly_key = self._effective_keys(cfg)
        provided = _extract_key(request)

        if request.method in _READ_ONLY_METHODS:
            if (readonly_key and _safe_eq(provided, readonly_key)) or (
                admin_key and _safe_eq(provided, admin_key)
            ):
                return await call_next(request)
        elif admin_key and _safe_eq(provided, admin_key):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
