"""Business logic for documents. Plain typed Python; no request objects, no framework glue.

Documents are owned: every function takes the acting `user` except the download path, where
the signed link itself is the authorization (a browser `<a href>` cannot send the bearer header).
"""

from collections.abc import Sequence

from django.core import signing
from django.core.files.uploadedfile import UploadedFile
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.documents.models import Document, DocumentId

MAX_DOCUMENT_SIZE = 25 * 1024 * 1024  # 25 MiB
MAX_DOCUMENTS_PER_UPLOAD = 20
DOWNLOAD_LINK_MAX_AGE = 60 * 60  # seconds
_DOWNLOAD_SALT = "documents.download"


class DocumentNotFound(Exception):
    pass


class InvalidDocument(Exception):
    """The upload was rejected; the message is safe to show to the user."""


def list_documents(user: User) -> QuerySet[Document]:
    return Document.objects.for_user(user)


def get_document(user: User, document_id: DocumentId) -> Document:
    try:
        return Document.objects.for_user(user).get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise DocumentNotFound(document_id) from exc


def get_document_for_download(document_id: DocumentId) -> Document:
    """Lookup behind a *verified* signed link (`verify_download`), which stands in for the user."""
    try:
        return Document.objects.get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise DocumentNotFound(document_id) from exc


def validate_upload(files: Sequence[UploadedFile[bytes]]) -> None:
    if not files:
        raise InvalidDocument("No files were uploaded")
    if len(files) > MAX_DOCUMENTS_PER_UPLOAD:
        raise InvalidDocument(f"At most {MAX_DOCUMENTS_PER_UPLOAD} files per upload")
    for file in files:
        size = file.size or 0
        if not file.name:
            raise InvalidDocument("A file has no name")
        if size == 0:
            raise InvalidDocument(f"{file.name} is empty")
        if size > MAX_DOCUMENT_SIZE:
            raise InvalidDocument(f"{file.name} exceeds {MAX_DOCUMENT_SIZE // (1024 * 1024)} MiB")


def store_documents(user: User, files: Sequence[UploadedFile[bytes]]) -> list[Document]:
    """Validate the whole batch first, then persist it (all or nothing from the user's view)."""
    validate_upload(files)
    return [
        Document.objects.create(
            owner=user,
            file=file,
            name=file.name or "",
            content_type=file.content_type or "",
            size=file.size or 0,
        )
        for file in files
    ]


def delete_document(user: User, document_id: DocumentId) -> None:
    document = get_document(user, document_id)
    document.file.delete(save=False)
    document.delete()


def sign_download(document_id: DocumentId) -> str:
    """Signature for a download link: lets a plain `<a href>` fetch a file without the bearer
    header (the API itself requires auth). Same idea as S3 presigned URLs, which can replace it."""
    return signing.dumps(str(document_id), salt=_DOWNLOAD_SALT)


def verify_download(document_id: DocumentId, signature: str) -> bool:
    try:
        signed_id = signing.loads(signature, salt=_DOWNLOAD_SALT, max_age=DOWNLOAD_LINK_MAX_AGE)
    except signing.BadSignature:
        return False
    return bool(signed_id == str(document_id))
