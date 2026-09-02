"""Documents: schemas, logic and the ninja router in one module.

Uploads (a `Blob` per distinct content, a `Document` per file, an extraction queued), listing,
download, delete, and the read side of the extraction snapshot **through the `Document`
facade** — snapshot rows never appear in the public API shape: a client sees the current
html/text, the outline, one page's reduced html and regions, and the node under a point.

Documents are owned, so every read goes through `Document.objects.for_user(user)` — except the
download path, where the signed link itself is the authorization (a browser `<a href>` cannot
send the bearer header). The link carries the owner's id under the signature: the view opens
that user's tenant context to read the row — row-level security hides it otherwise.
"""

import mimetypes
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core import signing
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Count, IntegerField, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import FileResponse, HttpRequest
from ninja import File, ModelSchema, Router, Schema, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile as NinjaUploadedFile
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core.db import tenant_context
from apps.core.schemas import StrictSchema
from apps.documents import snapshot, strategies
from apps.documents.dating import DateSource, InvalidDate, UncertainDate
from apps.documents.models import (
    ConfStats,
    Dated,
    Document,
    DocumentContent,
    DocumentId,
    ExtractionStatus,
    Node,
    Page,
)

router = Router(tags=["documents"])

MAX_DOCUMENT_SIZE = 25 * 1024 * 1024  # 25 MiB
MAX_DOCUMENTS_PER_UPLOAD = 20
DOWNLOAD_LINK_MAX_AGE = 60 * 60  # seconds
_DOWNLOAD_SALT = "documents.download"
SEARCH_LIMIT = 20
SNIPPET_BEFORE, SNIPPET_AFTER = 60, 120


# --- Schemas -------------------------------------------------------------------------------------


class DateOut(Schema):
    """From when the content originates (`apps/documents/dating.py`): the EDTF truth, its
    strict bounds (null = unknown on that side), how it is known, how sure — and `display`
    for a person: "May 12–20, 1943 (interpolated, 0.60)"."""

    edtf: str
    min: date | None
    max: date | None
    source: DateSource
    conf: float | None
    display: str


def date_out(row: Dated | None) -> DateOut | None:
    estimate = row.estimate if row is not None else None
    if estimate is None:
        return None
    return DateOut(
        edtf=estimate.date.edtf,
        min=estimate.date.min,
        max=estimate.date.max,
        source=estimate.source,
        conf=estimate.conf,
        display=estimate.display(),
    )


class DocumentOut(ModelSchema):
    id: uuid.UUID
    title: str
    meta: dict[str, Any]
    version: int
    mime_type: str
    size: int
    #: The most recent extraction's status; None when no extractor handles this file.
    status: ExtractionStatus | None
    page_count: int
    #: The current snapshot's information-origin date; None until one is extracted.
    date: DateOut | None
    download_url: str

    class Meta:
        model = Document
        fields = ["id", "title", "meta", "created", "modified", "version"]

    @staticmethod
    def resolve_mime_type(obj: Document) -> str:
        return obj.source_blob.mime_type

    @staticmethod
    def resolve_size(obj: Document) -> int:
        return obj.source_blob.size

    @staticmethod
    def resolve_status(obj: Document) -> ExtractionStatus | None:
        status: str | None = getattr(obj, "latest_status", None)  # annotated by `listing`
        return ExtractionStatus(status) if status else None

    @staticmethod
    def resolve_page_count(obj: Document) -> int:
        count: int = getattr(obj, "page_count", 0)
        return count

    @staticmethod
    def resolve_date(obj: Document) -> DateOut | None:
        return date_out(obj.current_content)

    @staticmethod
    def resolve_download_url(obj: Document) -> str:
        # Served by download_document below (signed, expires); later a presigned storage URL.
        return f"/api/documents/{obj.pk}/download?sig={sign_download(obj)}"


class DocumentPatch(StrictSchema):
    title: str | None = None
    meta: dict[str, Any] | None = None


class ExtractionOut(Schema):
    """One extraction run — process state, never the snapshot's rows (`extraction_out`)."""

    id: uuid.UUID
    status: ExtractionStatus
    extractor: str
    is_current: bool
    error: str
    stats: dict[str, Any]
    created: datetime
    started_at: datetime | None
    finished_at: datetime | None


class OutlineEntryOut(Schema):
    nid: int
    tag: str
    level: int | None
    title: str


class DocumentNodeOut(Schema):
    """A node of the current snapshot, as the facade hands it out (`node_out`)."""

    nid: int
    tag: str
    path: str
    level: int | None
    title: str | None
    pages: list[int]
    html: str
    text: str
    text_start: int
    text_end: int
    confidence: ConfStats | None
    date: DateOut | None


class ContentOut(Schema):
    """The facade in one response: what the current snapshot says, plus the latest run."""

    status: ExtractionStatus | None
    extraction: ExtractionOut | None
    html: str
    text: str
    page_count: int
    confidence: ConfStats | None
    date: DateOut | None
    outline: list[OutlineEntryOut]
    meta: dict[str, Any]


class TimelineEntryOut(Schema):
    """A dated node of the current snapshot — `Document.timeline()`, one row each."""

    nid: int
    tag: str
    pages: list[int]
    excerpt: str
    date: DateOut


class RegionOut(Schema):
    nid: int
    tag: str
    order: int
    x0: float
    y0: float
    x1: float
    y1: float
    polygon: list[list[float]] | None
    text: str


class PageOut(Schema):
    number: int
    label: str | None
    width: float | None
    height: float | None
    html: str
    text: str
    confidence: ConfStats | None
    date: DateOut | None
    regions: list[RegionOut]


class SearchHitOut(Schema):
    document_id: uuid.UUID
    title: str
    offset: int
    snippet: str
    node: DocumentNodeOut | None


# --- Logic ---------------------------------------------------------------------------------------


def listing(user: User) -> QuerySet[Document]:
    """The user's documents with what the list shows: blob, latest status, page count."""
    latest = (
        DocumentContent.objects.filter(document=OuterRef("pk"))
        .order_by("-created")
        .values("status")[:1]
    )
    page_count = (
        Page.objects.filter(content_id=OuterRef("current_content_id"))
        .order_by()
        .values("content_id")
        .annotate(n=Count("pk"))
        .values("n")[:1]
    )
    return (
        Document.objects.for_user(user)
        .select_related("source_blob", "current_content")
        .defer("current_content__html", "current_content__text")
        .annotate(
            latest_status=Subquery(latest),
            page_count=Coalesce(Subquery(page_count, output_field=IntegerField()), Value(0)),
        )
    )


def get_document_for(user: User, document_id: DocumentId) -> Document:
    """One document, or a 404 — another user's document does not exist from here."""
    try:
        return listing(user).get(pk=document_id)
    except Document.DoesNotExist:
        raise HttpError(404, "Document not found") from None


def mime_type_of(file: UploadedFile[bytes]) -> str:
    """The browser's MIME type unless it sent nothing useful, then a guess from the name."""
    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    guessed, _ = mimetypes.guess_type(file.name or "")
    return (guessed or declared or "application/octet-stream").lower()


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
    """Validate the whole batch first, then persist it: one blob per distinct content, one
    document per file. Extraction is a separate step (`Document.reextract`)."""
    validate_upload(files)
    documents = []
    for file in files:
        blob = snapshot.store_blob(user.pk, file, mime_type_of(file))
        documents.append(
            Document.create(
                operation=None,
                sources=[],
                owner=user,
                title=(file.name or "")[:500],
                source_blob=blob,
            )
        )
    return documents


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


def extraction_out(content: DocumentContent) -> ExtractionOut:
    return ExtractionOut(
        id=content.pk,
        status=ExtractionStatus(content.status),
        extractor=str(content.extractor),
        is_current=content.is_current,
        error=content.error,
        stats=content.stats,
        created=content.created,
        started_at=content.started_at,
        finished_at=content.finished_at,
    )


def node_out(node: Node) -> DocumentNodeOut:
    return DocumentNodeOut(
        nid=node.nid,
        tag=node.tag,
        path=node.path,
        level=node.level,
        title=node.title,
        pages=node.pages(),
        html=node.html(),
        text=node.text(),
        text_start=node.text_start,
        text_end=node.text_end,
        confidence=node.conf_stats,
        date=date_out(node),
    )


EXCERPT_LENGTH = 120


def timeline_out(node: Node) -> TimelineEntryOut:
    found = date_out(node)
    assert found is not None  # `timeline()` lists dated nodes only
    text = " ".join(node.text().split())
    excerpt = text if len(text) <= EXCERPT_LENGTH else text[:EXCERPT_LENGTH].rstrip() + "…"
    return TimelineEntryOut(
        nid=node.nid, tag=node.tag, pages=node.pages(), excerpt=excerpt, date=found
    )


def parse_period(period: str) -> UncertainDate:
    """A `?period=` query value: EDTF, or a 422 that says what is wrong with it."""
    try:
        return UncertainDate.parse(period)
    except InvalidDate as exc:
        raise HttpError(422, f"period: {exc}") from None


def content_out(document: Document) -> ContentOut:
    latest = document.latest_content()
    return ContentOut(
        status=ExtractionStatus(latest.status) if latest is not None else None,
        extraction=extraction_out(latest) if latest is not None else None,
        html=document.html,
        text=document.text,
        page_count=document.pages.count(),
        confidence=document.confidence(),
        date=date_out(document.current_content),
        outline=[
            OutlineEntryOut(nid=n.nid, tag=n.tag, level=n.level, title=n.title or n.text())
            for n in document.outline()
        ],
        meta=document.meta,
    )


def page_out(page: Page) -> PageOut:
    regions = list(page.regions.select_related("node").order_by("node__nid", "order"))
    for region in regions:
        region.node.content = page.content  # loaded already; no query per region
    return PageOut(
        number=page.number,
        label=page.label,
        width=page.width,
        height=page.height,
        html=page.reduced_html(),
        text=page.text(),
        confidence=page.conf_stats,
        date=date_out(page),
        regions=[
            RegionOut(
                nid=region.node.nid,
                tag=region.node.tag,
                order=region.order,
                x0=region.x0,
                y0=region.y0,
                x1=region.x1,
                y1=region.y1,
                polygon=region.polygon,
                text=region.text(),
            )
            for region in regions
        ],
    )


@dataclass(frozen=True)
class SearchHit:
    document: Document
    offset: int
    snippet: str
    node: Node | None


_TEXT_VECTOR = SearchVector("text", config="simple")


def search_documents_for(user: User, q: str) -> list[SearchHit]:
    """Full-text search over every current snapshot ('simple' config, matches the GIN index),
    each hit resolved to the deepest node containing the first matched term."""
    terms = [term.casefold() for term in q.split()]
    if not terms:
        return []
    query = SearchQuery(q, config="simple")
    contents = (
        DocumentContent.objects.for_user(user)
        .filter(is_current=True)
        .annotate(vector=_TEXT_VECTOR)
        .filter(vector=query)
        .annotate(rank=SearchRank(_TEXT_VECTOR, query))
        .order_by("-rank", "-created")
        .select_related("document")[:SEARCH_LIMIT]
    )
    hits = []
    for content in contents:
        folded = content.text.casefold()
        found = [offset for offset in (folded.find(term) for term in terms) if offset >= 0]
        offset = min(found) if found else 0
        hits.append(
            SearchHit(
                document=content.document,
                offset=offset,
                snippet=snippet(content.text, offset),
                node=content.node_at(offset) if found else None,
            )
        )
    return hits


def snippet(text: str, offset: int) -> str:
    start = max(0, offset - SNIPPET_BEFORE)
    end = min(len(text), offset + SNIPPET_AFTER)
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/documents", response=list[DocumentOut])
@paginate(PageNumberPagination)
def list_documents(request: HttpRequest, period: str | None = None) -> QuerySet[Document]:
    """The caller's documents; `period` (EDTF) keeps those whose current content has
    information from that period — the corpus query, served by the partial date index."""
    documents = listing(current_user(request))
    if period:
        current = DocumentContent.objects.filter(is_current=True)
        documents = documents.filter(current_content__in=current.overlapping(parse_period(period)))
    return documents


@router.get("/documents/search", response=list[SearchHitOut])
def search_documents(request: HttpRequest, q: str) -> list[SearchHitOut]:
    """Full-text search across the caller's documents (their current snapshots)."""
    return [
        SearchHitOut(
            document_id=hit.document.pk,
            title=hit.document.title,
            offset=hit.offset,
            snippet=hit.snippet,
            node=node_out(hit.node) if hit.node is not None else None,
        )
        for hit in search_documents_for(current_user(request), q)
    ]


@router.post("/documents/upload", response={201: list[DocumentOut]})
def upload_documents(
    request: HttpRequest, files: File[list[NinjaUploadedFile]]
) -> Status[list[Document]]:
    """Upload one or more files as multipart/form-data (field name `files`). Each gets an
    extraction queued when an extractor handles its type."""
    user = current_user(request)
    documents = store_documents(user, files)
    for document in documents:
        document.reextract()
    by_id = {d.pk: d for d in listing(user).filter(pk__in=[d.pk for d in documents])}
    return Status(201, [by_id[d.pk] for d in documents])


@router.get("/documents/{document_id}", response=DocumentOut)
def get_document(request: HttpRequest, document_id: uuid.UUID) -> Document:
    return get_document_for(current_user(request), DocumentId(document_id))


@router.patch("/documents/{document_id}", response=DocumentOut)
def update_document(
    request: HttpRequest, document_id: uuid.UUID, payload: DocumentPatch
) -> Document:
    user = current_user(request)
    document = get_document_for(user, DocumentId(document_id))
    document.set_payload_partial(payload)
    document.save(operation=None, sources=[])
    return get_document_for(user, DocumentId(document_id))


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
            document = Document.objects.select_related("source_blob").get(
                pk=document_id, owner_id=owner_id
            )
        except Document.DoesNotExist:
            raise HttpError(404, "Document not found") from None
    return FileResponse(
        document.source_blob.file.open("rb"), as_attachment=True, filename=document.title
    )


@router.delete("/documents/{document_id}", response={204: None})
def delete_document(request: HttpRequest, document_id: uuid.UUID) -> Status[None]:
    """Soft delete, and the stored object stays.

    Deleting the bytes would leave every earlier version of this document pointing at a file
    that no longer exists. The row drops out of listings and downloads immediately; its
    snapshots go with it (they are only reachable through it) and the object is reclaimed when
    the tenant is erased (`apps/core/tenants.py`).
    """
    get_document_for(current_user(request), DocumentId(document_id)).soft_delete()
    return Status(204, None)


@router.get("/documents/{document_id}/content", response=ContentOut)
def get_document_content(request: HttpRequest, document_id: uuid.UUID) -> ContentOut:
    """The current snapshot through the facade: html, text, outline, confidence — and the
    latest run's state, so a client can tell "nothing extracted yet" from "still running"."""
    return content_out(get_document_for(current_user(request), DocumentId(document_id)))


@router.get("/documents/{document_id}/extractions", response=list[ExtractionOut])
def list_extractions(request: HttpRequest, document_id: uuid.UUID) -> list[ExtractionOut]:
    """Every extraction run of this document, newest first."""
    document = get_document_for(current_user(request), DocumentId(document_id))
    runs = document.contents.select_related("extractor").order_by("-created")
    return [extraction_out(run) for run in runs]


@router.post("/documents/{document_id}/reextract", response={202: ExtractionOut})
def reextract_document(
    request: HttpRequest,
    document_id: uuid.UUID,
    from_raw: bool = False,
    strategy: str | None = None,
) -> Status[ExtractionOut]:
    """Queue a fresh extraction — with the strategy registered for this file type, or the
    named one (`?strategy=gemini-ocr`). With `from_raw` the run rebuilds from the latest
    snapshot's extractor output instead of extracting again (a re-dating: no OCR cost, a
    normal flip)."""
    document = get_document_for(current_user(request), DocumentId(document_id))
    chosen = None
    if strategy:
        try:
            chosen = strategies.strategy_named(strategy)
        except strategies.UnknownStrategy as exc:
            raise HttpError(422, str(exc)) from None
    try:
        content = document.reextract(chosen, from_raw=from_raw)
    except snapshot.NothingToRebuildFrom as exc:
        raise HttpError(422, str(exc)) from None
    if content is None:
        raise HttpError(
            422, f"No extractor handles {document.source_blob.mime_type or 'this file'}"
        )
    return Status(202, extraction_out(content))


@router.get("/documents/{document_id}/timeline", response=list[TimelineEntryOut])
def get_document_timeline(
    request: HttpRequest,
    document_id: uuid.UUID,
    source: DateSource | None = None,
    max_conf: float | None = None,
) -> list[TimelineEntryOut]:
    """The current snapshot's dated nodes, earliest first. `source` and `max_conf` make it a
    review queue: `?source=interpolated`, `?max_conf=0.5`."""
    document = get_document_for(current_user(request), DocumentId(document_id))
    nodes = document.timeline()
    if source is not None:
        nodes = nodes.filter(date_source=source)
    if max_conf is not None:
        nodes = nodes.filter(date_conf__lte=max_conf)
    return [timeline_out(node) for node in nodes]


@router.get("/documents/{document_id}/pages/{number}", response=PageOut)
def get_document_page(request: HttpRequest, document_id: uuid.UUID, number: int) -> PageOut:
    """One page of the current snapshot: its reduced html, text and the regions on it."""
    document = get_document_for(current_user(request), DocumentId(document_id))
    page = document.pages.filter(number=number).first()
    if page is None:
        raise HttpError(404, "Page not found")
    return page_out(page)


@router.get("/documents/{document_id}/hit", response=DocumentNodeOut | None)
def hit_document(
    request: HttpRequest, document_id: uuid.UUID, page: int, x: float, y: float
) -> DocumentNodeOut | None:
    """The node drawn under a point of a page (normalized coordinates, origin top-left);
    null over page furniture or empty space."""
    document = get_document_for(current_user(request), DocumentId(document_id))
    node = document.hit(page, x, y)
    return node_out(node) if node is not None else None
