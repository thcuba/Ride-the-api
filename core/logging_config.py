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
from pathlib import Path

import structlog

# Active configured level name, set by :func:`setup_logging`.
_active_level: str = "INFO"


def _resolve_level(level: str | int) -> tuple[int, str | None]:
    """Numeric level for ``level``, plus a warning when it had to fall back.

    Names are case-insensitive (``info`` == ``INFO``): ``Logger.setLevel``
    only accepts upper-case names, so a lower-case value in config.yaml used to
    stop the service at startup. An unknown name falls back to INFO with a
    warning instead of crashing.
    """
    if isinstance(level, int):
        return level, None
    name = str(level).strip().upper()
    mapping = logging.getLevelNamesMapping()
    if name in mapping:
        return mapping[name], None
    if name.isdigit():
        return int(name), None
    return logging.INFO, f"unknown log level {level!r}; using INFO"


def _build_handler(output: str) -> tuple[logging.Handler, str | None]:
    """Handler for ``output``: ``stdout``, ``stderr`` or a file path.

    A file path used to be ignored silently (everything went to stdout). If the
    file cannot be opened, logs go to stderr and a warning says why.
    """
    target = (output or "stdout").strip()
    lowered = target.lower()
    if lowered == "stdout":
        return logging.StreamHandler(sys.stdout), None
    if lowered == "stderr":
        return logging.StreamHandler(sys.stderr), None
    try:
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return logging.FileHandler(path, encoding="utf-8"), None
    except OSError as exc:
        return (
            logging.StreamHandler(sys.stderr),
            f"cannot open log file {target!r} ({exc}); logging to stderr",
        )


def setup_logging(level: str | int = "INFO", fmt: str = "json", output: str = "stdout") -> None:
    """Configure structlog as the root logging handler.

    Args:
        level: Minimum log level (name in any case, or numeric).
        fmt: Rendering format; ``json`` (default) or ``console``.
        output: ``stdout`` (default), ``stderr``, or a file path.
    """
    global _active_level  # noqa: PLW0603
    numeric_level, level_warning = _resolve_level(level)
    _active_level = logging.getLevelName(numeric_level)

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

    handler, output_warning = _build_handler(output)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()
    root.addHandler(handler)

    # Tunable to help stdlib ``logging.getLogger`` call sites stay compliant.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Reported once the handler is live, so the operator actually sees them.
    for warning in (level_warning, output_warning):
        if warning:
            logging.getLogger(__name__).warning("Logging setup: %s", warning)


def get_log_level() -> str:
    """Return the active configured log level name."""
    return _active_level
