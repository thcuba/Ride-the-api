"""Structured logging via ``structlog``, wired as the process root handler.

Replaces the bare ``logging.basicConfig`` call with a structlog pipeline so
every ``logging.getLogger(...)`` and ``structlog.get_logger(...)`` call site
produces consistent, machine-readable, filterable log records. The log level
and rendering format are driven by the ``observability.logging`` config section
so operators can switch between ``json`` (default) and ``console`` output.
"""

from __future__ import annotations

import logging
import sys

import structlog

# Active configured level name, set by :func:`setup_logging`.
_active_level: str = "INFO"


def setup_logging(level: str | int = "INFO", fmt: str = "json", output: str = "stdout") -> None:
    """Configure structlog as the root logging handler.

    Args:
        level: Minimum log level (name or numeric).
        fmt: Rendering format; ``json`` (default) or ``console``.
        output: Stream to emit to; ``stdout`` (default) or ``stderr``.
    """
    global _active_level
    _active_level = (
        level if isinstance(level, str) else logging.getLevelName(level)
    ).upper()

    if output.lower() == "stderr":
        stream = sys.stderr
    else:
        stream = sys.stdout

    shared_processors: list = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if fmt.lower() == "json"
        else structlog.dev.ConsoleRenderer()
    )

    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Tunable to help stdlib ``logging.getLogger`` call sites stay compliant.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_log_level() -> str:
    """Return the active configured log level name."""
    return _active_level
