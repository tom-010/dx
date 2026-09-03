"""The HTTP request behind a write (`apps/core/request_record.py`): recorded on the first write
of a request, stamped on every version and edge it produced, redacted and bounded.

The three things worth pinning: that a write through the API can be traced back to what the
client sent; that nothing is recorded where there was no request or no write; and that what is
recorded never includes a credential or a file.
"""

import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.models import User
from apps.core import lineage, request_record
from apps.core.db import tenant_context
from apps.core.request_record import BODY_LIMIT, REDACTED, RequestRecord
from apps.core.testing import acting_as
from apps.datasets.models import Dataset
from apps.documents.models import Document
from apps.documents.testing import uploadable

pytestmark = pytest.mark.django_db


def post_json(client: Client, path: str, payload: object) -> Client:
    response = client.post(path, data=json.dumps(payload), content_type="application/json")
    assert response.status_code in (200, 201), response.content
    return client


def dataset_payload(name: str) -> dict[str, object]:
    return {"name": name, "description": "", "row_count": 0, "options": {}, "tags": []}


def test_a_write_through_the_api_records_the_request_and_stamps_the_version(
    auth_client: Client, user: User
) -> None:
    response = auth_client.post(
        "/api/datasets?source=test",
        data=json.dumps(dataset_payload("from the api")),
        content_type="application/json",
    )
    assert response.status_code == 201

    with tenant_context(user.pk):
        (record,) = RequestRecord.objects.all()
        dataset = Dataset.objects.get(pk=response.json()["id"])
        version = dataset.history()[-1]

        assert (record.method, record.path) == ("POST", "/api/datasets")
        assert record.sent_query == {"source": "test"}
        assert record.sent_body == dataset_payload("from the api")
        assert record.body_status == "json"
        assert record.body_size == len(json.dumps(dataset_payload("from the api")))
        assert record.content_type == "application/json"
        assert record.request_id  # django-structlog's, the join key into the logs
        assert version.request_id == record.pk


def test_credentials_are_present_but_redacted(auth_client: Client, user: User) -> None:
    """A backup with live bearer tokens in it is a bad artefact whoever owns the row."""
    post_json(auth_client, "/api/datasets", dataset_payload("x"))

    with tenant_context(user.pk):
        record = RequestRecord.objects.get()

    assert record.sent_headers["Authorization"] == REDACTED
    assert "Bearer" not in json.dumps(record.sent_headers)
    assert record.sent_headers["Content-Type"] == "application/json"  # the rest is kept


def test_one_request_is_one_record_however_much_it_writes(auth_client: Client, user: User) -> None:
    """A dataset with tags is several rows in one request: one record, stamped on all of them."""
    payload = {**dataset_payload("tagged"), "tags": ["finance", "q3"]}
    post_json(auth_client, "/api/datasets", payload)

    with tenant_context(user.pk):
        (record,) = RequestRecord.objects.all()
        dataset = Dataset.objects.get(name="tagged")
        stamped = [dataset.history()[-1].request_id]
        stamped += [link.history()[-1].request_id for link in dataset.tag_links.all()]
        stamped += [link.tag.history()[-1].request_id for link in dataset.tag_links.all()]

    assert len(stamped) == 5  # the dataset, two links, two tags
    assert set(stamped) == {record.pk}


def test_a_request_that_writes_nothing_leaves_no_record(auth_client: Client, user: User) -> None:
    assert auth_client.get("/api/datasets").status_code == 200
    assert auth_client.get(f"/api/datasets/{uuid.uuid4()}").status_code == 404
    bad = auth_client.post("/api/datasets", data="{}", content_type="application/json")
    assert bad.status_code == 422

    with tenant_context(user.pk):
        assert RequestRecord.objects.count() == 0


def test_a_write_outside_any_request_records_nothing(user: User) -> None:
    """A command, a task, a shell: nothing to record, and the version says so with NULL."""
    with acting_as(user):
        dataset = Dataset.create(operation=None, sources=[], name="from a shell")

        assert dataset.history()[-1].request_id is None
        assert RequestRecord.objects.count() == 0
        assert request_record.current_request_id() is None


def test_an_upload_records_the_request_but_not_the_file(auth_client: Client, user: User) -> None:
    """Django raises on `request.body` once the view has read the files — and a file would not
    belong here if it did not. The request itself is still recorded."""
    upload = SimpleUploadedFile("rows.csv", b"a,b\n1,2\n", content_type="text/csv")
    # A CSV is not a type the upload offers; this test is about the record, not the picker.
    with uploadable("text/csv"):
        response = auth_client.post("/api/documents/upload", data={"files": [upload]})
    assert response.status_code == 201, response.content

    with tenant_context(user.pk):
        record = RequestRecord.objects.get()
        document = Document.objects.get()
        stamped = document.history()[-1].request_id  # event rows are tenant data: read in context

    assert record.sent_body is None
    assert record.body_status == "none"  # not application/json, so never attempted
    assert record.content_type.startswith("multipart/form-data")
    assert stamped == record.pk


def test_a_body_over_the_limit_is_recorded_by_size_only(auth_client: Client, user: User) -> None:
    """Truncated JSON is not JSON, so it is all or nothing — the size says how much there was."""
    payload = {**dataset_payload("big"), "description": "x" * (BODY_LIMIT + 1)}
    response = auth_client.post(
        "/api/datasets", data=json.dumps(payload), content_type="application/json"
    )
    assert response.status_code == 201, response.content

    with tenant_context(user.pk):
        record = RequestRecord.objects.get()

    assert record.sent_body is None
    assert record.body_status == "too-large"
    assert record.body_size > BODY_LIMIT


def test_a_lineage_edge_carries_the_request_too(auth_client: Client, user: User) -> None:
    """The import endpoint is the app's one derivation: its edge names the request as well."""
    upload = SimpleUploadedFile("rows.csv", b"a,b\n1,2\n3,4\n", content_type="text/csv")
    with uploadable("text/csv"):
        uploaded = auth_client.post("/api/documents/upload", data={"files": [upload]})
    document_id = uploaded.json()[0]["id"]
    imported = auth_client.post(
        "/api/datasets/import-document",
        data=json.dumps({"document_id": document_id}),
        content_type="application/json",
    )
    assert imported.status_code == 201, imported.content

    with tenant_context(user.pk):
        dataset = Dataset.objects.get(pk=imported.json()["id"])
        (edge,) = lineage.all_sources_of(dataset)
        records = {r.pk: r for r in RequestRecord.objects.all()}

    assert edge.request in records
    assert records[edge.request].path == "/api/datasets/import-document"
    assert len(records) == 2  # the upload and the import, one each


def test_the_record_is_the_tenants_and_erased_with_them(
    auth_client: Client, user: User, other_user: User
) -> None:
    """Owned, not shared: another tenant cannot see it, and the erasure walks it."""
    from apps.core import rls

    post_json(auth_client, "/api/datasets", dataset_payload("mine"))

    with tenant_context(other_user.pk):
        assert RequestRecord.objects.count() == 0
    assert RequestRecord in rls.isolated_models()


def test_export_scrubs_what_the_client_sent() -> None:
    """A request body is PII by definition; `pull_tenant` blanks it and the headers."""
    from apps.core import scrub

    record = RequestRecord.example()
    scrub.scrub(record, 1)  # in place; the typed handle is `record`

    assert record.sent_body is None
    assert record.sent_query == {}
    assert record.sent_headers == {"Content-Type": "application/json"}
    assert (record.method, record.path) == ("PATCH", "/api/datasets/01a0-example")
