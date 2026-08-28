"""HTTP surface for documents: multipart upload, listing, download, delete."""

import uuid

from django.db.models import QuerySet
from django.http import FileResponse, HttpRequest
from ninja import File, ModelSchema, Router, Status, UploadedFile
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.auth import current_user
from apps.documents import services
from apps.documents.models import Document, DocumentId

router = Router(tags=["documents"])


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
        document_id = DocumentId(obj.pk)
        return f"/api/documents/{document_id}/download?sig={services.sign_download(document_id)}"


@router.get("/documents", response=list[DocumentOut])
@paginate(PageNumberPagination)
def list_documents(request: HttpRequest) -> QuerySet[Document]:
    return services.list_documents(current_user(request))


@router.post("/documents/upload", response={201: list[DocumentOut]})
def upload_documents(
    request: HttpRequest, files: File[list[UploadedFile]]
) -> Status[list[Document]]:
    """Upload one or more files as multipart/form-data (field name `files`)."""
    try:
        documents = services.store_documents(current_user(request), files)
    except services.InvalidDocument as exc:
        raise HttpError(422, str(exc)) from None
    return Status(201, documents)


@router.get("/documents/{document_id}", response=DocumentOut)
def get_document(request: HttpRequest, document_id: uuid.UUID) -> Document:
    try:
        return services.get_document(current_user(request), DocumentId(document_id))
    except services.DocumentNotFound:
        raise HttpError(404, "Document not found") from None


@router.get("/documents/{document_id}/download", auth=None)
def download_document(request: HttpRequest, document_id: uuid.UUID, sig: str) -> FileResponse:
    """Streams the stored file as an attachment (binary response, not part of the JSON contract).

    Public but signed: use the `download_url` from `DocumentOut`, links expire after an hour.
    """
    if not services.verify_download(DocumentId(document_id), sig):
        raise HttpError(403, "Invalid or expired download link")
    try:
        document = services.get_document_for_download(DocumentId(document_id))
    except services.DocumentNotFound:
        raise HttpError(404, "Document not found") from None
    return FileResponse(document.file.open("rb"), as_attachment=True, filename=document.name)


@router.delete("/documents/{document_id}", response={204: None})
def delete_document(request: HttpRequest, document_id: uuid.UUID) -> Status[None]:
    try:
        services.delete_document(current_user(request), DocumentId(document_id))
    except services.DocumentNotFound:
        raise HttpError(404, "Document not found") from None
    return Status(204, None)
