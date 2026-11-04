"""Serving re-built HTTP responses: headers and status that are safe to send.

A locally served response is rendered from a learned template, so headers that
described one particular transfer of the vendor's original response must not
be replayed with it.
"""

from __future__ import annotations

# Headers about a single transfer, not the message. Replayed on a re-rendered
# body they lie: a stale Content-Length truncated the HTTP response ("content
# shorter than Content-Length") and made h11 reject the TLS one outright, a
# Content-Encoding claims a compression the body no longer has, and
# Connection/Keep-Alive describe a link that is not this one.
FRAMING_HEADERS = frozenset(
    {
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "content-encoding",
    }
)

_MIN_FINAL_STATUS, _MAX_STATUS = 200, 599


def without_framing_headers(headers: object) -> dict[str, str]:
    """``headers`` without the framing headers, as ``str: str`` (non-dict -> ``{}``)."""
    if not isinstance(headers, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in FRAMING_HEADERS
    }


def servable_status(value: object, default: int = 200) -> int:
    """A final status (200-599) to answer with, else ``default``.

    A learned 1xx (a captured "100 Continue", say) cannot end an exchange and
    broke the response the same way a stale Content-Length did.
    """
    if isinstance(value, bool):
        return default
    try:
        status = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return status if _MIN_FINAL_STATUS <= status <= _MAX_STATUS else default
