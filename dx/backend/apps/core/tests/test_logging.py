"""config/logging.py: one formatter for structlog and stdlib records — plain console or JSON."""

import json
import logging

import structlog
from celery.signals import setup_logging

from config.celery import configure_worker_logging
from config.logging import (
    REQUEST_LOGGER,
    TASK_LOGGER,
    DevRenderer,
    build_formatter,
    compact_dev_events,
    logging_config,
)


def _record(
    message: str = "hello %s",
    args: tuple[object, ...] = ("world",),
    name: str = "apps.test",
    level: int = logging.INFO,
) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, args, None)


def _event(
    event: str, logger: str = "apps.test", level: str = "info", **context: object
) -> dict[str, object]:
    return {
        "event": event,
        "logger": logger,
        "level": level,
        "timestamp": "2026-08-28T20:38:05.519791Z",
        **context,
    }


def _render(event_dict: dict[str, object]) -> str:
    return DevRenderer(colors=False)(None, "info", compact_dev_events(None, "info", event_dict))


# --- JSON (production) ---------------------------------------------------------------------


def test_json_format_is_one_object_per_line() -> None:
    line = build_formatter("json").format(_record())

    data = json.loads(line)
    assert data["event"] == "hello world"
    assert data["level"] == "info"
    assert data["logger"] == "apps.test"
    assert data["timestamp"].endswith("Z")


def test_stdlib_records_get_the_structlog_context() -> None:
    """django-structlog binds request_id/user_id; plain `logging` calls carry them too."""
    structlog.contextvars.bind_contextvars(request_id="req-1")
    try:
        data = json.loads(build_formatter("json").format(_record()))
    finally:
        structlog.contextvars.clear_contextvars()

    assert data["request_id"] == "req-1"


def test_json_keeps_request_context_and_both_request_events() -> None:
    """Everything the console hides is what production correlates lines with."""
    formatter = build_formatter("json")
    structlog.contextvars.bind_contextvars(request_id="req-1", ip="127.0.0.1", user_id="u1")
    try:
        started = json.loads(formatter.format(_record("request_started", (), REQUEST_LOGGER)))
        finished = json.loads(formatter.format(_record("request_finished", (), REQUEST_LOGGER)))
    finally:
        structlog.contextvars.clear_contextvars()

    assert started["event"] == "request_started"
    assert finished["event"] == "request_finished"
    assert finished["request_id"] == "req-1"
    assert finished["ip"] == "127.0.0.1"
    assert finished["user_id"] == "u1"


# --- Console (development) -----------------------------------------------------------------


def test_console_line_is_plain_and_readable() -> None:
    line = build_formatter("console").format(_record())

    assert line.endswith(" INFO  hello world  [apps.test]")
    assert len(line.split(" ", 1)[0]) == len("HH:MM:SS")
    assert "\x1b[" not in line


def test_console_hides_correlation_context() -> None:
    line = _render(
        _event("dataset_imported", request_id="req-1", ip="127.0.0.1", user_id="u1", rows=3)
    )

    assert line.endswith(" INFO  dataset_imported rows=3  [apps.test]")
    assert "req-1" not in line


def test_console_prints_one_line_per_request() -> None:
    finished = _render(
        _event("request_finished", REQUEST_LOGGER, request="GET /api/health", code=200)
    )

    assert finished.endswith(" INFO  GET /api/health 200")
    assert "django_structlog" not in finished


def test_console_drops_request_started_and_task_events() -> None:
    for event_dict in (
        _event("request_started", REQUEST_LOGGER, request="GET /api/health"),
        _event("task_started", TASK_LOGGER, task="apps.core.tasks.add"),
        _event("task_succeeded", TASK_LOGGER, level="error"),
    ):
        try:
            compact_dev_events(None, "info", event_dict)
        except structlog.DropEvent:
            continue
        raise AssertionError(f"{event_dict['event']} was not dropped")


def test_console_marks_failed_requests_as_errors() -> None:
    line = _render(
        _event("request_failed", REQUEST_LOGGER, "error", request="POST /api/x", code=500)
    )

    assert line.endswith(" ERROR POST /api/x 500")


def test_console_names_the_logger_for_warnings_but_not_for_library_info() -> None:
    routine = _render(_event("Task add[1] received", "celery.worker.strategy"))
    warning = _render(_event("Broken pipe", "django.server", "warning"))

    assert routine.endswith(" INFO  Task add[1] received")
    assert warning.endswith(" WARN  Broken pipe  [django.server]")


def test_console_quotes_values_with_spaces() -> None:
    line = _render(_event("x", name="a b", n=1, empty=""))

    assert line.endswith(" INFO  x empty='' n=1 name='a b'  [apps.test]")


def test_console_appends_a_plain_traceback() -> None:
    record = _record("boom", ())
    try:
        raise ValueError("bad")
    except ValueError as exc:
        record.exc_info = (type(exc), exc, exc.__traceback__)

    line = build_formatter("console").format(record)

    first, rest = line.split("\n", 1)
    assert first.endswith(" INFO  boom  [apps.test]")
    assert rest.startswith("Traceback (most recent call last):")
    assert rest.rstrip().endswith("ValueError: bad")


def test_stdlib_extra_is_not_carried() -> None:
    """Django/Celery attach objects via `extra=` (WSGIRequest, socket, task dict)."""
    record = _record("Not Found: /x", (), "django.request", logging.WARNING)
    record.status_code = 404  # what `extra=` does
    record.request = object()

    console = build_formatter("console").format(record)
    data = json.loads(build_formatter("json").format(record))

    assert console.endswith(" WARN  Not Found: /x  [django.request]")
    assert "status_code" not in data and "request" not in data


def test_console_shows_local_time() -> None:
    line = DevRenderer(colors=False)(
        None, "info", {"event": "x", "level": "info", "timestamp": "2026-08-28T20:38:05Z"}
    )
    assert line.endswith(" INFO  x")
    assert len(line) == len("HH:MM:SS INFO  x")


def test_console_colors_only_when_asked() -> None:
    line = DevRenderer(colors=True)(None, "info", _event("x", level="error"))

    assert "\x1b[31mERROR\x1b[0m" in line


# --- Django LOGGING ------------------------------------------------------------------------


def test_logging_config_levels() -> None:
    config = logging_config(level="WARNING", fmt="json", sql=True)

    assert config["root"] == {"handlers": ["console"], "level": "WARNING"}
    assert config["loggers"]["django.db.backends"]["level"] == "DEBUG"
    assert logging_config(level="INFO", fmt="console")["loggers"]["django.db.backends"] == {
        "level": "INFO"
    }


def test_logging_config_removes_djangos_default_handlers() -> None:
    """Django applies DEFAULT_LOGGING first (`django` → console, `django.server` → its own
    handler, propagate off); left alone, every Django line would be printed twice."""
    loggers = logging_config(level="INFO", fmt="console")["loggers"]

    assert loggers["django"]["handlers"] == []
    assert loggers["django.server"] == {"handlers": [], "level": "CRITICAL", "propagate": True}


def test_django_lines_go_through_the_root_handler_exactly_once() -> None:
    """The running configuration (settings.LOGGING applied on top of Django's default)."""
    for name in ("django", "django.server", "django.utils.autoreload"):
        logger = logging.getLogger(name)
        assert logger.handlers == [], name
        assert logger.propagate, name
    assert _structured_root_handlers() == 1  # pytest adds its own capture handlers


def test_celery_worker_uses_the_django_logging_config() -> None:
    assert setup_logging.has_listeners()
    configure_worker_logging()  # idempotent: still the one handler from LOGGING
    assert _structured_root_handlers() == 1


def _structured_root_handlers() -> int:
    root = logging.getLogger()
    return sum(
        isinstance(handler.formatter, structlog.stdlib.ProcessorFormatter)
        for handler in root.handlers
    )
