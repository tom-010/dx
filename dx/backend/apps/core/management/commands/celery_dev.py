"""`manage.py celery_dev [CELERY ARGS]` — a Celery worker that restarts on code changes.

Dev only (`./scripts/celery.sh` runs it). Watches `.py` files under `apps/` and `config/`;
restarts are warm (running tasks finish first), see `apps/core/worker_reload.py`.

    uv run python manage.py celery_dev                 # worker, --concurrency=1
    uv run python manage.py celery_dev -- -c 4 -Q io   # extra args go to `celery worker`
"""

import subprocess
import sys
from pathlib import Path

import djclick as click
from watchfiles import PythonFilter, watch

from apps.core import worker_reload
from config.env import BASE_DIR

WATCHED = ("apps", "config")


@click.command(context_settings={"ignore_unknown_options": True})
@click.option(
    "--stop-timeout",
    type=click.FloatRange(min=0),
    default=150,
    show_default=True,
    help=(
        "Seconds a running task may take to finish on restart before the worker is killed. "
        "Generous on purpose: a killed task comes back only after the broker's visibility "
        "timeout (2 h), while a task that drains is re-queued at once."
    ),
)
@click.argument("celery_args", nargs=-1, type=click.UNPROCESSED)
def command(stop_timeout: float, celery_args: tuple[str, ...]) -> None:
    """Run a Celery worker and restart it whenever backend code changes (like runserver)."""
    argv = [sys.executable, "-m", "celery", "-A", "config", "worker", "--loglevel=info"]
    if not any(arg.startswith(("-c", "--concurrency")) for arg in celery_args):
        argv.append("--concurrency=1")  # one process: fast restarts
    argv.extend(celery_args)

    paths = [BASE_DIR / name for name in WATCHED]
    click.echo(f"celery_dev: watching {', '.join(WATCHED)} for .py changes (Ctrl+C stops)")

    def echo(message: str) -> None:
        click.echo(f"celery_dev: {message}")

    def spawn() -> subprocess.Popen[bytes]:
        return worker_reload.start_worker(argv)

    changes = (
        {(change, str(Path(path).relative_to(BASE_DIR))) for change, path in changed}
        for changed in watch(*paths, watch_filter=PythonFilter())
    )
    sys.exit(worker_reload.run_with_reload(spawn, changes, stop_timeout, echo))
