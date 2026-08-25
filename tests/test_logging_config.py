"""Tests for the structlog-based structured logging configuration."""

import io
import json
import logging

import pytest

from core import logging_config


@pytest.fixture(autouse=True)
def _cleanup_root_handlers():
    yield
    root = logging.getLogger()
    root.handlers.clear()


def test_get_log_level_defaults_to_info():
    assert logging_config.get_log_level() == "INFO"


def test_setup_logging_installs_a_single_root_handler():
    logging_config.setup_logging(level="WARNING", fmt="json", output="stderr")
    root = logging.getLogger()
    # structlog config + root handler must not stack verbose handlers.
    assert len(root.handlers) == 1


def test_setup_records_configured_level():
    logging_config.setup_logging(level="DEBUG", fmt="json")
    assert logging_config.get_log_level() == "DEBUG"


def test_stdlog_call_is_emitted_as_json_into_stream(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    logging_config.setup_logging(level="INFO", fmt="json", output="stderr")
    logging.getLogger("core.test").info("hello structured world", extra={"k": "v"})
    emitted = stream.getvalue().strip()
    # The ProcessorFormatter emits one JSON object per line.
    record = json.loads(emitted.splitlines()[-1])
    assert record["event"] == "hello structured world"
    assert record["level"] == "info"
    assert record["logger"] == "core.test"


def test_console_renderer_not_json(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    logging_config.setup_logging(level="INFO", fmt="console", output="stderr")
    logging.getLogger("core.console").info("human readable")
    emitted = stream.getvalue().strip()
    assert "human readable" in emitted