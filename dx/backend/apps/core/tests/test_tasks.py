"""Sample Celery tasks: run eagerly in tests (see conftest.py), API round trip included."""

import json
from typing import cast

import pytest
from django.http import StreamingHttpResponse
from django.test import Client

from apps.accounts.models import User
from apps.core import services, tasks
from apps.datasets.services import create_dataset

pytestmark = pytest.mark.django_db


def test_services_are_plain_functions() -> None:
    seen: list[tuple[int, int]] = []

    assert services.add(2, 3) == 5
    assert services.count_to(3, 0, lambda current, total: seen.append((current, total))) == 3
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_run_add_returns_the_result_inline(auth_client: Client) -> None:
    response = auth_client.post("/api/tasks/add", {"a": 2, "b": 3}, content_type="application/json")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "SUCCESS"
    assert body["ready"] is True
    assert body["result"] == 5
    assert body["error"] is None

    # The stored result can be polled afterwards.
    polled = auth_client.get(f"/api/tasks/{body['id']}")
    assert polled.status_code == 200
    assert polled.json()["result"] == 5


def test_run_count_validates_and_runs(auth_client: Client) -> None:
    too_slow = auth_client.post(
        "/api/tasks/count", {"n": 5, "delay": 60}, content_type="application/json"
    )
    assert too_slow.status_code == 422

    response = auth_client.post(
        "/api/tasks/count", {"n": 3, "delay": 0}, content_type="application/json"
    )
    assert response.json()["result"] == 3


def test_dataset_summary_task_reads_the_database(auth_client: Client, user: User) -> None:
    create_dataset(user, name="a", row_count=10)
    create_dataset(user, name="b", row_count=5)

    response = auth_client.post("/api/tasks/dataset-summary")

    assert response.json()["result"] == {"datasets": 2, "rows": 15}


def test_failing_task_is_reported_not_raised(auth_client: Client) -> None:
    response = auth_client.post("/api/tasks/fail")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "FAILURE"
    assert body["ready"] is True
    assert body["error"] == "DemoFailure: This task fails on purpose"


def test_unknown_task_id_is_pending(auth_client: Client) -> None:
    response = auth_client.get("/api/tasks/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 200
    body = response.json()
    stream_url = body.pop("stream_url")
    assert body == {
        "id": "00000000-0000-0000-0000-000000000000",
        "state": "PENDING",
        "ready": False,
        "result": None,
        "error": None,
        "progress": None,
    }
    assert stream_url.startswith("/api/tasks/00000000-0000-0000-0000-000000000000/events?sig=")


def test_progress_is_exposed_from_task_meta() -> None:
    class FakeResult:
        id = "x"
        state = tasks.PROGRESS
        info = {"current": 2, "total": 5}

        def ready(self) -> bool:
            return False

    status = tasks.status_of(FakeResult())  # type: ignore[arg-type]  # duck-typed AsyncResult

    assert status.progress == (2, 5)
    assert status.ready is False


# --- Live status stream (SSE) ------------------------------------------------------------------


def _status(state: tasks.TaskState, **extra: object) -> tasks.TaskStatus:
    return tasks.TaskStatus(id="x", state=state, ready=state == "SUCCESS", **extra)  # type: ignore[arg-type]  # test helper


def test_watch_yields_changes_only_and_stops_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = iter(
        [
            _status("PENDING"),
            _status("PENDING"),  # unchanged: not sent again
            _status("PROGRESS", progress=(1, 2)),
            _status("PROGRESS", progress=(2, 2)),
            _status("SUCCESS", result=2),
            _status("SUCCESS", result=2),  # never reached: the stream ends when ready
        ]
    )
    monkeypatch.setattr(tasks, "status_by_id", lambda task_id: next(lookups))

    seen = list(tasks.watch("x", interval=0))

    assert [(s.state, s.progress, s.result) for s in seen] == [
        ("PENDING", None, None),
        ("PROGRESS", (1, 2), None),
        ("PROGRESS", (2, 2), None),
        ("SUCCESS", None, 2),
    ]


def test_watch_repeats_unchanged_status_as_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "status_by_id", lambda task_id: _status("STARTED"))

    seen = list(tasks.watch("x", interval=0, heartbeat=0, timeout=0))

    assert [s.state for s in seen] == ["STARTED"]  # sent once, then the timeout ends the stream


def test_watch_gives_up_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "status_by_id", lambda task_id: _status("PENDING"))

    assert len(list(tasks.watch("x", interval=0, timeout=0))) == 1


def test_stream_signature_is_bound_to_the_task_id() -> None:
    signature = tasks.sign_stream("abc")

    assert tasks.verify_stream("abc", signature) is True
    assert tasks.verify_stream("abd", signature) is False
    assert tasks.verify_stream("abc", "nope") is False


def test_stream_sends_the_final_status_and_closes(auth_client: Client, client: Client) -> None:
    started = auth_client.post(
        "/api/tasks/count", {"n": 2, "delay": 0}, content_type="application/json"
    ).json()

    # The signed link works without a bearer token (EventSource cannot send one).
    response = client.get(started["stream_url"])

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    assert response["Cache-Control"] == "no-cache"
    # The test client types every response as its own class; this one really is streaming.
    streaming = cast("StreamingHttpResponse", response)
    body = streaming.getvalue()
    events = [e for e in body.decode().split("\n\n") if e]
    assert len(events) == 1  # eager: already SUCCESS on the first lookup, then the stream ends
    kind, data = events[0].split("\n")
    assert kind == "event: status"
    assert data.startswith("data: ")
    assert json.loads(data.removeprefix("data: ")) == started


def test_stream_rejects_unsigned_or_foreign_links(client: Client) -> None:
    other = tasks.sign_stream("other-task")

    assert client.get("/api/tasks/some-task/events").status_code == 422  # sig missing
    assert client.get("/api/tasks/some-task/events?sig=nope").status_code == 403
    assert client.get(f"/api/tasks/some-task/events?sig={other}").status_code == 403
