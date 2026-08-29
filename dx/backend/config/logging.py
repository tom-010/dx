"""Logging: plain lines for developers, structured JSON (structlog) for production.

One pipeline for everything: `logging.getLogger()` (Django, Celery, libraries) and
`structlog.get_logger()` (our code) end up in the same `ProcessorFormatter`. `LOG_FORMAT`
decides how it renders:

- `console` (default with DEBUG — what ./scripts/serve.sh shows): readable terminal output,
  `HH:MM:SS LEVEL message key=value …` (`DevRenderer`). One line per request
  (`GET /api/health 200`), plain Python tracebacks, Celery's own task lines. The correlation
  context django-structlog binds (`request_id`, `user_id`, `ip`, task ids) is not shown.
- `json` (default in production, i.e. the docker image): one JSON object per line with the
  full context — `request_id`, `user_id`, `ip`, task ids — for Loki/CloudWatch & co.
  django-structlog logs `request_started`/`request_finished` and `task_started`/
  `task_succeeded`/…; its Celery integration carries the request id into task logs.

Our code logs key/value pairs, not formatted strings, in both formats:

    import structlog
    log = structlog.get_logger()
    log.info("dataset_imported", dataset_id=str(dataset.pk), rows=count)

Settings: `LOG_LEVEL`, `LOG_FORMAT` (`console`|`json`), `LOG_SQL` (config/env.py).
"""

import logging
import sys
from datetime import datetime
from typing import Any, Literal

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

LogFormat = Literal["console", "json"]

# Applied to every record (structlog and stdlib) before rendering.
_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    # No `ExtraAdder`: our code passes key/value pairs to structlog, and the stdlib `extra=`
    # Django and Celery attach holds objects (the WSGIRequest, a socket, Celery's task dict).
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]

# --- Console format -------------------------------------------------------------------------

REQUEST_LOGGER = "django_structlog.middlewares.request"
TASK_LOGGER = "django_structlog.celery.receivers"

# Context bound by django-structlog that only matters for correlating lines in a log store.
HIDDEN_IN_CONSOLE: frozenset[str] = frozenset(
    {"request_id", "correlation_id", "ip", "user_agent", "user_id", "task_id", "parent_task_id"}
)

_LEVEL_LABELS = {
    "debug": "DEBUG",
    "info": "INFO ",
    "warning": "WARN ",
    "error": "ERROR",
    "critical": "CRIT ",
}
_LEVEL_COLORS = {
    "debug": "\x1b[36m",
    "info": "\x1b[32m",
    "warning": "\x1b[33m",
    "error": "\x1b[31m",
    "critical": "\x1b[1;31m",
}
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def compact_dev_events(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """Console only: one line per request, no duplicate task lines.

    django-structlog logs `request_started` + `request_finished` (with `request='GET /x'`,
    `code=200`); keep only the latter, as `GET /x 200`. Its Celery events (`task_started`, …)
    are dropped: the worker's own "received"/"succeeded" lines (with the traceback on failure)
    say the same. JSON keeps all of them.
    """
    logger_name = event_dict.get("logger")
    if logger_name == TASK_LOGGER:
        raise structlog.DropEvent
    if logger_name == REQUEST_LOGGER:
        event = event_dict.get("event")
        if event == "request_started":
            raise structlog.DropEvent
        if event in {"request_finished", "request_failed"}:
            request = event_dict.pop("request", "")
            code = event_dict.pop("code", "")
            event_dict["event"] = f"{request} {code}".strip()
            event_dict.pop("logger", None)
    return event_dict


class DevRenderer:
    """`HH:MM:SS LEVEL message key=value …  [logger]`; tracebacks on the following lines.

    The logger name is appended for our own code (`apps.*`, `config.*`) and for warnings and
    errors — where it helps to find the call — not for Django's and Celery's routine lines.
    """

    def __init__(self, *, colors: bool) -> None:
        self.colors = colors

    def __call__(self, _logger: WrappedLogger, _name: str, event_dict: EventDict) -> str:
        level = str(event_dict.pop("level", "info"))
        logger_name = str(event_dict.pop("logger", ""))
        event = str(event_dict.pop("event", ""))
        clock = _clock(event_dict.pop("timestamp", None))
        exception = event_dict.pop("exception", None)
        stack = event_dict.pop("stack", None)
        for key in HIDDEN_IN_CONSOLE:
            event_dict.pop(key, None)

        parts = [clock, self._paint(_LEVEL_COLORS.get(level, ""), _LEVEL_LABELS.get(level, level))]
        parts.append(event)
        parts.extend(f"{key}={_value(value)}" for key, value in sorted(event_dict.items()))
        if logger_name and (
            level not in {"debug", "info"} or logger_name.startswith(("apps.", "config."))
        ):
            parts.append(self._paint(_DIM, f" [{logger_name}]"))
        line = " ".join(part for part in parts if part)
        for block in (stack, exception):
            if block:
                line = f"{line}\n{block}"
        return line

    def _paint(self, color: str, text: str) -> str:
        return f"{color}{text}{_RESET}" if self.colors and color else text


def _clock(timestamp: object) -> str:
    """`HH:MM:SS` from the ISO UTC timestamp `TimeStamper` added, in the process time zone —
    Django sets that to `TIME_ZONE` (UTC), so it matches runserver's own lines and the DB."""
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M:%S")
        except ValueError:
            pass
    return datetime.now().astimezone().strftime("%H:%M:%S")


def _value(value: object) -> str:
    text = str(value)
    return repr(value) if not text or any(char.isspace() for char in text) else text


# --- Formatter + Django LOGGING ------------------------------------------------------------


def build_formatter(fmt: LogFormat) -> logging.Formatter:
    """The stdlib formatter that renders both structlog and plain logging records."""
    processors: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        # exc_info → the traceback as a string (JSON field / lines below the console line).
        structlog.processors.format_exc_info,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors += [compact_dev_events, DevRenderer(colors=sys.stdout.isatty())]
    return structlog.stdlib.ProcessorFormatter(
        processors=processors, foreign_pre_chain=_SHARED_PROCESSORS
    )


def configure_structlog() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def logging_config(*, level: str, fmt: LogFormat, sql: bool = False) -> dict[str, Any]:
    """Django's LOGGING setting: everything to stdout through the one formatter above.

    Django applies its own DEFAULT_LOGGING before this; the `django` and `django.server`
    entries drop the handlers it attached there, otherwise every Django line is printed twice
    (plain, then formatted). The Celery worker uses the same config
    (`config/celery.py::configure_worker_logging`).
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"structured": {"()": build_formatter, "fmt": fmt}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "structured",
            }
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            # Only the root handler, see the docstring.
            "django": {"handlers": [], "level": "INFO"},
            # django-structlog logs every request with method/path/status: runserver's own
            # line would repeat it, and so would Django's "Not Found: /x" per 4xx — but its
            # "Internal Server Error: /x" carries the traceback, so 5xx stay.
            "django.server": {"handlers": [], "level": "CRITICAL", "propagate": True},
            "django.request": {"level": "ERROR"},
            "django.db.backends": {"level": "DEBUG" if sql else "INFO"},
            "django.utils.autoreload": {"level": "INFO"},
            # Chatty at DEBUG without adding much.
            "boto3": {"level": "INFO"},
            "botocore": {"level": "INFO"},
            "urllib3": {"level": "INFO"},
            "kombu": {"level": "INFO"},
            "amqp": {"level": "INFO"},
        },
    }
