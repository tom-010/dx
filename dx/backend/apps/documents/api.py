"""Documents: schemas, logic and the ninja router in one module — multipart upload, listing,
download, delete.

Documents are owned, so every read goes through `Document.objects.for_user(user)` — except the
download path, where the signed link itself is the authorization (a browser `<a href>` cannot
send the bearer header). The link carries the owner's id under the signature: the view opens
that user's tenant context to read the row — row-level security hides it otherwise.
"""

import uuid
from collections.abc import Sequence

from django.core import signing
from django.core.files.uploadedfile import UploadedFile
from django.db.models import QuerySet
from django.http import FileResponse, HttpRequest
from ninja import File, ModelSchema, Router, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile as NinjaUploadedFile
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core.db import tenant_context
from apps.documents.models import Document, DocumentId

router = Router(tags=["documents"])

MAX_DOCUMENT_SIZE = 25 * 1024 * 1024  # 25 MiB
MAX_DOCUMENTS_PER_UPLOAD = 20
DOWNLOAD_LINK_MAX_AGE = 60 * 60  # seconds
_DOWNLOAD_SALT = "documents.download"


class DocumentOut(ModelSchema):
    id: uuid.UUID
    content_type: str
    download_url: str

    class Meta:
        model = Document
        fields = ["id", "name", "content_type", "size", "created"]

    @staticmethod
    def resolve_download_url(obj: Document) -> str:
        # Served by download_document below (signed, expires); later a presigned storage URL.
        return f"/api/documents/{obj.pk}/download?sig={sign_download(obj)}"


def get_document_for(user: User, document_id: DocumentId) -> Document:
    """One document, or a 404 — another user's document does not exist from here."""
    try:
        return Document.objects.for_user(user).get(pk=document_id)
    except Document.DoesNotExist:
        raise HttpError(404, "Document not found") from None


def validate_upload(files: Sequence[UploadedFile[bytes]]) -> None:
    """Reject the whole batch before anything is stored (422; the messages are safe to show)."""
    if not files:
        raise HttpError(422, "No files were uploaded")
    if len(files) > MAX_DOCUMENTS_PER_UPLOAD:
        raise HttpError(422, f"At most {MAX_DOCUMENTS_PER_UPLOAD} files per upload")
    for file in files:
        size = file.size or 0
        if not file.name:
            raise HttpError(422, "A file has no name")
        if size == 0:
            raise HttpError(422, f"{file.name} is empty")
        if size > MAX_DOCUMENT_SIZE:
            raise HttpError(422, f"{file.name} exceeds {MAX_DOCUMENT_SIZE // (1024 * 1024)} MiB")


def store_documents(user: User, files: Sequence[UploadedFile[bytes]]) -> list[Document]:
    """Validate the whole batch first, then persist it (all or nothing from the user's view)."""
    validate_upload(files)
    return [
        Document.create(
            operation=None,
            sources=[],
            owner=user,
            file=file,
            name=file.name or "",
            content_type=file.content_type or "",
            size=file.size or 0,
        )
        for file in files
    ]


def sign_download(document: Document) -> str:
    """Signature for a download link: lets a plain `<a href>` fetch a file without the bearer
    header (the API itself requires auth). Same idea as S3 presigned URLs, which can replace it.
    Signs the document *and* its owner, so the download view knows whose context to open."""
    return signing.dumps([str(document.pk), str(document.owner_id)], salt=_DOWNLOAD_SALT)


def verify_download(document_id: DocumentId, signature: str) -> uuid.UUID | None:
    """The owner id the link was signed for, or None for an invalid/expired/foreign link."""
    try:
        signed = signing.loads(signature, salt=_DOWNLOAD_SALT, max_age=DOWNLOAD_LINK_MAX_AGE)
    except signing.BadSignature:
        return None
    if not isinstance(signed, list) or len(signed) != 2 or signed[0] != str(document_id):
        return None
    try:
        return uuid.UUID(str(signed[1]))
    except ValueError:
        return None


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/documents", response=list[DocumentOut])
@paginate(PageNumberPagination)
def list_documents(request: HttpRequest) -> QuerySet[Document]:
    return Document.objects.for_user(current_user(request))


@router.post("/documents/upload", response={201: list[DocumentOut]})
def upload_documents(
    request: HttpRequest, files: File[list[NinjaUploadedFile]]
) -> Status[list[Document]]:
    """Upload one or more files as multipart/form-data (field name `files`)."""
    return Status(201, store_documents(current_user(request), files))


@router.get("/documents/{document_id}", response=DocumentOut)
def get_document(request: HttpRequest, document_id: uuid.UUID) -> Document:
    return get_document_for(current_user(request), DocumentId(document_id))


@router.get("/documents/{document_id}/download", auth=None)
def download_document(request: HttpRequest, document_id: uuid.UUID, sig: str) -> FileResponse:
    """Streams the stored file as an attachment (binary response, not part of the JSON contract).

    Public but signed: use the `download_url` from `DocumentOut`, links expire after an hour.
    The signature names the owner; their tenant context is opened just for the lookup.
    """
    owner_id = verify_download(DocumentId(document_id), sig)
    if owner_id is None:
        raise HttpError(403, "Invalid or expired download link")
    with tenant_context(owner_id):
        # The verified signature stands in for the user: the ORM scope and the policy need the
        # owner's context, and only that owner's row can match.
        try:
            document = Document.objects.get(pk=document_id, owner_id=owner_id)
        except Document.DoesNotExist:
            raise HttpError(404, "Document not found") from None
    return FileResponse(document.file.open("rb"), as_attachment=True, filename=document.name)


@router.delete("/documents/{document_id}", response={204: None})
def delete_document(request: HttpRequest, document_id: uuid.UUID) -> Status[None]:
    """Soft delete, and the stored object stays.

    Deleting the bytes would leave every earlier version of this document pointing at a file
    that no longer exists. The row drops out of listings and downloads immediately; the object
    is reclaimed when the tenant is erased (`apps/core/tenants.py`).
    """
    get_document_for(current_user(request), DocumentId(document_id)).soft_delete()
    return Status(204, None)
