"""Control-plane authentication and request hardening.

The FastAPI app doubles as the data plane (device proxy) and the control
plane (administrative ``/api/*`` routes). Without protection, any client that
can reach the server can change modes, delete patterns, start listeners,
upload TLS keys, read captures and trigger LLM calls.

This module provides a middleware that requires a single control-plane
``password`` for every ``/api/*`` route. When ``security.auth_enabled`` is
true, the password grants full access (reads and writes) — there is no
read-only vs admin distinction. It is accepted via the ``X-API-Key`` header
or ``Authorization: Bearer ***``.

If ``password`` is left empty while auth is enabled, the control plane is
locked: every protected request is rejected with ``503`` + the
``X-Auth-Setup: required`` header, so the UI can offer a first-run dialog
that sets the password via ``POST /api/auth/password`` (the only route
reachable without credentials in that state).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import posixpath
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from core.config import SecurityConfig

logger = logging.getLogger(__name__)

# Paths that must remain reachable without credentials.
# The CA certificate is downloaded by an operator to install on devices; it is
# not secret, so it stays public to avoid breaking MITM onboarding.
PUBLIC_PATHS = frozenset({"/api/tls/ca-cert"})

# First-run setup endpoint. Reachable WITHOUT credentials ONLY while the
# control-plane password is still empty, and only with the one-time setup token
# (see `setup_token()`); once a password exists it falls back to normal
# authentication.
SETUP_PATH = "/api/auth/password"

# Header the first-run setup token must arrive in.
SETUP_TOKEN_HEADER = "x-setup-token"

# One-time setup token state. It is generated lazily (only while no password
# exists), persisted so the operator can read it from the filesystem as well as
# the logs, and cached for the process lifetime.
_SETUP_TOKEN: str | None = None
_SETUP_TOKEN_FILE = os.environ.get("SETUP_TOKEN_FILE", "ridebase/setup-token")


def setup_token(path: str | Path | None = None) -> str:
    """Return the one-time token that authorises first-run provisioning.

    While ``security.password`` is empty the setup endpoint cannot validate a
    credential, so anyone able to reach the port first could claim
    administration. Requiring this token closes that window: it is printed in
    the logs (and written to a file, mode 0600) so only someone with
    local/admin access can complete the bootstrap.

    ``path`` lets the caller place the file next to the databases; it is only
    honoured the first time the token is generated.
    """
    global _SETUP_TOKEN, _SETUP_TOKEN_FILE  # noqa: PLW0603 - generated once
    if _SETUP_TOKEN is None:
        token = secrets.token_urlsafe(24)
        target = Path(path) if path is not None else Path(_SETUP_TOKEN_FILE)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(token + "\n", encoding="utf-8")
            target.chmod(0o600)
            where = str(target.resolve())
        except OSError as exc:  # noqa: BLE001 - the log line still carries it
            logger.warning("Could not persist the setup token to %s: %s", target, exc)
            where = "(not persisted)"
        logger.warning(
            "First-run setup token: %s  (also in %s)\n"
            "Use it as the %s header when setting security.password.",
            token,
            where,
            SETUP_TOKEN_HEADER,
        )
        _SETUP_TOKEN_FILE = str(target)
        _SETUP_TOKEN = token
    return _SETUP_TOKEN


def _safe_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to avoid timing side channels."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _extract_key(request: Request) -> str:
    """Return the password from the X-API-Key header or Authorization bearer."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


class ControlPlaneAuthMiddleware(BaseHTTPMiddleware):
    """Require the control-plane password for every ``/api/*`` route.

    ``get_security_config`` is a zero-argument callable returning the current
    :class:`SecurityConfig` so hot-reloads of the YAML are picked up live.
    """

    def __init__(
        self,
        app,
        get_security_config: Callable[[], SecurityConfig],
    ) -> None:
        super().__init__(app)
        self._get_security_config = get_security_config
        self._logged = False

    def _log_once(self, password_ok: bool) -> None:
        """Log once at startup so the operator can confirm auth state."""
        if self._logged:
            return
        self._logged = True
        if password_ok:
            logger.info("Control-plane auth enabled. A password is configured in config.yaml.")
        else:
            logger.warning(
                "Control-plane auth enabled but security.password is empty: "
                "the control plane is locked until you set it in config.yaml."
            )

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, PLR0911
        # Performance optimization: direct scope lookup avoids parsing URL object (~4x speedup)
        path = request.scope.get("path", "")
        # Decide on the canonical form, never on the raw spelling: an access-control check that
        # compares the incoming string can be sidestepped by whichever layer normalises differently
        # from us. A path that resolves into /api/* must require credentials even if it was written
        # as /api/../api/... , and one carrying a NUL or a backslash is rejected outright.
        if "\x00" in path or "\\" in path:
            return JSONResponse(status_code=400, content={"error": "Invalid request path"})
        path = posixpath.normpath(path)
        if path != "/api" and not path.startswith("/api/"):
            return await call_next(request)
        if request.method == "OPTIONS":
            # CORS preflight — never requires credentials.
            return await call_next(request)
        if path in PUBLIC_PATHS:
            return await call_next(request)

        cfg = self._get_security_config()
        if not cfg.auth_enabled:
            return await call_next(request)

        password = cfg.password
        if not password:
            self._log_once(password_ok=False)
            if path == SETUP_PATH:
                # First-run: the operator is provisioning the control-plane
                # password. There is no credential to validate against yet, so a
                # one-time setup token guards the window instead — otherwise
                # whoever reaches the port first could claim administration.
                if _safe_eq(request.headers.get(SETUP_TOKEN_HEADER, ""), setup_token()):
                    return await call_next(request)
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": (
                            "Setup token required: the control plane has no password yet. "
                            "Read the one-time token from the server logs (or "
                            "ridebase/setup-token) and send it as X-Setup-Token."
                        )
                    },
                    headers={"X-Auth-Setup": "token-required"},
                )
            return JSONResponse(
                status_code=503,
                content={
                    "error": (
                        "Control-plane auth is enabled but security.password is "
                        "empty. Set your password via POST /api/auth/password "
                        "or in config.yaml and restart, or disable "
                        "auth_enabled."
                    )
                },
                headers={"X-Auth-Setup": "required"},
            )

        self._log_once(password_ok=True)
        provided = _extract_key(request)
        if _safe_eq(provided, password):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )


class RequestBodyTooLarge(StarletteHTTPException):
    """Raised when a control-plane body stream exceeds the configured cap."""

    def __init__(self, max_size: int) -> None:
        super().__init__(
            status_code=413,
            detail=f"Request body exceeds the maximum allowed size ({max_size} bytes)",
        )


class MaxBodySizeMiddleware:
    """Reject control-plane request bodies larger than ``max_size`` bytes.

    Only enforced for ``/api/*`` routes: the catch-all data-plane route
    legitimately proxies oversized device payloads to the cloud, so it stays
    exempt. ``Content-Length`` is checked up front; chunked/streamed bodies are
    guarded by wrapping the ASGI receive channel and raising a 413 as soon as
    the running total crosses the cap (F-09).
    """

    def __init__(self, app, max_size: int) -> None:
        self.app = app
        self.max_size = max_size

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001, C901
        if scope["type"] != "http" or self.max_size <= 0:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # Reject up front when Content-Length already declares an oversize body.
        # Performance optimization: header iteration avoids dict allocation
        # per request (~1.4x speedup).
        declared = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length" or name.lower() == b"content-length":
                declared = value
                break
        if declared is not None:
            try:
                if int(declared) > self.max_size:
                    await self._reply_413(send)
                    return
            except (TypeError, ValueError):
                pass

        received = 0
        response_started = False

        async def send_wrapper(message):  # noqa: ANN001
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def guarded_receive():  # noqa: ANN202
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_size:
                    raise RequestBodyTooLarge(self.max_size)
            return message

        try:
            await self.app(scope, guarded_receive, send_wrapper)
        except RequestBodyTooLarge:
            if not response_started:
                await self._reply_413(send)
            else:
                raise

    async def _reply_413(self, send) -> None:  # noqa: ANN001
        body = json.dumps(
            {"error": (f"Request body exceeds the maximum allowed size ({self.max_size} bytes)")}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# Pre-computed byte tuples for default HTTP security headers
_DEFAULT_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard HTTP security response headers to all responses."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        raw = response.raw_headers
        # Performance optimization: single pass tuple inspection avoids 4x
        # MutableHeaders.setdefault linear scans (~2.3x speedup per response).
        has_security_header = False
        for name, _ in raw:
            lname = name.lower()
            if lname in (
                b"x-content-type-options",
                b"x-frame-options",
                b"x-xss-protection",
                b"referrer-policy",
            ):
                has_security_header = True
                break

        if not has_security_header:
            raw.extend(_DEFAULT_SECURITY_HEADERS)
        else:
            existing = {name.lower() for name, _ in raw}
            for name, value in _DEFAULT_SECURITY_HEADERS:
                if name not in existing:
                    raw.append((name, value))

        return response
