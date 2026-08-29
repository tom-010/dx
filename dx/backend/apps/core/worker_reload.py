"""Restart a Celery worker when backend code changes — the dev-only "runserver for Celery".

Celery has had no reloader since 4.0 and `watchfiles`' CLI stops processes with SIGINT, which a
starting worker (first ~second, while importing) swallows for good. This module drives the
worker itself: SIGTERM (honoured at every stage: warm shutdown once the worker runs — running
tasks finish, reserved ones go back to the queue), SIGKILL only after `stop_timeout`.
"""

import signal
import subprocess
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from typing import Any

from watchfiles import Change

FileChange = tuple[Change, str]
Spawn = Callable[[], subprocess.Popen[bytes]]
Echo = Callable[[str], None]


def start_worker(argv: list[str]) -> subprocess.Popen[bytes]:
    """Start the worker in its own session: Ctrl+C in the terminal then reaches only the
    reloader, which forwards a single SIGTERM (a terminal SIGINT on top would count as Celery's
    "second signal" = cold shutdown, killing running tasks)."""
    return subprocess.Popen(argv, start_new_session=True)


def stop_worker(
    process: subprocess.Popen[bytes], stop_timeout: float, echo: Echo | None = None
) -> None:
    """Warm shutdown via SIGTERM; SIGKILL if the worker is still around after `stop_timeout`
    seconds (a killed task is re-queued only after the broker's visibility timeout)."""
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(stop_timeout)
    except subprocess.TimeoutExpired:
        if echo is not None:
            echo(f"worker still running after {stop_timeout:g} s, killing it")
        process.kill()
        process.wait()


@contextmanager
def _sigterm_as_interrupt() -> Any:  # noqa: ANN401  # contextmanager's generator type is opaque
    """Treat SIGTERM (`kill`, `docker compose stop`) like Ctrl+C, so the worker is stopped
    with the reloader instead of surviving it in its own session."""

    def raise_interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, raise_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_with_reload(
    spawn: Spawn, changes: Iterable[set[FileChange]], stop_timeout: float, echo: Echo
) -> int:
    """Run the worker, restart it for every set of changes, stop it on Ctrl+C.

    `changes` is normally `watchfiles.watch(...)` (blocks until files change, raises
    KeyboardInterrupt on SIGINT); tests pass a list. Returns the last worker's exit code.
    """
    process = spawn()
    interrupted = False
    with _sigterm_as_interrupt():
        try:
            for changed in changes:
                files = ", ".join(sorted({path for _, path in changed}))
                if process.poll() is not None:
                    echo(f"worker had exited with code {process.returncode}")
                echo(f"{files} changed, restarting worker")
                stop_worker(process, stop_timeout, echo)
                process = spawn()
        except KeyboardInterrupt:
            interrupted = True
        finally:
            # Ctrl+C reaches us more than once (`uv run` forwards it to its child on top of the
            # terminal's process-group delivery): a repeat must not abort the graceful stop.
            # Repeating SIGTERM is harmless — Celery's TERM handler is the same warm shutdown.
            while True:
                try:
                    if interrupted:
                        echo("stopping worker")
                        interrupted = False
                    stop_worker(process, stop_timeout, echo)
                    break
                except KeyboardInterrupt:
                    continue
    # Negative = ended by our signal (expected); positive = the worker exited on its own (crash).
    return max(process.returncode or 0, 0)
