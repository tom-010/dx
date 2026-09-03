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


def read(auth_client: Client, ids: list[str]) -> dict[str, Any]:
    response = auth_client.post(
        "/api/notifications/read", {"ids": ids}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def test_the_bell_counts_what_is_unread(auth_client: Client) -> None:
    create_dataset(auth_client, "A")
    create_dataset(auth_client, "B")
    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 2}

    first = auth_client.get("/api/notifications?unread=true").json()["items"][0]["id"]
    assert read(auth_client, [first]) == {"read": 1}

    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 1}
    assert auth_client.get("/api/notifications?unread=true").json()["count"] == 1
    assert auth_client.get("/api/notifications").json()["count"] == 2  # still in the list


def test_reading_a_whole_page_at_once(auth_client: Client) -> None:
    """What the inbox does on arrival: mark everything it is showing, in one request."""
    create_dataset(auth_client, "A")
    create_dataset(auth_client, "B")
    shown = [item["id"] for item in auth_client.get("/api/notifications").json()["items"]]

    assert read(auth_client, shown) == {"read": 2}
    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 0}
    assert read(auth_client, shown) == {"read": 0}  # idempotent


def test_pagination_leaves_the_pages_it_did_not_show_unread(auth_client: Client) -> None:
    for name in ("A", "B", "C"):
        create_dataset(auth_client, name)

    page_one = auth_client.get("/api/notifications?page=1&page_size=2").json()
    assert page_one["count"] == 3
    assert len(page_one["items"]) == 2

    assert read(auth_client, [item["id"] for item in page_one["items"]]) == {"read": 2}
    assert auth_client.get("/api/notifications/unread-count").json() == {"unread": 1}

    page_two = auth_client.get("/api/notifications?page=2&page_size=2").json()
    assert len(page_two["items"]) == 1
    assert page_two["items"][0]["read_at"] is None


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
    assert read(other, [notification_id]) == {"read": 0}
    # ...and the owner's message is untouched.
    assert auth_client.get("/api/notifications").json()["items"][0]["read_at"] is None


def test_reading_an_id_that_does_not_exist_changes_nothing(auth_client: Client) -> None:
    assert read(auth_client, [str(uuid.uuid7())]) == {"read": 0}


def test_the_inbox_needs_a_token(client: Client) -> None:
    assert client.get("/api/notifications").status_code == 401
