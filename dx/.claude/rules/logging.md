---
paths:
  - "**/backend/config/logging.py"
  - "**/backend/apps/core/tests/test_logging.py"
---

## Logging (`config/logging.py`)

- One pipeline, two renderings: `logging.getLogger()` (Django, Celery, libraries) and
  `structlog.get_logger()` (our code) share one `ProcessorFormatter`. **`LOG_FORMAT=console`**
  (default with `DEBUG` — what `./scripts/serve.sh` shows) prints plain developer output,
  `HH:MM:SS LEVEL message key=value …` (time in `TIME_ZONE`, like runserver's own lines): one
  line per request (`GET /api/health 200`, 4xx as
  `WARN`, 5xx as `ERROR`), plain Python tracebacks, Celery's own task lines, the module name
  appended for our code and for warnings/errors. **`LOG_FORMAT=json`** (default without
  `DEBUG`, i.e. the docker image) prints one JSON object per line with the full structlog
  context for Loki/CloudWatch & co. `LOG_LEVEL` is the root level; `LOG_SQL=true` (dev only)
  logs every query.
- The console format deliberately hides what only matters for correlating lines in a log
  store: `request_id`, `user_id`, `ip`, task ids, `request_started`, and django-structlog's
  `task_started`/`task_succeeded` events (the worker's "received"/"succeeded" lines say the
  same). `compact_dev_events` + `DevRenderer` in `config/logging.py`; JSON keeps everything.
- Log events, not sentences: `log = structlog.get_logger(__name__)`;
  `log.info("dataset_imported", dataset_id=str(dataset.pk), rows=n)`. The event name is a
  constant, the key/value pairs are what you filter on later — no f-strings.
- django-structlog (`RequestMiddleware`, last in `MIDDLEWARE`) binds `request_id`, `user_id`
  (re-bound after the view, so the bearer-authenticated API user shows up) and `ip` to every
  line of a request and logs `request_started`/`request_finished` (status, path);
  `django.server`'s request lines and Django's "Not Found: /x" per 4xx are silenced to avoid
  duplicates (`django.request` stays at `ERROR`: its "Internal Server Error: /x" carries the
  traceback), Django's DEFAULT_LOGGING handlers are removed (else every Django line prints
  twice), and stdlib `extra=` is not carried into lines (Django/Celery put objects there). The Celery boot step in `config/celery.py`
  (`DJANGO_STRUCTLOG_CELERY_ENABLED`) carries the ids into task logs, and its `setup_logging`
  receiver (`configure_worker_logging`) makes the worker use `LOGGING` instead of Celery's own
  root handler — so the worker prints the same plain/JSON lines as the web process.
- Tests: `apps/core/tests/test_logging.py` (both formats; stdlib records get the context; the
  console compaction; Django's handlers gone; the worker hook).
