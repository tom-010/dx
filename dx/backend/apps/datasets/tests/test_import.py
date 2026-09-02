"""Importing a dataset from an uploaded document — the project's one derivation, and therefore
the one place that writes lineage edges.

The point of recording the edge against the document's *version* rather than the document row:
when the document is later renamed or replaced, the dataset's history still says what it was
actually built from, and `stale_derivations` finds the datasets that need rebuilding.
"""

from typing import Protocol, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.models import User
from apps.core import lineage
from apps.core.history import EventRow
from apps.core.testing import acting_as
from apps.datasets.api import import_dataset_for
from apps.datasets.models import Dataset, DatasetOptions
from apps.documents.api import store_documents
from apps.documents.models import Document, DocumentId

pytestmark = pytest.mark.django_db

CSV = b"name,amount\nalice,10\nbob,20\n"


class DocumentEventRow(EventRow, Protocol):
    """A `DocumentEvent` row — the generated event models are invisible to a type checker, so
    the fields we read are spelled out (`apps/core/history.py::EventRow`)."""

    title: str


def _upload(user: User, name: str = "orders.csv", body: bytes = CSV) -> Document:
    (document,) = store_documents(user, [SimpleUploadedFile(name, body, content_type="text/csv")])
    return document


def test_import_counts_rows_and_records_where_they_came_from(
    auth_client: Client, user: User
) -> None:
    with acting_as(user):
        document = _upload(user)

    created = auth_client.post(
        "/api/datasets/import-document",
        {"document_id": str(document.pk), "tags": ["imported"]},
        content_type="application/json",
    )

    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "orders"  # the file name without its suffix
    assert body["row_count"] == 2  # two data rows; the header does not count
    assert body["description"] == "Imported from orders.csv"
    assert body["tags"] == ["imported"]

    with acting_as(user):
        dataset = Dataset.objects.get(pk=body["id"])
        (edge,) = lineage.sources_of(dataset)
        assert edge.source_obj_id == document.pk
        assert edge.source_version == 1


def test_the_edge_keeps_pointing_at_the_version_it_read(user: User) -> None:
    with acting_as(user):
        document = _upload(user)
        dataset = import_dataset_for(user, DocumentId(document.pk))

        document.title = "renamed.csv"
        document.save(operation=None, sources=[])
        document.refresh_from_db()

        (edge,) = lineage.sources_of(dataset)
        assert document.version == 2
        assert edge.source_version == 1
        was = cast(DocumentEventRow, edge.resolve_source())
        assert was.title == "orders.csv"  # not "renamed.csv"
        # The question the whole design exists to answer: what needs rebuilding now?
        assert [e.pk for e in lineage.stale_derivations(document)] == [edge.pk]


def test_nothing_is_stale_until_the_source_moves(user: User) -> None:
    with acting_as(user):
        document = _upload(user)
        import_dataset_for(user, DocumentId(document.pk))

        assert lineage.derived_from(document).count() == 1
        assert lineage.stale_derivations(document).count() == 0


def test_header_option_changes_the_row_count(user: User) -> None:
    with acting_as(user):
        document = _upload(user)
        dataset = import_dataset_for(
            user, DocumentId(document.pk), options=DatasetOptions(has_header=False)
        )
    assert dataset.row_count == 3


def test_a_final_line_without_a_newline_still_counts(user: User) -> None:
    with acting_as(user):
        document = _upload(user, body=b"name\nalice\nbob")
        dataset = import_dataset_for(user, DocumentId(document.pk))
    assert dataset.row_count == 2


def test_a_header_only_file_imports_as_zero_rows(user: User) -> None:
    """Not an empty file — uploads reject those (`documents.api.validate_upload`)."""
    with acting_as(user):
        document = _upload(user, body=b"name,amount\n")
        dataset = import_dataset_for(user, DocumentId(document.pk))
    assert dataset.row_count == 0


def test_a_non_text_document_is_refused(auth_client: Client, user: User) -> None:
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile("scan.pdf", b"%PDF-1.4", content_type="application/pdf")]
        )

    response = auth_client.post(
        "/api/datasets/import-document",
        {"document_id": str(document.pk)},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "not delimited text" in response.json()["detail"]


def test_another_tenants_document_is_a_404(
    auth_client: Client, user: User, other_user: User
) -> None:
    with acting_as(other_user):
        theirs = _upload(other_user)

    response = auth_client.post(
        "/api/datasets/import-document",
        {"document_id": str(theirs.pk)},
        content_type="application/json",
    )

    assert response.status_code == 404
    with acting_as(user):
        assert Dataset.objects.count() == 0
        assert lineage.Lineage.objects.count() == 0


def test_the_import_shows_up_in_the_datasets_history(auth_client: Client, user: User) -> None:
    with acting_as(user):
        document = _upload(user)
        dataset = import_dataset_for(user, DocumentId(document.pk))

    body = auth_client.get(f"/api/history/dataset/{dataset.pk}").json()

    (source,) = body["groups"][0]["revisions"][0]["sources"]
    assert (source["model"], source["label"], source["version"]) == ("Document", "orders.csv", 1)
    assert source["is_stale"] is False
