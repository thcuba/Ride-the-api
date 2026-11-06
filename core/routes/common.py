"""Shared utilities and helpers for route modules."""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _client_error(exc: BaseException, message: str, status_code: int = 400) -> JSONResponse:
    """Return a sanitized JSONResponse for client errors (400, 422, etc.).

    Internal exception details are stripped in production to prevent information disclosure.
    """
    logger.warning("%s: %s", message, exc)
    return JSONResponse(status_code=status_code, content={"error": message})
