"""Structured logging (structlog) for Django, Celery and management commands.

One pipeline for everything: `logging.getLogger()` (Django, libraries) and
`structlog.get_logger()` (our code) both end up in `ProcessorFormatter`, which renders either
human-readable console lines (dev) or one JSON object per line (prod, for Loki/CloudWatch/...).
django-structlog binds `request_id`, `user_id` and `ip` to every line of a request and logs
`request_started`/`request_finished`; its Celery integration carries the ids into task logs.

Our code logs key/value pairs, not formatted strings:

    import structlog
    log = structlog.get_logger()
    log.info("dataset_imported", dataset_id=str(dataset.pk), rows=count)

Settings: `LOG_LEVEL`, `LOG_FORMAT` (`console`|`json`), `LOG_SQL` (config/env.py).
"""

import logging
import sys
from typing import Any, Literal

import structlog
from structlog.typing import Processor

LogFormat = Literal["console", "json"]

# Applied to every record (structlog and stdlib) before rendering.
_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.stdlib.ExtraAdder(),
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
]


def renderer(fmt: LogFormat) -> Processor:
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())


def build_formatter(fmt: LogFormat) -> logging.Formatter:
    """The stdlib formatter that renders both structlog and plain logging records."""
    processors: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if fmt == "json":
        # ConsoleRenderer prints tracebacks itself; JSON needs them as a string field.
        processors.insert(0, structlog.processors.format_exc_info)
    return structlog.stdlib.ProcessorFormatter(
        processors=[*processors, renderer(fmt)], foreign_pre_chain=_SHARED_PROCESSORS
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
    """Django's LOGGING setting: everything to stdout through the structlog formatter."""
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
            # django-structlog's request_finished already has method/path/status/user/ip.
            "django.server": {"level": "WARNING"},
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
