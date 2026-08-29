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
from pytest_django.fixtures import Settings
from watchfiles import Change

from apps.accounts.models import User
from apps.core import worker_reload
from apps.core.management.commands import backup, ensure_bucket, hello_world, restore
from apps.core.storage import BucketStatus
from apps.core.storage import ensure_bucket as ensure_bucket_fn
from apps.datasets.models import Dataset
from apps.datasets.services import create_dataset

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
def test_backup_and_restore_commands(user: User, backup_dir: Path) -> None:
    dataset = create_dataset(user, name="keep me")

    created = runner.invoke(backup.command, [])
    assert created.exit_code == 0, created.output
    assert "wrote dx-" in created.output
    (dump,) = backup_dir.glob("dx-*.json.gz")

    listed = runner.invoke(backup.command, ["--list"])
    assert dump.name in listed.output

    Dataset.objects.all().delete()
    restored = runner.invoke(restore.command, ["--latest", "--yes"])
    assert restored.exit_code == 0, restored.output
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
    changes = [{(modified, "apps/core/services.py")}, {(modified, "config/settings.py")}]

    code = worker_reload.run_with_reload(spawn, changes, stop_timeout=5, echo=messages.append)

    assert len(started) == 3  # initial + one per change set
    assert all(p.returncode == -signal.SIGTERM for p in started)
    assert messages == [
        "apps/core/services.py changed, restarting worker",
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
