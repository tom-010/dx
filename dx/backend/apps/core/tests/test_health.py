"""Liveness (`/api/health`) and readiness (`/api/ready`) probes."""

import pytest
from django.test import Client

from apps.core import health


def test_health_is_a_plain_liveness_probe(client: Client) -> None:
    # No `django_db` marker: pytest-django blocks database access, so this proves the
    # liveness probe never touches the database.
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_ready_runs_every_check(client: Client) -> None:
    response = client.get("/api/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert [c["name"] for c in body["checks"]] == [
        "database",
        "migrations",
        "rls",
        "celery",
        "storage:default",
        "storage:backups",
    ]
    assert all(c["ok"] for c in body["checks"])
    assert body["checks"][3]["detail"] == "eager mode, no broker"  # tests run eagerly
    assert body["checks"][2]["detail"].endswith("role app_user")


@pytest.mark.django_db
def test_ready_is_503_when_a_check_fails(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        health, "check_broker", lambda: health.Check("celery", False, "connection refused")
    )

    response = client.get("/api/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert {"name": "celery", "ok": False, "detail": "connection refused"} in body["checks"]
