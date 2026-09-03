"""The inbox over HTTP: listing, the bell's count, reading, and tenant isolation."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.datasets.notification_types import DATASET_CREATED

pytestmark = pytest.mark.django_db


def create_dataset(auth_client: Client, name: str, description: str = "") -> dict[str, Any]:
    response = auth_client.post(
        "/api/datasets",
        {"name": name, "description": description},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body: dict[str, Any] = response.json()
    return body


def test_creating_a_dataset_puts_a_message_in_the_inbox(auth_client: Client) -> None:
    dataset = create_dataset(auth_client, "Orders", "From the shop export")

    listed = auth_client.get("/api/notifications")
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    notification = body["items"][0]
    assert notification["notification_type"] == DATASET_CREATED
    assert notification["title"] == "New dataset: Orders"
    assert notification["description"] == "From the shop export"
    assert notification["read_at"] is None
    # The backend ships a reference, never a route: the SPA registry turns this into a link.
    assert notification["source"] == {"type": "datasets.dataset", "id": dataset["id"]}


def test_the_bell_counts_what_is_unread(auth_client: Client) -> None:
    create_dataset(auth_client, "A")
    create_dataset(auth_client, "B")
    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 2}

    unread_id = auth_client.get("/api/notifications?unread=true").json()["items"][0]["id"]
    read = auth_client.post(f"/api/notifications/{unread_id}/read")
    assert read.status_code == 200
    assert read.json()["read_at"] is not None

    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 1}
    assert auth_client.get("/api/notifications?unread=true").json()["count"] == 1
    assert auth_client.get("/api/notifications").json()["count"] == 2  # still in the list


def test_read_all(auth_client: Client) -> None:
    create_dataset(auth_client, "A")
    create_dataset(auth_client, "B")

    assert auth_client.post("/api/notifications/read").json() == {"read": 2}
    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 0}
    assert auth_client.post("/api/notifications/read").json() == {"read": 0}


def test_deleting_the_dataset_takes_its_message_with_it(auth_client: Client) -> None:
    dataset = create_dataset(auth_client, "Orders")
    assert auth_client.delete(f"/api/datasets/{dataset['id']}").status_code == 204

    assert auth_client.get("/api/notifications").json() == {"items": [], "count": 0}


def test_another_users_inbox_is_not_visible(
    auth_client: Client, other_user: User, client_for: Callable[[User], Client]
) -> None:
    create_dataset(auth_client, "Mine")
    notification_id = auth_client.get("/api/notifications").json()["items"][0]["id"]
    other = client_for(other_user)

    assert other.get("/api/notifications").json() == {"items": [], "count": 0}
    assert other.get("/api/notifications/unread-count").json() == {"unread": 0}
    assert other.post(f"/api/notifications/{notification_id}/read").status_code == 404
    # ...and the owner's message is untouched.
    assert auth_client.get("/api/notifications").json()["items"][0]["read_at"] is None


def test_reading_something_that_does_not_exist_is_a_404(auth_client: Client) -> None:
    assert auth_client.post(f"/api/notifications/{uuid.uuid7()}/read").status_code == 404


def test_the_inbox_needs_a_token(client: Client) -> None:
    assert client.get("/api/notifications").status_code == 401
