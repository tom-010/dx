"""config/logging.py: one formatter for structlog and stdlib records, console or JSON."""

import json
import logging

import structlog

from config.logging import build_formatter, logging_config


def _record(message: str = "hello %s", args: tuple[object, ...] = ("world",)) -> logging.LogRecord:
    return logging.LogRecord("apps.test", logging.INFO, __file__, 1, message, args, None)


def test_json_format_is_one_object_per_line() -> None:
    line = build_formatter("json").format(_record())

    data = json.loads(line)
    assert data["event"] == "hello world"
    assert data["level"] == "info"
    assert data["logger"] == "apps.test"
    assert data["timestamp"].endswith("Z")


def test_console_format_is_human_readable() -> None:
    line = build_formatter("console").format(_record())

    assert "hello world" in line
    assert "[info" in line
    assert "apps.test" in line


def test_stdlib_records_get_the_structlog_context() -> None:
    """django-structlog binds request_id/user_id; plain `logging` calls carry them too."""
    structlog.contextvars.bind_contextvars(request_id="req-1")
    try:
        data = json.loads(build_formatter("json").format(_record()))
    finally:
        structlog.contextvars.clear_contextvars()

    assert data["request_id"] == "req-1"


def test_logging_config_levels() -> None:
    config = logging_config(level="WARNING", fmt="json", sql=True)

    assert config["root"] == {"handlers": ["console"], "level": "WARNING"}
    assert config["loggers"]["django.db.backends"]["level"] == "DEBUG"
    assert logging_config(level="INFO", fmt="console")["loggers"]["django.db.backends"] == {
        "level": "INFO"
    }
