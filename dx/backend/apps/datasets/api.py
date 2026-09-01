"""Datasets: schemas, logic and the ninja router in one module.

This is the template for new feature apps (`manage.py newapp <name>` copies the same
shape): paginated list, get, create, PUT, PATCH, delete — all scoped to the caller. Reads go
through `Dataset.objects.for_user(user)`, so another user's dataset does not exist from the
caller's point of view: 404, never 403.

Deletes are soft, so **cascade is application logic** — Django's collector never runs
(CLAUDE.md "Versioning, history and lineage"). The decisions made here:

- soft-deleting a dataset soft-deletes its tag links, then prunes tags that nothing uses;
- a tag exists only while something is tagged with it, so dropping the last link removes it.

Both go through `set_dataset_tags` / `prune_unused_tags` rather than a signal: a signal would
fire for the restore path and the erasure path too, where it is exactly wrong.

Functions shared by several operations take the acting `user` and carry a `_for` suffix where a
route already owns the plain name (the route name is the OpenAPI operation id, `config/api.py`).
"""

import unicodedata
import uuid
from collections.abc import Iterable

from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest
from ninja import Field, ModelSchema, Router, Status
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core import lineage
from apps.core.schemas import StrictSchema
from apps.datasets.models import Dataset, DatasetId, DatasetOptions, DatasetTag, Tag
from apps.documents.api import get_document_for
from apps.documents.models import Document, DocumentId

router = Router(tags=["datasets"])

# A cap, not a policy: keeps a single request from creating an unbounded number of tag rows.
MAX_TAGS = 25

# Schema field that is not a column of `datasets_dataset` (see `VersionedModel.set_payload`).
TAG_FIELD = frozenset({"tags"})


class DatasetOut(ModelSchema):
    # ModelSchema would mark these optional/nullable in the OpenAPI output (pk, blank=True,
    # default=...); redeclaring them keeps the generated TS types strict.
    id: uuid.UUID
    description: str
    row_count: int
    options: DatasetOptions
    tags: list[str]
    version: int

    class Meta:
        model = Dataset
        fields = [
            "id",
            "name",
            "description",
            "row_count",
            "options",
            "version",
            "created",
            "modified",
        ]

    @staticmethod
    def resolve_tags(obj: Dataset) -> list[str]:
        return obj.tag_names()


class DatasetIn(StrictSchema):
    """Create (POST) and full update (PUT): every field, omitted ones take the defaults."""

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    row_count: int = Field(default=0, ge=0)
    options: DatasetOptions = Field(default_factory=DatasetOptions)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)


class DatasetPatch(StrictSchema):
    """Partial update (PATCH): only the fields that are present change."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    options: DatasetOptions | None = None
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)


class ImportDatasetIn(StrictSchema):
    """Build a dataset from an uploaded document (`POST /api/datasets/import-document`)."""

    document_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=200)
    options: DatasetOptions = Field(default_factory=DatasetOptions)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)


def get_dataset_for(user: User, dataset_id: DatasetId) -> Dataset:
    """One dataset, or a 404 — another user's dataset does not exist from here.

    Deliberately *not* prefetching `tag_links`: this is the read every write path starts from,
    and a prefetch cache filled before `set_dataset_tags` would still be holding the old tags
    when the response is serialised.
    """
    try:
        return Dataset.objects.for_user(user).get(pk=dataset_id)
    except Dataset.DoesNotExist:
        raise HttpError(404, "Dataset not found") from None


# --- Tags ---------------------------------------------------------------------------------------


def clean_tag_names(names: Iterable[str]) -> list[str]:
    """Normalise what a client sent: trimmed, NFC, case-folded for comparison, deduplicated.

    Tags are matched case-insensitively but stored as the user first typed them, so "Sales" and
    "sales" are one tag rather than two rows fighting over the unique constraint.
    """
    seen: dict[str, str] = {}
    for raw in names:
        name = unicodedata.normalize("NFC", raw).strip()
        if name:
            seen.setdefault(name.casefold(), name[:100])
    return list(seen.values())


def set_dataset_tags(user: User, dataset: Dataset, names: Iterable[str]) -> None:
    """Make `dataset`'s tags exactly `names`, creating and retiring tags as needed.

    Runs in the caller's transaction (the request's), so the links, the tags and the dataset
    row it belongs to share one history context and render as a single revision.
    """
    wanted = {name.casefold(): name for name in clean_tag_names(names)}
    links = {link.tag.name.casefold(): link for link in dataset.tag_links.select_related("tag")}

    for key, link in links.items():
        if key not in wanted:
            link.soft_delete()

    for key, name in wanted.items():
        if key in links:
            continue
        # Not `get_or_create`: the lookup has to be case-insensitive while the stored value
        # keeps the user's spelling, and a tag that was retired earlier is deliberately *not*
        # revived — its version chain ended, and a new row starts a new one.
        tag = Tag.objects.for_user(user).filter(name__iexact=name).first()
        tag = tag or Tag.objects.create(owner=user, name=name)
        DatasetTag.objects.create(owner=user, dataset=dataset, tag=tag)

    prune_unused_tags(user)


def prune_unused_tags(user: User) -> int:
    """Retire tags nothing is tagged with any more; returns how many were retired.

    A tag carries no information of its own, so an unused one is invisible either way — but
    leaving the rows behind would grow the table forever and make "the user's tags" a list of
    everything they ever typed.
    """
    unused = (
        Tag.objects.for_user(user)
        .annotate(links=Count("dataset_links", filter=Q(dataset_links__deleted_at__isnull=True)))
        .filter(links=0)
    )
    retired = 0
    for tag in unused:
        tag.soft_delete()
        retired += 1
    return retired


def create_dataset_for(
    user: User,
    *,
    name: str,
    description: str = "",
    row_count: int = 0,
    options: DatasetOptions | None = None,
    tags: Iterable[str] = (),
) -> Dataset:
    """Create a dataset with its tags — POST and the document import both land here."""
    dataset = Dataset.objects.create(
        owner=user,
        name=name,
        description=description,
        row_count=row_count,
        options=options or DatasetOptions(),
    )
    set_dataset_tags(user, dataset, tags)
    return dataset


# --- Import: building a dataset from an uploaded document ---------------------------------------

# What a delimited-text import will accept. Anything else is a 400 rather than a row count of
# whatever the newline bytes happened to say.
IMPORTABLE_SUFFIXES = (".csv", ".tsv", ".txt")
IMPORTABLE_TYPES = ("text/csv", "text/tab-separated-values", "text/plain", "application/csv")
_READ_CHUNK = 64 * 1024


def is_importable(document: Document) -> bool:
    name = (document.name or "").lower()
    return name.endswith(IMPORTABLE_SUFFIXES) or document.content_type in IMPORTABLE_TYPES


def count_rows(document: Document, options: DatasetOptions) -> int:
    """Data rows in a delimited-text document: lines, minus the header, minus a trailing newline.

    Streamed in chunks and counted as bytes — the file may be up to `MAX_DOCUMENT_SIZE`, and
    decoding it whole to count newlines would buy nothing (a newline is a newline in every
    encoding this accepts).
    """
    newlines = 0
    last = b""
    with document.file.open("rb") as stream:
        while chunk := stream.read(_READ_CHUNK):
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    if last not in (b"", b"\n"):
        newlines += 1  # a final line without a trailing newline still holds data
    if options.has_header:
        newlines -= 1
    return max(newlines, 0)


def delete_dataset_for(user: User, dataset_id: DatasetId) -> None:
    """Soft delete: the row keeps its place in the version chain (apps/core/models.py).

    The tag links go with it, and a tag that lost its last link is retired too — the cascade
    Django no longer runs for us.
    """
    dataset = get_dataset_for(user, dataset_id)
    set_dataset_tags(user, dataset, [])
    dataset.soft_delete()


def import_dataset_for(
    user: User,
    document_id: DocumentId,
    *,
    name: str | None = None,
    options: DatasetOptions | None = None,
    tags: Iterable[str] = (),
) -> Dataset:
    """Create a dataset from a document and record what it was built from.

    The lineage edge points at the document's *current version*, not at the document row, so a
    later rename or re-upload does not rewrite what this dataset was derived from —
    `stale_derivations(document)` is then how you find the datasets that need rebuilding
    (`apps/core/lineage.py`).

    Runs in the caller's transaction (the request's), so the dataset, its tags and the edge all
    land together or not at all, under one history context.
    """
    document = get_document_for(user, document_id)
    if not is_importable(document):
        raise HttpError(
            400,
            f"{document.name} is not delimited text (expected one of "
            f"{', '.join(IMPORTABLE_SUFFIXES)})",
        )
    settings = options or DatasetOptions()
    dataset = create_dataset_for(
        user,
        name=name or document.name.rsplit(".", 1)[0][:200] or "Imported dataset",
        description=f"Imported from {document.name}",
        row_count=count_rows(document, settings),
        options=settings,
        tags=tags,
    )
    lineage.record_derivation(dataset, sources=[document])
    return dataset


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/datasets", response=list[DatasetOut])
@paginate(PageNumberPagination)  # `?page=&page_size=` → `{items: [...], count: n}`
def list_datasets(request: HttpRequest) -> QuerySet[Dataset]:
    """The user's datasets, newest first.

    `tag_links` is prefetched because every row renders its tags: the related manager is the
    owned one, so the prefetch leaves out soft-deleted links by itself.
    """
    return Dataset.objects.for_user(current_user(request)).prefetch_related("tag_links__tag")


@router.post("/datasets", response={201: DatasetOut})
def create_dataset(request: HttpRequest, payload: DatasetIn) -> Status[Dataset]:
    dataset = create_dataset_for(
        current_user(request),
        name=payload.name,
        description=payload.description,
        row_count=payload.row_count,
        options=payload.options,
        tags=payload.tags,
    )
    return Status(201, dataset)


@router.post("/datasets/import-document", response={201: DatasetOut})
def import_dataset_from_document(request: HttpRequest, payload: ImportDatasetIn) -> Status[Dataset]:
    """Build a dataset from an uploaded document and record the lineage edge.

    The edge names the document *version* the rows were counted from, so
    `GET /api/history/dataset/{id}` can show what this was built from even after the document
    is renamed or replaced (`apps/core/lineage.py`).
    """
    dataset = import_dataset_for(
        current_user(request),
        DocumentId(payload.document_id),
        name=payload.name,
        options=payload.options,
        tags=payload.tags,
    )
    return Status(201, dataset)


@router.get("/datasets/{dataset_id}", response=DatasetOut)
def get_dataset(request: HttpRequest, dataset_id: uuid.UUID) -> Dataset:
    return get_dataset_for(current_user(request), DatasetId(dataset_id))


@router.put("/datasets/{dataset_id}", response=DatasetOut)
def update_dataset(request: HttpRequest, dataset_id: uuid.UUID, payload: DatasetIn) -> Dataset:
    """Full update: every field is replaced (omitted fields take their defaults)."""
    user = current_user(request)
    dataset = get_dataset_for(user, DatasetId(dataset_id))
    dataset.set_payload(payload, exclude=TAG_FIELD)  # tags live in DatasetTag, not on this row
    dataset.save()
    set_dataset_tags(user, dataset, payload.tags)
    return dataset


@router.patch("/datasets/{dataset_id}", response=DatasetOut)
def patch_dataset(request: HttpRequest, dataset_id: uuid.UUID, payload: DatasetPatch) -> Dataset:
    """Partial update: only the fields present in the body change."""
    user = current_user(request)
    dataset = get_dataset_for(user, DatasetId(dataset_id))
    dataset.set_payload_partial(payload, exclude=TAG_FIELD)
    # Only save the row when a column of it actually changed: a PATCH that touches nothing but
    # tags would otherwise bump the dataset's version and add an empty revision to its history.
    if payload.model_fields_set - TAG_FIELD:
        dataset.save()
    if payload.tags is not None:
        set_dataset_tags(user, dataset, payload.tags)
    return dataset


@router.delete("/datasets/{dataset_id}", response={204: None})
def delete_dataset(request: HttpRequest, dataset_id: uuid.UUID) -> Status[None]:
    """`objects` already hides deleted rows, so a second DELETE of the same id is a 404,
    exactly as a hard delete was."""
    delete_dataset_for(current_user(request), DatasetId(dataset_id))
    return Status(204, None)
