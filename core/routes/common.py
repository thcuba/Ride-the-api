"""Shared utilities and helpers for route modules."""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _client_error(exc: BaseException, message: str, status_code: int = 400) -> JSONResponse:
    """Return a control-plane error response that never leaks internals.

    Raw exception text can carry filesystem paths, library internals or configuration
    details (CodeQL ``py/stack-trace-exposure``). The client gets a stable, actionable
    message; the detail is logged server-side, where it is actually useful for debugging.

    This is the single implementation. core.server re-exports it, so the proxy and every
    router share one behaviour and one log line, and nobody needs a lazy import to reach
    it (the old delegation did a `from core import server` on every call).
    """
    logger.info("Rejected request (%s): %s", message, exc)
    return JSONResponse(status_code=status_code, content={"error": message})
