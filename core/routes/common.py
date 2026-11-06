"""Shared utilities and helpers for route modules."""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _client_error(exc: BaseException, message: str, status_code: int = 400) -> JSONResponse:
    """Return a sanitized JSONResponse for client errors (400, 422, etc.).

    Internal exception details are stripped in production to prevent information disclosure.
    The one real implementation lives in core.server (it also emits the
    "Rejected request" log line the tests assert on); it is imported lazily because
    core.server imports the routers, so a module-level import would be circular.
    """
    from core import server

    return server._client_error(exc, message, status_code)
