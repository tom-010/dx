import uuid
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import services
from apps.documents.models import Document, DocumentId

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _pdf(name: str = "report.pdf", content: bytes = b"%PDF-1.4 demo") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def test_upload_multiple_files(auth_client: Client, media_root: Path) -> None:
    response = auth_client.post(
        "/api/documents/upload",
        {"files": [_pdf("a.pdf"), _pdf("b.pdf", b"second")]},
    )

    assert response.status_code == 201
    body = response.json()
    assert [d["name"] for d in body] == ["a.pdf", "b.pdf"]
    assert [d["size"] for d in body] == [len(b"%PDF-1.4 demo"), len(b"second")]
    assert body[0]["content_type"] == "application/pdf"
    assert body[0]["download_url"].startswith(f"/api/documents/{body[0]['id']}/download?sig=")
    assert len(list(media_root.rglob("*.pdf"))) == 2

    listed = auth_client.get("/api/documents")
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert {d["name"] for d in listed.json()["items"]} == {"a.pdf", "b.pdf"}


def test_upload_rejects_empty_file(auth_client: Client, user: User) -> None:
    response = auth_client.post("/api/documents/upload", {"files": [_pdf("empty.pdf", b"")]})

    assert response.status_code == 422
    assert response.json() == {"detail": "empty.pdf is empty"}
    with acting_as(user):
        assert Document.objects.count() == 0


def test_upload_without_files_is_a_validation_error(auth_client: Client) -> None:
    response = auth_client.post("/api/documents/upload", {})

    assert response.status_code == 422


def test_download_streams_the_file(client: Client, user: User) -> None:
    with acting_as(user):
        (document,) = services.store_documents(user, [_pdf("download.pdf", b"payload")])

    # A signed link works without a bearer token (plain <a href> in the SPA).
    response = client.get(
        f"/api/documents/{document.pk}/download?sig={services.sign_download(document)}"
    )

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"payload"  # type: ignore[attr-defined]
    assert 'filename="download.pdf"' in response["Content-Disposition"]


def test_download_rejects_unsigned_or_foreign_links(
    client: Client, user: User, other_user: User
) -> None:
    with acting_as(user):
        (document,) = services.store_documents(user, [_pdf("secret.pdf")])
    other_document = services.sign_download(Document(id=uuid.uuid7(), owner_id=user.pk))
    # Same document id, but signed for another owner: the link is valid, the row is not theirs.
    other_owner = services.sign_download(Document(id=document.pk, owner_id=other_user.pk))
    url = f"/api/documents/{document.pk}/download"

    assert client.get(url).status_code == 422  # sig missing
    assert client.get(f"{url}?sig=nope").status_code == 403
    assert client.get(f"{url}?sig={other_document}").status_code == 403
    assert client.get(f"{url}?sig={other_owner}").status_code == 404


def test_a_still_valid_link_stops_working_once_the_document_is_deleted(
    client: Client, auth_client: Client, user: User
) -> None:
    """Soft delete drops the link even though the bytes are still in the store: the signature
    has not expired, but the row it names is gone from the application's point of view."""
    with acting_as(user):
        (document,) = services.store_documents(user, [_pdf("gone.pdf", b"payload")])
        url = f"/api/documents/{document.pk}/download?sig={services.sign_download(document)}"
    working = client.get(url)
    assert working.status_code == 200
    # Consume it rather than close(): closing a test-client response fires `request_finished`,
    # which drops the database connection the rest of this test still needs.
    assert b"".join(working.streaming_content) == b"payload"  # type: ignore[attr-defined]

    assert auth_client.delete(f"/api/documents/{document.pk}").status_code == 204

    assert client.get(url).status_code == 404


def test_delete_hides_the_row_and_keeps_the_file(
    auth_client: Client, user: User, media_root: Path
) -> None:
    """Deletes are soft, so the stored object stays: earlier versions of the document still
    reference it (apps/documents/services.py::delete_document)."""
    with acting_as(user):
        (document,) = services.store_documents(user, [_pdf("gone.pdf")])
    assert len(list(media_root.rglob("*.pdf"))) == 1

    assert auth_client.delete(f"/api/documents/{document.pk}").status_code == 204
    assert auth_client.get(f"/api/documents/{document.pk}").status_code == 404
    assert auth_client.get("/api/documents").json()["items"] == []
    assert len(list(media_root.rglob("*.pdf"))) == 1

    with acting_as(user):
        assert Document.objects.filter(pk=document.pk).count() == 0
        gone = Document.all_objects.get(pk=document.pk)
    assert gone.deleted_at is not None
    assert gone.version == 2  # the soft delete is a version of the object


def test_service_validates_size_limit() -> None:
    big = SimpleUploadedFile("big.bin", b"x", content_type="application/octet-stream")
    big.size = services.MAX_DOCUMENT_SIZE + 1

    with pytest.raises(services.InvalidDocument, match="exceeds"):
        services.validate_upload([big])


def test_service_raises_for_unknown_id(user: User) -> None:
    with acting_as(user), pytest.raises(services.DocumentNotFound):
        services.get_document(user, DocumentId(uuid.uuid7()))
