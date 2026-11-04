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
    for handler in root.handlers:
        handler.close()
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


@pytest.mark.parametrize("level", ["info", "Info", "INFO", " warning "])
def test_level_names_are_case_insensitive(level):
    """A lower-case level in config.yaml used to raise at startup."""
    logging_config.setup_logging(level=level, fmt="json", output="stderr")
    expected = logging.getLevelNamesMapping()[level.strip().upper()]
    assert logging.getLogger().level == expected
    assert logging_config.get_log_level() == level.strip().upper()


def test_unknown_level_falls_back_to_info_with_a_warning(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    logging_config.setup_logging(level="verbose", fmt="json", output="stderr")
    assert logging.getLogger().level == logging.INFO
    assert logging_config.get_log_level() == "INFO"
    assert "unknown log level" in stream.getvalue()


def test_file_output_writes_to_the_file(tmp_path):
    log_file = tmp_path / "logs" / "app.log"
    logging_config.setup_logging(level="INFO", fmt="json", output=str(log_file))
    logging.getLogger("core.filetest").info("to the file")
    for handler in logging.getLogger().handlers:
        handler.flush()
    record = json.loads(log_file.read_text().strip().splitlines()[-1])
    assert record["event"] == "to the file"


def test_console_renderer_not_json(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    logging_config.setup_logging(level="INFO", fmt="console", output="stderr")
    logging.getLogger("core.console").info("human readable")
    emitted = stream.getvalue().strip()
    assert "human readable" in emitted
