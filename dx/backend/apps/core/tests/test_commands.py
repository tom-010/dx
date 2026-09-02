import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner
from django.apps import apps as django_apps
from django.db import DatabaseError
from pytest_django.fixtures import Settings
from watchfiles import Change

from apps.accounts.models import User
from apps.core import cli, usage, worker_reload
from apps.core.history import hard_delete
from apps.core.management.commands import (
    backup,
    deleteapp,
    ensure_bucket,
    hello_world,
    newcommand,
    restore,
    tui,
)
from apps.core.storage import BucketStatus
from apps.core.storage import ensure_bucket as ensure_bucket_fn
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for
from apps.datasets.models import Dataset

runner = CliRunner()


@dataclass
class FakeS3Client:
    """Just enough of boto3's S3 client for ensure_bucket()."""

    buckets: dict[str, str | None] = field(default_factory=dict)  # name -> versioning status
    calls: list[str] = field(default_factory=list)

    def head_bucket(self, Bucket: str) -> None:  # boto3's argument names
        self.calls.append("head")
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")

    def create_bucket(self, Bucket: str) -> None:
        self.calls.append("create")
        self.buckets[Bucket] = None

    def get_bucket_versioning(self, Bucket: str) -> dict[str, Any]:
        status = self.buckets[Bucket]
        return {"Status": status} if status else {}

    def put_bucket_versioning(self, Bucket: str, VersioningConfiguration: dict[str, str]) -> None:
        self.calls.append("versioning")
        self.buckets[Bucket] = VersioningConfiguration["Status"]


def test_ensure_bucket_creates_and_enables_versioning() -> None:
    client = FakeS3Client()

    status = ensure_bucket_fn(client, "dx-media")  # type: ignore[arg-type]

    assert status == BucketStatus(bucket="dx-media", created=True, versioning_enabled=True)
    assert client.buckets == {"dx-media": "Enabled"}


def test_ensure_bucket_is_idempotent() -> None:
    client = FakeS3Client(buckets={"dx-media": "Enabled"})

    status = ensure_bucket_fn(client, "dx-media")  # type: ignore[arg-type]

    assert status == BucketStatus(bucket="dx-media", created=False, versioning_enabled=False)
    assert client.calls == ["head"]


def test_ensure_bucket_turns_versioning_on_for_existing_bucket() -> None:
    client = FakeS3Client(buckets={"dx-media": "Suspended"})

    status = ensure_bucket_fn(client, "dx-media")  # type: ignore[arg-type]

    assert status.versioning_enabled
    assert client.buckets == {"dx-media": "Enabled"}


def test_ensure_bucket_propagates_other_errors() -> None:
    class Forbidden(FakeS3Client):
        def head_bucket(self, Bucket: str) -> None:
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket")

    with pytest.raises(ClientError, match="Forbidden"):
        ensure_bucket_fn(Forbidden(), "dx-media")  # type: ignore[arg-type]


def test_ensure_bucket_command_skips_local_storage() -> None:
    result = runner.invoke(ensure_bucket.command, [])  # tests run with the local backend

    assert result.exit_code == 0, result.output
    assert "default: local storage" in result.output
    assert "backups: local storage" in result.output


# --- hello_world: the reference command --------------------------------------------------------


@pytest.mark.django_db
def test_hello_world_greets_and_shows_the_environment() -> None:
    result = runner.invoke(hello_world.command, ["dx", "--shout", "-n", "2"])

    assert result.exit_code == 0, result.output
    assert result.output.count("HELLO, DX!") == 2
    assert "Environment" in result.output
    assert "users" in result.output


def test_hello_world_validates_options() -> None:
    result = runner.invoke(hello_world.command, ["--repeat", "0"])

    assert result.exit_code == 2  # click usage error
    assert "Invalid value for '--repeat'" in result.output


# --- backup / restore ---------------------------------------------------------------------------


@pytest.fixture
def backup_dir(settings: Settings, tmp_path: Path) -> Path:
    settings.STORAGES = {
        **settings.STORAGES,
        "backups": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path)},
        },
    }
    return tmp_path


@pytest.mark.django_db
@pytest.mark.cross_tenant  # a dump spans tenants: it needs the table owner's role
def test_backup_and_restore_commands(user: User, backup_dir: Path) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="keep me")

    created = runner.invoke(backup.command, [])
    assert created.exit_code == 0, created.output
    assert "wrote dx-" in created.output
    (dump,) = backup_dir.glob("dx-*.json.gz")

    listed = runner.invoke(backup.command, ["--list"])
    assert dump.name in listed.output

    with acting_as(user), hard_delete():
        Dataset.all_objects.all().hard_delete()
    restored = runner.invoke(restore.command, ["--latest", "--yes"])
    assert restored.exit_code == 0, restored.output
    with acting_as(user):
        assert Dataset.objects.get(pk=dataset.pk).name == "keep me"


@pytest.mark.django_db
def test_restore_needs_a_name_or_latest(backup_dir: Path) -> None:
    assert runner.invoke(restore.command, ["-y"]).exit_code == 2
    missing = runner.invoke(restore.command, ["nope.json.gz", "-y"])
    assert missing.exit_code == 1
    assert "no such backup" in missing.output
    assert "no backups found" in runner.invoke(restore.command, ["--latest", "-y"]).output


# --- celery_dev: worker auto-reload --------------------------------------------------------------


def test_stop_worker_terminates_gracefully_and_kills_stragglers() -> None:
    polite = subprocess.Popen(["sleep", "60"])
    worker_reload.stop_worker(polite, stop_timeout=5)
    assert polite.returncode == -signal.SIGTERM

    ignore_term = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(9)"
    )
    stubborn = subprocess.Popen([sys.executable, "-c", ignore_term], stdout=subprocess.PIPE)
    assert stubborn.stdout is not None
    assert stubborn.stdout.readline() == b"ready\n"  # SIG_IGN installed before we signal
    worker_reload.stop_worker(stubborn, stop_timeout=0.2)
    stubborn.stdout.close()
    assert stubborn.returncode == -signal.SIGKILL

    worker_reload.stop_worker(stubborn, stop_timeout=1)  # already gone: no error


def test_run_with_reload_restarts_the_worker_per_change_set() -> None:
    started: list[subprocess.Popen[bytes]] = []
    messages: list[str] = []

    def spawn() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(["sleep", "60"])
        started.append(process)
        return process

    modified = Change.modified
    changes = [{(modified, "apps/core/tasks.py")}, {(modified, "config/settings.py")}]

    code = worker_reload.run_with_reload(spawn, changes, stop_timeout=5, echo=messages.append)

    assert len(started) == 3  # initial + one per change set
    assert all(p.returncode == -signal.SIGTERM for p in started)
    assert messages == [
        "apps/core/tasks.py changed, restarting worker",
        "config/settings.py changed, restarting worker",
    ]
    assert code == 0  # a SIGTERM'd worker is the expected end, not a failure


def test_run_with_reload_survives_a_repeated_ctrl_c() -> None:
    """`uv run` forwards Ctrl+C to its child on top of the process-group delivery, so a second
    KeyboardInterrupt lands while the worker is being stopped — it must not abort the stop."""
    workers: list[subprocess.Popen[bytes]] = []
    messages: list[str] = []

    def spawn() -> subprocess.Popen[bytes]:
        workers.append(subprocess.Popen(["sleep", "60"]))
        return workers[-1]

    def ctrl_c() -> set[worker_reload.FileChange]:
        raise KeyboardInterrupt

    def echo(message: str) -> None:
        messages.append(message)
        if messages.count("stopping worker") == 1:
            raise KeyboardInterrupt  # the repeat, arriving mid-stop

    code = worker_reload.run_with_reload(spawn, iter(ctrl_c, None), stop_timeout=5, echo=echo)

    assert workers[0].returncode == -signal.SIGTERM
    assert messages == ["stopping worker", "stopping worker"]
    assert code == 0


def test_run_with_reload_stops_the_worker_on_sigterm() -> None:
    """`kill`/`docker compose stop` must not leave the worker running in its own session."""
    workers: list[subprocess.Popen[bytes]] = []
    messages: list[str] = []

    def spawn() -> subprocess.Popen[bytes]:
        workers.append(subprocess.Popen(["sleep", "60"]))
        return workers[-1]

    def changes_then_sigterm() -> Iterator[set[worker_reload.FileChange]]:
        yield {(Change.added, "apps/x.py")}
        os.kill(os.getpid(), signal.SIGTERM)  # handled synchronously by the reloader's handler
        yield set()  # never reached

    worker_reload.run_with_reload(
        spawn, changes_then_sigterm(), stop_timeout=5, echo=messages.append
    )

    assert [p.returncode for p in workers] == [-signal.SIGTERM, -signal.SIGTERM]
    assert messages[-1] == "stopping worker"
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # handler restored


# --- The command index and `manage.py tui` ----------------------------------------------------


def test_every_app_can_hold_commands() -> None:
    """Commands are how this project is operated, so every app has the directory ready — and
    `manage.py newapp` scaffolds it (apps/core/scaffold.py)."""
    missing = [
        app.name
        for app in django_apps.get_app_configs()
        if app.name.startswith("apps.")
        and not (Path(app.path) / "management" / "commands" / "__init__.py").exists()
    ]
    assert missing == [], f"add management/commands/__init__.py to: {missing}"


def test_command_index_puts_this_project_first() -> None:
    index = cli.command_index()
    names = [command.name for command in index]
    assert {"hello_world", "tui", "migrate"} <= set(names)

    groups = [command.group for command in index]
    assert groups == sorted(groups, key=cli.GROUPS.index)  # project, then django, then the rest

    hello = next(command for command in index if command.name == "hello_world")
    assert (hello.app, hello.group, hello.loaded) == ("apps.core", "project", True)
    assert hello.help.startswith("Greet NAME")


def test_a_command_that_does_not_import_is_listed_with_its_error() -> None:
    """The one tool that can tell you why a command is broken must not be the one that hides
    it: an unimportable module is a row, not a crash."""
    broken = cli.describe("no_such_command", "apps.core")

    assert broken.loaded is False
    assert "ModuleNotFoundError" in broken.help


def test_fuzzy_score_ranks_an_abbreviation_at_the_top() -> None:
    assert cli.fuzzy_score("", "anything") == 0
    assert cli.fuzzy_score("zzz", "hello_world") is None

    ranked = sorted(
        ["shell_as", "showmigrations", "hello_world"],
        key=lambda name: -(cli.fuzzy_score("hw", name) or 0),
    )
    assert ranked[0] == "hello_world"


def test_search_matches_names_loosely_and_descriptions_tightly() -> None:
    commands = [
        cli.CommandInfo("rls_sync", "apps.core", "Create/refresh the RLS policies."),
        cli.CommandInfo("hello_world", "apps.core", "Greet NAME and show a few facts."),
    ]

    assert [c.name for c in cli.search(commands, "rls")] == ["rls_sync"]
    assert cli.search(commands, "") == commands

    # The descriptions are searched only when asked (ctrl+f in the UI, -d on the command line).
    assert cli.search(commands, "polic") == []
    assert [c.name for c in cli.search(commands, "polic", descriptions=True)] == ["rls_sync"]
    # ...and then still tightly: six letters scattered across a sentence are not a match, or
    # every query would match everything.
    assert cli.search(commands, "tenant", descriptions=True) == []


def test_group_by_app_keeps_the_order_it_is_given() -> None:
    """The list is grouped under the app that ships each command, best-matching group first."""
    commands = [
        cli.CommandInfo("migrate", "django.core", "..."),
        cli.CommandInfo("backup", "apps.core", "..."),
        cli.CommandInfo("check", "django.core", "..."),
    ]

    assert [(app, [c.name for c in group]) for app, group in cli.group_by_app(commands)] == [
        ("django.core", ["migrate", "check"]),
        ("apps.core", ["backup"]),
    ]


def test_tui_lists_every_command() -> None:
    result = runner.invoke(tui.command, ["--list"])

    assert result.exit_code == 0
    assert "project commands" in result.output
    assert "hello_world" in result.output
    assert "migrate" in result.output


def test_tui_filters_by_query() -> None:
    result = runner.invoke(tui.command, ["--list", "tenant"])

    assert result.exit_code == 0
    assert "pull_tenant" in result.output
    assert "hello_world" not in result.output

    assert "no command matches" in runner.invoke(tui.command, ["--list", "zzzz"]).output


def test_full_help_is_the_commands_own_help() -> None:
    """Captured from the command itself, so it cannot drift from the real options."""
    text = cli.full_help("hello_world")

    assert "Usage: manage.py hello_world" in text
    assert "--shout" in text


def test_the_query_is_also_the_command_line() -> None:
    """Arguments start only once the first word names a command; until then every word is
    part of the search, so a two-word query narrows the list instead of breaking it."""
    names = {"hello_world", "shell"}

    assert tui.split_query("hello_world dx --shout", names) == ("hello_world", ["dx", "--shout"])
    assert tui.split_query("  hello_world  ", names) == ("hello_world", [])
    assert tui.split_query("del ten", names) == ("del ten", [])
    assert tui.split_query("", names) == ("", [])
    assert tui.split_query('hello_world "unbalanced', names) == ("hello_world", [])  # still typing


def test_search_narrows_as_you_add_words() -> None:
    """Fuzzy in fzf's sense: every term has to match, in any order."""
    index = cli.command_index()

    assert [c.name for c in cli.search(index, "del ten")][0] == "delete_tenant"
    assert [c.name for c in cli.search(index, "ten del")][0] == "delete_tenant"
    assert cli.search(index, "del ten zzz") == []


def test_running_a_command_starts_a_child_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """In this process it would raise `SynchronousOnlyOperation`: the explorer is an event loop
    and Django refuses synchronous database access from one."""
    started: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        started.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert tui.run_command("migrate", ["--noinput"]) == 0
    assert started == [[sys.executable, str(tui.MANAGE), "migrate", "--noinput"]]


def test_newcommand_refuses_an_app_that_does_not_exist() -> None:
    """The app is asked for when `--app` is left out; a wrong one is a usage error, not a file
    written somewhere surprising."""
    result = runner.invoke(newcommand.command, ["purge_old", "--app", "nope"])

    assert result.exit_code == 2
    assert "unknown app" in result.output
    assert "core" in result.output  # ...and it says which apps there are


# --- The usage log (apps/core/usage.py) --------------------------------------------------------


@pytest.mark.django_db
def test_every_invocation_is_recorded() -> None:
    """`manage.py` records the run before the command starts (see manage.py)."""
    usage.record_run(["pull_tenant", "alice", "--no-scrub"])

    run = usage.CommandRun.objects.get()
    assert (run.name, run.arguments) == ("pull_tenant", "alice --no-scrub")
    assert str(run) == "manage.py pull_tenant alice --no-scrub"


@pytest.mark.django_db
def test_the_explorer_itself_is_not_recorded() -> None:
    """It reads this log; writing to it would rearrange the list it is showing you."""
    usage.record_run(["tui", "backup"])
    usage.record_run([])  # bare `manage.py`
    usage.record_run(["--version"])

    assert usage.CommandRun.objects.count() == 0


@pytest.mark.django_db
def test_recording_a_run_never_breaks_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """No table yet (the first `migrate`), no database at all — the command still runs."""

    def boom(**kwargs: object) -> None:
        raise DatabaseError("no such table")

    monkeypatch.setattr(usage.CommandRun.objects, "create", boom)

    usage.record_run(["migrate"])  # does not raise


@pytest.mark.django_db
def test_recent_runs_names_each_command_once_newest_first() -> None:
    for argv in (["backup"], ["hello_world"], ["backup", "--list"]):
        usage.record_run(argv)

    assert usage.recent_runs() == ["backup", "hello_world"]
    assert usage.recent_runs(limit=1) == ["backup"]


def test_search_prefers_the_command_you_ran_last() -> None:
    """Recency breaks ties: two equally good matches, the one you used last comes first."""
    commands = [
        cli.CommandInfo("backup", "apps.core", "..."),
        cli.CommandInfo("backend", "apps.core", "..."),
    ]

    assert [c.name for c in cli.search(commands, "back")] == ["backup", "backend"]
    assert [c.name for c in cli.search(commands, "back", recent=["backend"])] == [
        "backend",
        "backup",
    ]


def test_tui_takes_a_multi_word_query() -> None:
    result = runner.invoke(tui.command, ["--list", "delete", "tenant"])

    assert result.exit_code == 0
    assert "delete_tenant" in result.output


def test_deleteapp_refuses_infrastructure_and_unknown_apps() -> None:
    """The two mistakes worth making impossible: `deleteapp core`, and a typo that would have
    matched nothing but still unregistered something."""
    infrastructure = runner.invoke(deleteapp.command, ["core"])
    unknown = runner.invoke(deleteapp.command, ["nope"])

    assert infrastructure.exit_code == 1
    assert "not a feature app" in infrastructure.output
    assert unknown.exit_code == 1
    assert "no app named apps.nope" in unknown.output
    assert "datasets" in unknown.output  # ...and it says which apps there are
