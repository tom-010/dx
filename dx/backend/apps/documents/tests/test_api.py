import uuid
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
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


def test_upload_rejects_empty_file(auth_client: Client) -> None:
    response = auth_client.post("/api/documents/upload", {"files": [_pdf("empty.pdf", b"")]})

    assert response.status_code == 422
    assert response.json() == {"detail": "empty.pdf is empty"}
    assert Document.objects.count() == 0


def test_upload_without_files_is_a_validation_error(auth_client: Client) -> None:
    response = auth_client.post("/api/documents/upload", {})

    assert response.status_code == 422


def test_download_streams_the_file(client: Client, user: User) -> None:
    (document,) = services.store_documents(user, [_pdf("download.pdf", b"payload")])
    document_id = DocumentId(document.pk)

    # A signed link works without a bearer token (plain <a href> in the SPA).
    response = client.get(
        f"/api/documents/{document_id}/download?sig={services.sign_download(document_id)}"
    )

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"payload"  # type: ignore[attr-defined]
    assert 'filename="download.pdf"' in response["Content-Disposition"]


def test_download_rejects_unsigned_or_foreign_links(client: Client, user: User) -> None:
    (document,) = services.store_documents(user, [_pdf("secret.pdf")])
    other_signature = services.sign_download(DocumentId(uuid.uuid7()))

    assert client.get(f"/api/documents/{document.pk}/download").status_code == 422  # sig missing
    assert client.get(f"/api/documents/{document.pk}/download?sig=nope").status_code == 403
    assert (
        client.get(f"/api/documents/{document.pk}/download?sig={other_signature}").status_code
        == 403
    )


def test_delete_removes_file_and_row(auth_client: Client, user: User, media_root: Path) -> None:
    (document,) = services.store_documents(user, [_pdf("gone.pdf")])
    assert len(list(media_root.rglob("*.pdf"))) == 1

    assert auth_client.delete(f"/api/documents/{document.pk}").status_code == 204
    assert auth_client.get(f"/api/documents/{document.pk}").status_code == 404
    assert len(list(media_root.rglob("*.pdf"))) == 0


def test_service_validates_size_limit() -> None:
    big = SimpleUploadedFile("big.bin", b"x", content_type="application/octet-stream")
    big.size = services.MAX_DOCUMENT_SIZE + 1

    with pytest.raises(services.InvalidDocument, match="exceeds"):
        services.validate_upload([big])


def test_service_raises_for_unknown_id(user: User) -> None:
    with pytest.raises(services.DocumentNotFound):
        services.get_document(user, DocumentId(uuid.uuid7()))
