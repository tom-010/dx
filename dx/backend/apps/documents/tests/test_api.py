"""The HTTP contract of the documents API: upload, list, download, delete, and the snapshot
read through the facade (content, page, hit, search, re-extraction)."""

import uuid
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from ninja.errors import HttpError
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import snapshot, strategies
from apps.documents.api import (
    MAX_DOCUMENT_SIZE,
    get_document_for,
    mime_type_of,
    sign_download,
    store_documents,
    validate_upload,
)
from apps.documents.models import Blob, Document, DocumentContent, DocumentId
from apps.documents.tests.conftest import FakeStrategy, text_pdf, upload

pytestmark = pytest.mark.django_db


def _file(name: str = "report.fake", content: bytes = b"fake bytes") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/x-fake")


def test_upload_stores_files_and_queues_an_extraction(
    auth_client: Client, user: User, media_root: Path, fake: FakeStrategy
) -> None:
    response = auth_client.post(
        "/api/documents/upload", {"files": [_file("a.fake"), _file("b.fake", b"second")]}
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert [d["title"] for d in body] == ["a.fake", "b.fake"]
    assert [d["size"] for d in body] == [len(b"fake bytes"), len(b"second")]
    assert body[0]["mime_type"] == "application/x-fake"
    assert body[0]["status"] == "pending"  # the task runs when the request commits
    assert body[0]["page_count"] == 0
    assert body[0]["download_url"].startswith(f"/api/documents/{body[0]['id']}/download?sig=")
    assert len([p for p in media_root.rglob("*") if p.is_file()]) == 2

    listed = auth_client.get("/api/documents")
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert {d["title"] for d in listed.json()["items"]} == {"a.fake", "b.fake"}


def test_the_extraction_runs_once_the_upload_commits(
    auth_client: Client,
    user: User,
    fake: FakeStrategy,
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        created = auth_client.post("/api/documents/upload", {"files": [_file()]})
    document_id = created.json()[0]["id"]

    body = auth_client.get(f"/api/documents/{document_id}").json()
    assert body["status"] == "succeeded" and body["page_count"] == 2
    assert body["meta"] == {"title": "Annual report"}

    content = auth_client.get(f"/api/documents/{document_id}/content").json()
    assert content["status"] == "succeeded"
    assert content["extraction"]["extractor"] == "fake 1"
    assert content["extraction"]["is_current"] is True
    assert content["html"].startswith('<section data-nid="1" data-pages="1,2">')
    assert content["text"].startswith("Annual report")
    assert content["page_count"] == 2
    assert content["confidence"]["n"] == 8
    assert content["outline"] == [{"nid": 2, "tag": "h1", "level": 1, "title": "Annual report 😀"}]


def test_upload_deduplicates_identical_files(auth_client: Client, user: User) -> None:
    response = auth_client.post(
        "/api/documents/upload", {"files": [_file("one.fake"), _file("two.fake")]}
    )
    assert response.status_code == 201
    with acting_as(user):
        assert Document.objects.count() == 2
        assert Blob.objects.count() == 1


def test_upload_rejects_empty_file(auth_client: Client, user: User) -> None:
    response = auth_client.post("/api/documents/upload", {"files": [_file("empty.fake", b"")]})

    assert response.status_code == 422
    assert response.json() == {"detail": "empty.fake is empty"}
    with acting_as(user):
        assert Document.objects.count() == 0


def test_upload_without_files_is_a_validation_error(auth_client: Client) -> None:
    assert auth_client.post("/api/documents/upload", {}).status_code == 422


def test_page_hit_and_search_read_the_current_snapshot(
    auth_client: Client, user: User, document: Document, fake: FakeStrategy
) -> None:
    with acting_as(user):
        snapshot.extract_now(document, fake)
    base = f"/api/documents/{document.pk}"

    page = auth_client.get(f"{base}/pages/2")
    assert page.status_code == 200, page.content
    body = page.json()
    assert (body["number"], body["width"], body["height"]) == (2, 612.0, 792.0)
    assert body["html"].startswith('<p data-nid="3" data-pages="1,2">')
    assert [(r["nid"], r["tag"]) for r in body["regions"]] == [
        (3, "p"),
        (4, "ul"),
        (5, "li"),
        (6, "li"),
        (7, "table"),
        (8, "figure"),
    ]
    assert body["regions"][2]["polygon"] == [[0.5, 0.5], [0.7, 0.7], [0.5, 0.9], [0.3, 0.7]]
    assert body["regions"][0]["text"] == "two pages"
    assert auth_client.get(f"{base}/pages/9").status_code == 404

    hit = auth_client.get(f"{base}/hit", {"page": "2", "x": "0.5", "y": "0.7"})
    assert hit.status_code == 200, hit.content
    assert hit.json()["nid"] == 5 and hit.json()["text"] == "Alpha"
    assert hit.json()["html"] == '<li data-nid="5" data-pages="2">Alpha</li>'
    assert hit.json()["pages"] == [2]
    assert auth_client.get(f"{base}/hit", {"page": "2", "x": "0.95", "y": "0.5"}).json() is None

    found = auth_client.get("/api/documents/search", {"q": "beta"})
    assert found.status_code == 200, found.content
    (result,) = found.json()
    assert result["document_id"] == str(document.pk) and result["node"]["nid"] == 6
    assert "Beta" in result["snippet"]

    runs = auth_client.get(f"{base}/extractions").json()
    assert [r["status"] for r in runs] == ["succeeded"]


def test_reextract_queues_a_run_or_says_why_not(
    auth_client: Client, user: User, document: Document, fake: FakeStrategy
) -> None:
    queued = auth_client.post(f"/api/documents/{document.pk}/reextract")
    assert queued.status_code == 202, queued.content
    assert queued.json()["status"] == "pending"
    assert queued.json()["extractor"] == "fake 1"

    unsupported = upload(user, "x.bin", b"\x00", "application/octet-stream")
    refused = auth_client.post(f"/api/documents/{unsupported.pk}/reextract")
    assert refused.status_code == 422
    assert refused.json() == {"detail": "No extractor handles application/octet-stream"}


def test_patch_renames_and_rejects_unknown_fields(auth_client: Client, document: Document) -> None:
    renamed = auth_client.patch(
        f"/api/documents/{document.pk}",
        {"title": "Annual report 2026"},
        content_type="application/json",
    )
    assert renamed.status_code == 200, renamed.content
    assert renamed.json()["title"] == "Annual report 2026"
    assert renamed.json()["version"] == 2

    typo = auth_client.patch(
        f"/api/documents/{document.pk}", {"titel": "x"}, content_type="application/json"
    )
    assert typo.status_code == 422


def test_download_streams_the_file(client: Client, user: User) -> None:
    document = upload(user, "download.fake", b"payload")

    # A signed link works without a bearer token (plain <a href> in the SPA).
    response = client.get(f"/api/documents/{document.pk}/download?sig={sign_download(document)}")

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"payload"  # type: ignore[attr-defined]
    assert 'filename="download.fake"' in response["Content-Disposition"]


def test_download_rejects_unsigned_or_foreign_links(
    client: Client, user: User, other_user: User
) -> None:
    document = upload(user, "secret.fake")
    other_document = sign_download(Document(id=uuid.uuid7(), owner_id=user.pk))
    # Same document id, but signed for another owner: the link is valid, the row is not theirs.
    other_owner = sign_download(Document(id=document.pk, owner_id=other_user.pk))
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
    document = upload(user, "gone.fake", b"payload")
    url = f"/api/documents/{document.pk}/download?sig={sign_download(document)}"
    working = client.get(url)
    assert working.status_code == 200
    # Consume it rather than close(): closing a test-client response fires `request_finished`,
    # which drops the database connection the rest of this test still needs.
    assert b"".join(working.streaming_content) == b"payload"  # type: ignore[attr-defined]

    assert auth_client.delete(f"/api/documents/{document.pk}").status_code == 204

    assert client.get(url).status_code == 404


def test_delete_hides_the_row_and_keeps_the_file(
    auth_client: Client, user: User, media_root: Path, document: Document
) -> None:
    """Deletes are soft, so the stored object stays: earlier versions of the document still
    reference it (delete_document in apps/documents/api.py)."""

    def stored() -> int:
        return len([p for p in media_root.rglob("*") if p.is_file()])

    assert stored() == 1
    assert auth_client.delete(f"/api/documents/{document.pk}").status_code == 204
    assert auth_client.get(f"/api/documents/{document.pk}").status_code == 404
    assert auth_client.get(f"/api/documents/{document.pk}/content").status_code == 404
    assert auth_client.get("/api/documents").json()["items"] == []
    assert stored() == 1

    with acting_as(user):
        assert Document.objects.filter(pk=document.pk).count() == 0
        gone = Document.all_objects.get(pk=document.pk)
    assert gone.deleted_at is not None
    assert gone.version == 2  # the soft delete is a version of the object


def test_upload_validates_the_size_limit() -> None:
    big = SimpleUploadedFile("big.bin", b"x", content_type="application/octet-stream")
    big.size = MAX_DOCUMENT_SIZE + 1

    with pytest.raises(HttpError, match="exceeds"):
        validate_upload([big])


def test_the_mime_type_is_the_browsers_unless_it_is_useless() -> None:
    assert (
        mime_type_of(SimpleUploadedFile("a.pdf", b"x", content_type="Application/PDF"))
        == "application/pdf"
    )
    assert (
        mime_type_of(SimpleUploadedFile("a.txt", b"x", content_type="application/octet-stream"))
        == "text/plain"
    )
    assert mime_type_of(SimpleUploadedFile("a.html", b"x", content_type="")) == "text/html"
    assert (
        mime_type_of(SimpleUploadedFile("weird", b"x", content_type=""))
        == "application/octet-stream"
    )


def test_lookup_raises_for_unknown_id(user: User) -> None:
    with acting_as(user), pytest.raises(HttpError):
        get_document_for(user, DocumentId(uuid.uuid7()))


def test_store_documents_does_not_extract_by_itself(user: User) -> None:
    with acting_as(user):
        (document,) = store_documents(user, [_file()])
        assert document.latest_content() is None
        assert DocumentContent.objects.count() == 0


# --- The workspace: page images, the page list, and following a run -------------------------------


def _pdf_document(user: User) -> Document:
    """A PDF document with a snapshot — the `pypdf` strategy handles it without a network."""
    document = upload(user, "born-digital.pdf", text_pdf(2), "application/pdf")
    with acting_as(user):
        # Explicitly: a PDF now belongs to `gemini-ocr`, which needs a key and a network.
        snapshot.extract_now(document, strategies.PdfStrategy())
    return document


def test_the_page_list_describes_every_page_with_its_images(
    auth_client: Client, user: User
) -> None:
    document = _pdf_document(user)

    pages = auth_client.get(f"/api/documents/{document.pk}/pages")

    assert pages.status_code == 200, pages.content
    body = pages.json()
    assert [p["number"] for p in body] == [1, 2]
    assert [p["region_count"] for p in body] == [1, 1]
    assert (body[0]["width"], body[0]["height"]) == (612.0, 792.0)  # Letter, in points
    assert body[0]["image_url"].startswith(f"/api/documents/{document.pk}/pages/1/image?sig=")
    assert body[0]["thumb_url"].endswith("&size=thumb")
    assert (
        auth_client.get(f"/api/documents/{document.pk}/pages/1").json()["image_url"]
        == (body[0]["image_url"])
    )


def test_a_page_image_is_rendered_from_the_pdf_behind_a_signed_link(
    client: Client, auth_client: Client, user: User
) -> None:
    document = _pdf_document(user)
    listed = auth_client.get(f"/api/documents/{document.pk}/pages").json()

    # An <img src> sends no bearer header: the signature stands in for the user.
    image = client.get(listed[1]["image_url"])
    assert image.status_code == 200
    # JPEG, not PNG: a scanned page is a photograph, and it encodes in a tenth of the time
    # and a third of the bytes (apps/documents/ocr/render.py).
    assert image["Content-Type"] == "image/jpeg"
    assert image.content.startswith(b"\xff\xd8\xff")
    assert "private" in image["Cache-Control"]
    thumb = client.get(listed[1]["thumb_url"])
    assert thumb.status_code == 200 and len(thumb.content) < len(image.content)

    unsigned = listed[1]["image_url"].split("?")[0]
    assert client.get(unsigned, {"sig": "nope"}).status_code == 403
    assert client.get(unsigned).status_code == 422  # sig missing
    assert client.get(f"{unsigned}?sig={sign_download(document)}&size=huge").status_code == 422


def test_a_document_without_page_images_says_so(client: Client, user: User) -> None:
    document = upload(user, "notes.txt", b"Kurze Notiz.", "text/plain")
    with acting_as(user):
        snapshot.extract_now(document)
    url = f"/api/documents/{document.pk}/pages/1/image?sig={sign_download(document)}"

    assert client.get(url).status_code == 404


def test_the_original_can_be_opened_inline_for_a_viewer(client: Client, user: User) -> None:
    document = _pdf_document(user)
    signature = sign_download(document)

    inline = client.get(f"/api/documents/{document.pk}/download?sig={signature}&inline=true")
    assert inline.status_code == 200
    assert "attachment" not in inline.get("Content-Disposition", "")
    assert inline["Content-Type"] == "application/pdf"
    # The SPA frames this; the site-wide DENY would stop it, and a file has no UI to click-jack.
    assert "X-Frame-Options" not in inline
    # Consumed, not closed: an unread FileResponse leaks the handle into a later test.
    assert b"".join(inline.streaming_content).startswith(b"%PDF")  # type: ignore[attr-defined]


def test_only_a_pdf_is_ever_served_inline(client: Client, user: User) -> None:
    """An uploaded HTML file rendered inline would run on the API's own origin — stored XSS
    against the admin session. `inline=true` is honoured for PDFs and nobody else."""
    document = upload(user, "evil.html", b"<script>alert(1)</script>", "text/html")
    url = f"/api/documents/{document.pk}/download?sig={sign_download(document)}&inline=true"

    served = client.get(url)

    assert served.status_code == 200
    assert "attachment" in served["Content-Disposition"]
    assert served["Content-Type"] == "text/html"
    assert b"".join(served.streaming_content) == b"<script>alert(1)</script>"  # type: ignore[attr-defined]


def test_a_run_can_be_followed_through_its_task_stream(auth_client: Client, user: User) -> None:
    """The task id *is* the snapshot id, so the client needs no second lookup to watch a run."""
    document = _pdf_document(user)

    (run,) = auth_client.get(f"/api/documents/{document.pk}/extractions").json()
    assert run["stream_url"].startswith(f"/api/tasks/{run['id']}/events?sig=")
    # The signature is the one the tasks endpoint checks (it is public but signed).
    assert auth_client.get(f"/api/tasks/{run['id']}").json()["id"] == run["id"]
