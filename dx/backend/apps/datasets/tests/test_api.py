import uuid

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.datasets import services
from apps.datasets.models import Dataset, DatasetId, DatasetOptions
from apps.datasets.schemas import DatasetPatch

pytestmark = pytest.mark.django_db


def test_create_and_list(auth_client: Client) -> None:
    created = auth_client.post(
        "/api/datasets",
        {"name": "Orders 2026", "description": "Import from ERP", "row_count": 120},
        content_type="application/json",
    )

    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Orders 2026"
    assert body["row_count"] == 120
    assert body["options"] == {"delimiter": ",", "has_header": True, "encoding": "utf-8"}
    assert uuid.UUID(body["id"]).version == 7
    assert "created" in body

    listed = auth_client.get("/api/datasets")
    assert listed.status_code == 200
    assert listed.json() == {"items": [body], "count": 1}


def test_list_is_paginated(auth_client: Client, user: User) -> None:
    for i in range(5):
        services.create_dataset(user, name=f"ds{i}")

    page = auth_client.get("/api/datasets?page=2&page_size=2").json()

    assert page["count"] == 5
    assert [d["name"] for d in page["items"]] == ["ds2", "ds1"]  # newest first


def test_create_validates_input(auth_client: Client) -> None:
    response = auth_client.post(
        "/api/datasets", {"name": "", "row_count": -1}, content_type="application/json"
    )

    assert response.status_code == 422


def test_create_rejects_unknown_fields(auth_client: Client) -> None:
    response = auth_client.post(
        "/api/datasets", {"name": "x", "rows": 3}, content_type="application/json"
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_options_are_a_typed_json_field(auth_client: Client, user: User) -> None:
    created = auth_client.post(
        "/api/datasets",
        {"name": "tsv", "options": {"delimiter": "\t", "has_header": False}},
        content_type="application/json",
    ).json()

    dataset = services.get_dataset(user, DatasetId(uuid.UUID(created["id"])))
    assert dataset.options == DatasetOptions(delimiter="\t", has_header=False)

    invalid = auth_client.post(
        "/api/datasets",
        {"name": "bad", "options": {"delimiter": ";;"}},  # max_length=1
        content_type="application/json",
    )
    assert invalid.status_code == 422


def test_get_put_patch_delete(auth_client: Client, user: User) -> None:
    dataset = services.create_dataset(user, name="Temp", description="d", row_count=1)
    url = f"/api/datasets/{dataset.pk}"

    assert auth_client.get(url).status_code == 200

    replaced = auth_client.put(url, {"name": "Full"}, content_type="application/json")
    assert replaced.status_code == 200
    assert (replaced.json()["name"], replaced.json()["description"]) == ("Full", "")
    assert replaced.json()["row_count"] == 0  # PUT: omitted fields fall back to defaults

    patched = auth_client.patch(url, {"row_count": 7}, content_type="application/json")
    assert patched.status_code == 200
    assert (patched.json()["name"], patched.json()["row_count"]) == ("Full", 7)

    assert auth_client.delete(url).status_code == 204
    assert auth_client.get(url).status_code == 404
    assert auth_client.get(url).json() == {"detail": "Dataset not found"}


def test_service_scopes_to_the_user(user: User, other_user: User) -> None:
    dataset = services.create_dataset(user, name="mine")

    with pytest.raises(services.DatasetNotFound):
        services.get_dataset(other_user, DatasetId(dataset.pk))
    with pytest.raises(services.DatasetNotFound):
        services.patch_dataset(other_user, DatasetId(dataset.pk), DatasetPatch(name="x"))
    with pytest.raises(services.DatasetNotFound):
        services.delete_dataset(other_user, DatasetId(dataset.pk))
    assert Dataset.objects.get(pk=dataset.pk).name == "mine"


def test_service_raises_for_unknown_id(user: User) -> None:
    with pytest.raises(services.DatasetNotFound):
        services.get_dataset(user, DatasetId(uuid.uuid7()))
