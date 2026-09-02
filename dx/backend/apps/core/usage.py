"""What was run: one row per `manage.py` invocation.

Commands are how this project is operated, so which ones are actually used — and with which
arguments — is worth keeping. `manage.py tui` reads it back to put what you use at the top of
the list.

Recorded centrally, in `manage.py` itself (`record_run`), before the command runs: that is the
one place every invocation passes through, so no command has to remember to log itself and a
new one is covered the day it is written. `call_command()` from application code and the test
suite deliberately does **not** land here — this is a record of what a person ran.

Bookkeeping must never be the reason a command fails: every function here swallows database
errors. The first `migrate` on an empty database runs before this table exists.
"""

import os
import shlex
from collections.abc import Sequence

import structlog
from django.db import models

from apps.core.models import ActiveManager, VersionedModel

log = structlog.get_logger(__name__)

#: Commands that do not record a run. `tui` is the tool that *reads* this log; if it wrote to it
#: too, opening the explorer would rearrange the list it is showing you.
NOT_RECORDED = frozenset({"tui"})


class CommandRun(VersionedModel):
    """One `manage.py <name> <arguments>`, as it was typed.

    The arguments are one string on purpose: this is a record of what was run, not a structure
    to query. Nothing here is tenant data — a command is run by an operator, not by a user —
    so the model is a plain `VersionedModel` with no `owner` and no row-level security.
    """

    # Direct `VersionedModel` subclasses declare the manager pair themselves (see VersionedModel).
    objects = ActiveManager()
    all_objects = models.Manager()

    name = models.CharField(max_length=100)
    arguments = models.TextField(blank=True)

    class Meta(VersionedModel.Meta):
        indexes = [models.Index(fields=["name", "-created"])]

    @staticmethod
    def example() -> CommandRun:
        return CommandRun(name="hello_world", arguments="--shout world")

    def __str__(self) -> str:
        return f"manage.py {self.name} {self.arguments}".rstrip()


def record_run(argv: Sequence[str]) -> None:
    """Record one invocation. Never raises.

    Called *before* the command runs, so a long-running one (`runserver`, `celery_dev`) is in
    the log while it is still running rather than when it is finally stopped.
    """
    if not argv or argv[0].startswith("-") or argv[0] in NOT_RECORDED:
        return  # `manage.py`, `manage.py --version`, or a command that opts out
    if os.environ.get("RUN_MAIN"):
        return  # the autoreloader's child process; its parent already recorded the run
    try:
        CommandRun.create(operation=None, sources=[], name=argv[0], arguments=shlex.join(argv[1:]))
    except Exception:
        # No database yet (the first `migrate`), none at all, a read-only replica: all fine.
        log.debug("command_run_not_recorded", command=argv[0], exc_info=True)


def recent_runs(limit: int = 20) -> list[str]:
    """The commands most recently run, newest first, each name once. Never raises."""
    try:
        rows = (
            CommandRun.objects.values("name")
            .annotate(last=models.Max("created"))
            .order_by("-last")[:limit]
        )
        return [str(row["name"]) for row in rows]
    except Exception:
        log.debug("recent_runs_unavailable", exc_info=True)
        return []
