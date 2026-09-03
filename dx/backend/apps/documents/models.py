"""Documents: an uploaded file and the immutable snapshots extracted from it.

`Document` is the mutable aggregate root and the **facade**: `text`, `html`, `pages`,
`outline()`, `hit()`, `confidence()` all delegate to `current_content` and are empty-safe — a
fresh document, or one whose extraction is still running, answers `""` / `.none()` / `None`,
never an exception. Behind the facade sits one immutable snapshot per extraction run:

    Document ──▶ DocumentContent (html, text, status, is_current)
                    ├── Page        (number, size, thumbnail)
                    ├── Node        (one row per `data-nid` tag in the html: a SQL index into it)
                    └── PageRegion  (the geometry that links a node to a page; word-level conf)

`content.html` is *the* artifact: one sanitized semantic HTML fragment whose structural tags
carry `data-nid` and `data-pages`. `Node` rows hold offsets into it (`html_start..html_end`)
and into the plain-text projection `content.text` (`text_start..text_end`), so reading a node
is a string slice and never a parser. `PageRegion` rows are the physical geometry — normalized
[0, 1] coordinates, origin top-left, y down — and hold per-word confidence; `conf_stats` is the
same additive summary (`ConfStats`) materialized bottom-up: region → node subtree → page →
content. Nothing inside a completed snapshot is ever updated; the extraction pipeline
(`apps/documents/snapshot.py`) is the only writer of snapshot rows, and the admin shows them
read-only.

Storage: every file is a `Blob`, content-addressed by sha256 and deduplicated per tenant; the
source upload, the extractor's raw output and thumbnails are all blobs.

Beside the snapshots sits one scratch table, `DocumentContentDraft`: **the whole state of an
extraction still in flight, as one `ExtractionState` object** — the pages read so far — kept
while the run works so that a run which dies half way through is *resumed* rather than paid
for twice, and discarded the moment a snapshot is written.

Dates: `DocumentContent`, `Page` and `Node` carry the `Dated` mixin — from when their content
*originates* (`apps/documents/dating.py` has the rule and the stage): `date_edtf` is the
truth, `date_min`/`date_max` its strict bounds for SQL, `date_source` how it is known,
`date_conf` how sure. `Document.date` and `timeline()` are the facade's side of it.

What the brief this app implements (`backend/documents_agent_brief.md`, `documents_model_v7.puml`)
had to give up to fit this project's invariants (CLAUDE.md "Invariants"): every table is an
`OwnedModel` — `Blob` dedup and the `Extractor` registry are per tenant, and the row-level
security policy applies to snapshot rows too; every foreign key between owned models is
`CASCADE` (the diagram's `PROTECT` edges), because nothing but tenant erasure ever hard-deletes
and erasure must be able to; deletes are soft, so `prune_contents` and `gc_blobs` retire rows
rather than remove them; and the row version counter every table carries is `version`, so the
extractor's own version string is `Extractor.tool_version`.
"""

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, ClassVar, NewType, Self, TypeVar, cast

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.db.models import F, Q
from django.db.models.base import ModelBase
from django.dispatch import Signal
from django_pydantic_field import SchemaField
from pydantic import BaseModel as PydanticModel
from pydantic import ConfigDict, Field

from apps.core.db import NoTenantContext, current_user_id
from apps.core.examples import unique
from apps.core.history import tracked
from apps.core.models import OwnedManager, OwnedModel, OwnedQuerySet, VersionedModel
from apps.documents.dating import (
    EDTF_MAX_LENGTH,
    DateEstimate,
    DateSource,
    InvalidDate,
    UncertainDate,
)

if TYPE_CHECKING:
    from apps.documents.strategies import ExtractionStrategy

DocumentId = NewType("DocumentId", uuid.UUID)
ContentId = NewType("ContentId", uuid.UUID)

# --- The vocabulary -------------------------------------------------------------------------------

#: The HTML tags a snapshot may contain — the sanitizer's allowlist *is* the node type
#: vocabulary (`Node.tag`), so there is no enum to migrate when a new structure appears.
ALLOWED_TAGS = frozenset(
    {
        "section", "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li",
        "table", "thead", "tbody", "tr", "th", "td",
        "figure", "figcaption", "blockquote", "pre", "code",
    }
)  # fmt: skip
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
#: `data-nid`, `data-pages`, `data-date` (EDTF, when dated) and `data-aside` (standing matter
#: a reader is shown only on request) are allowed on every tag; cells may also span.
NODE_ATTRIBUTES = frozenset({"data-nid", "data-pages", "data-date", "data-aside"})
CELL_ATTRIBUTES = frozenset({"colspan", "rowspan"})
#: Materialized path: zero-padded segments joined by ".", assigned from sibling order.
PATH_WIDTH = 4
PATH_SEPARATOR = "."
PATH_MAX_LENGTH = 255  # ≈ 50 levels


class ExtractionStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: Some pages failed; `stats["failed_pages"]` says which.
    PARTIAL = "partial"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset(
    {ExtractionStatus.SUCCEEDED, ExtractionStatus.PARTIAL, ExtractionStatus.FAILED}
)


class StructureSource(models.TextChoices):
    #: The structure came with the file (a tagged PDF, an HTML source).
    EMBEDDED = "embedded"
    #: A layout model inferred it.
    DETECTED = "detected"
    #: A person corrected it — reserved; nothing writes it yet.
    CURATED = "curated"


# --- Confidence -----------------------------------------------------------------------------------

HIST_BUCKETS = 10


class ConfStats(PydanticModel):
    """Additive summary of per-word confidences: `{n, sum, min, max, hist[10]}`.

    Ten uniform buckets over [0, 1], the last one right-inclusive. Merging two summaries is
    elementwise addition (plus min/max), which is what lets every level of the snapshot carry
    one — region, node subtree, page, content — computed once, bottom-up, at write time. The
    reader picks the measure: `mean`, the histogram, or the exact words of one region.

    `None` in a `conf_stats` column means "no OCR happened here" (born-digital input). Never a
    fake 1.0.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1)
    sum: float
    min: float
    max: float
    hist: list[int] = Field(min_length=HIST_BUCKETS, max_length=HIST_BUCKETS)

    @property
    def mean(self) -> float:
        return self.sum / self.n

    @staticmethod
    def bucket(conf: float) -> int:
        return builtin_min(builtin_max(int(conf * HIST_BUCKETS), 0), HIST_BUCKETS - 1)

    @classmethod
    def of(cls, confs: Iterable[float | None]) -> ConfStats | None:
        """The summary of some word confidences; None when none of them is known."""
        known = [conf for conf in confs if conf is not None]
        if not known:
            return None
        hist = [0] * HIST_BUCKETS
        for conf in known:
            hist[cls.bucket(conf)] += 1
        return cls(
            n=len(known),
            sum=builtin_sum(known),
            min=builtin_min(known),
            max=builtin_max(known),
            hist=hist,
        )

    @classmethod
    def merge(cls, parts: Iterable[ConfStats | None]) -> ConfStats | None:
        """The summary of several summaries — plain addition, so it equals recomputing."""
        found = [part for part in parts if part is not None]
        if not found:
            return None
        return cls(
            n=builtin_sum(part.n for part in found),
            sum=builtin_sum(part.sum for part in found),
            min=builtin_min(part.min for part in found),
            max=builtin_max(part.max for part in found),
            hist=[
                builtin_sum(column) for column in zip(*(part.hist for part in found), strict=True)
            ],
        )


# The pydantic fields above are called `sum`, `min` and `max` (that is the JSON schema), which
# shadows the builtins inside the class body only; the methods reach them through these names.
builtin_sum = sum
builtin_min = min
builtin_max = max


# --- Geometry -------------------------------------------------------------------------------------

Point = tuple[float, float]


def point_in_ring(ring: list[Point], x: float, y: float) -> bool:
    """Ray casting over an implicitly closed ring (last point connects to the first)."""
    inside = False
    count = len(ring)
    for i in range(count):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % count]
        if (y0 > y) != (y1 > y):
            cross = (x1 - x0) * (y - y0) / (y1 - y0) + x0
            if x < cross:
                inside = not inside
    return inside


def ring_area(ring: list[Point]) -> float:
    """Shoelace formula, absolute value."""
    total = 0.0
    count = len(ring)
    for i in range(count):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % count]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2


def envelope_of(ring: list[Point]) -> tuple[float, float, float, float]:
    xs = [x for x, _ in ring]
    ys = [y for _, y in ring]
    return builtin_min(xs), builtin_min(ys), builtin_max(xs), builtin_max(ys)


@dataclass(frozen=True)
class Word:
    """One entry of `PageRegion.words`: `[x0, y0, x1, y1, text_start, text_end, conf]`."""

    x0: float
    y0: float
    x1: float
    y1: float
    text_start: int
    text_end: int
    conf: float | None

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def as_list(self) -> list[float | int | None]:
        return [self.x0, self.y0, self.x1, self.y1, self.text_start, self.text_end, self.conf]


@dataclass(frozen=True)
class Polygon:
    """A node's shape on one page: a closed ring of points (the last connects to the first),
    normalized — or in pixels when a render size was given."""

    page: Page
    points: list[Point]


@dataclass(frozen=True)
class WordHit:
    node: Node
    region: PageRegion
    word: Word
    text: str


@dataclass(frozen=True)
class ContentDiff:
    """`DocumentContent.diff()`: what changed between two snapshots, v1-minimal."""

    status: tuple[str, str]
    stats: tuple[dict[str, object], dict[str, object]]
    nodes_added: list[tuple[str, str, str | None]]
    nodes_removed: list[tuple[str, str, str | None]]
    text_similarity: float


#: Sent inside the flip transaction (`snapshot.switch_current`) with `document=`, `content=`
#: (the new current snapshot) and `previous=` (the old one or None). Consumers that build
#: something from a snapshot — a search index, embeddings — store the `DocumentContent` id
#: they were built from and rebuild on this signal: invalidate-and-recompute, no alignment
#: across runs. Heavy work belongs in a task enqueued with `transaction.on_commit`.
content_switched = Signal()


# --- Dated: information-origin dates (apps/documents/dating.py) ----------------------------------

_DatedT = TypeVar("_DatedT", bound="OwnedModel")


class DatedQuerySet(OwnedQuerySet[_DatedT]):
    """Querysets of `Dated` models: the one implementation of the NULL-bound period query."""

    def overlapping(self, period: UncertainDate) -> Self:
        """Rows whose date range overlaps `period`: `(date_min IS NULL OR date_min <= P.max)
        AND (date_max IS NULL OR date_max >= P.min) AND NOT both NULL` — a NULL bound is open
        on that side, and a fully undated row never matches. An open side of `period` drops
        its clause."""
        condition = ~Q(date_min__isnull=True, date_max__isnull=True)
        if period.max is not None:
            condition &= Q(date_min__isnull=True) | Q(date_min__lte=period.max)
        if period.min is not None:
            condition &= Q(date_max__isnull=True) | Q(date_max__gte=period.min)
        return self.filter(condition)

    def dated(self) -> Self:
        return self.filter(date_edtf__isnull=False)

    def undated(self) -> Self:
        return self.filter(date_edtf__isnull=True)


class DatedManager(OwnedManager[_DatedT]):
    """`Model.objects` of a `Dated` model: the tenant scope plus `overlapping()`."""

    queryset_class = DatedQuerySet

    def get_queryset(self) -> DatedQuerySet[_DatedT]:
        return cast(DatedQuerySet[_DatedT], super().get_queryset())

    def all(self) -> DatedQuerySet[_DatedT]:
        return self.get_queryset()

    def filter(self, *args: Q, **kwargs: object) -> DatedQuerySet[_DatedT]:
        return self.get_queryset().filter(*args, **kwargs)

    def overlapping(self, period: UncertainDate) -> DatedQuerySet[_DatedT]:
        return self.get_queryset().overlapping(period)

    def dated(self) -> DatedQuerySet[_DatedT]:
        return self.get_queryset().dated()

    def undated(self) -> DatedQuerySet[_DatedT]:
        return self.get_queryset().undated()


class Dated(OwnedModel):
    """Abstract mixin: from when the row's content originates. `date_edtf` is the truth; the
    bounds are its strict derivation for SQL (NULL = unknown on that side); `date_source` says
    how we know and `date_conf` how sure the estimator was. All five NULL = undated. Written
    once by the dating stage of the snapshot builder; `check_date()` is what every writer must
    pass. An abstract `OwnedModel` rather than a bare `models.Model`, so `save()` keeps the
    project's signature and the concrete models inherit it alone."""

    date_edtf = models.CharField(max_length=EDTF_MAX_LENGTH, null=True, blank=True)
    date_min = models.DateField(null=True, blank=True)
    date_max = models.DateField(null=True, blank=True)
    date_source = models.CharField(max_length=12, choices=DateSource, null=True, blank=True)
    #: The estimator's belief in the attribution, 0..1 — never mixed into `conf_stats`.
    date_conf = models.FloatField(null=True, blank=True)

    class Meta(OwnedModel.Meta):
        abstract = True

    @property
    def date(self) -> UncertainDate | None:
        if self.date_edtf is None:
            return None
        return UncertainDate(self.date_edtf, self.date_min, self.date_max)

    @property
    def estimate(self) -> DateEstimate | None:
        found = self.date
        if found is None or self.date_source is None:
            return None
        return DateEstimate(found, DateSource(self.date_source), self.date_conf)

    def set_date(self, estimate: DateEstimate | None) -> None:
        self.date_edtf = estimate.date.edtf if estimate else None
        self.date_min = estimate.date.min if estimate else None
        self.date_max = estimate.date.max if estimate else None
        self.date_source = estimate.source if estimate else None
        self.date_conf = estimate.conf if estimate else None

    def check_date(self) -> None:
        """All five columns set or none, the EDTF string valid, the bounds its own — raises
        `InvalidDate` otherwise. Called by `save()`; bulk writers call it themselves."""
        if self.date_edtf is None:
            others = (self.date_min, self.date_max, self.date_source, self.date_conf)
            if any(value is not None for value in others):
                raise InvalidDate("an undated row has every date column NULL")
            return
        parsed = UncertainDate.parse(self.date_edtf)
        if (parsed.min, parsed.max) != (self.date_min, self.date_max):
            raise InvalidDate(
                f"{self.date_edtf} bounds {parsed.min}..{parsed.max}, "
                f"stored {self.date_min}..{self.date_max}"
            )
        if self.date_source is None:
            raise InvalidDate("a dated row needs a date_source")
        if self.date_conf is not None and not 0 <= self.date_conf <= 1:
            raise InvalidDate("date_conf is a belief in 0..1")

    def save(  # type: ignore[override]  # see OwnedModel.save
        self,
        *,
        operation: str | None,
        sources: Sequence[VersionedModel] | None,
        operation_description: str | None = None,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        self.check_date()
        super().save(
            operation=operation,
            sources=sources,
            operation_description=operation_description,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


def date_range_constraint(name: str) -> models.CheckConstraint:
    """`date_min <= date_max` when both are set — one per `Dated` model."""
    return models.CheckConstraint(
        condition=Q(date_min__isnull=True)
        | Q(date_max__isnull=True)
        | Q(date_min__lte=F("date_max")),  # fmt: skip
        name=name,
    )


# --- Blob: content-addressed storage --------------------------------------------------------------


def blob_upload_path(instance: models.Model, filename: str) -> str:
    """`documents/<owner id>/blobs/ab/cd/<sha256>` — sharded by hash, one prefix per tenant.

    The owner prefix keeps a tenant's objects listable and erasable like every other upload
    (`apps.core.models.owned_upload_path`); the hash is the name, so equal bytes land on equal
    keys and a re-upload never writes a second object.
    """
    if not isinstance(instance, Blob):
        raise TypeError("blob_upload_path is for Blob files only")
    owner_id = instance.__dict__.get("owner_id") or current_user_id.get()
    if owner_id is None:
        raise NoTenantContext("Blob has no owner to build a file path from")
    sha = instance.sha256
    return f"documents/{owner_id}/blobs/{sha[:2]}/{sha[2:4]}/{sha}"


@tracked
class Blob(OwnedModel):
    """Immutable bytes, addressed by their sha256: originals *and* derived artifacts
    (thumbnails, an extractor's raw output). One row per distinct content per tenant
    (`snapshot.store_blob` deduplicates); the file is never overwritten."""

    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    mime_type = models.CharField(max_length=100, blank=True)
    file = models.FileField(upload_to=blob_upload_path, max_length=255)

    class Meta(OwnedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "sha256"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_active_blob_sha256_per_owner",
            )
        ]

    @staticmethod
    def example() -> Blob:
        # Distinct bytes per call: two examples of a blob side by side must not collide on
        # the (owner, sha256) constraint — and a blob *is* its bytes.
        content = f"name,amount\nrent,1200\n# {unique('example')}\n".encode()
        return Blob(
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            mime_type="text/csv",
            file=ContentFile(content, name="upload"),
        )

    def read_bytes(self) -> bytes:
        with self.file.open("rb") as stream:
            data: bytes = stream.read()
            return data

    def __str__(self) -> str:
        return f"{self.mime_type or 'blob'} {self.size} B {self.sha256[:12]}"


# --- Extractor registry ---------------------------------------------------------------------------


@tracked
class Extractor(OwnedModel):
    """One extraction strategy at one version with one configuration — what a snapshot was
    produced with. Rows are created on demand from the strategies in
    `apps/documents/strategies.py` (`snapshot.extractor_row`); a new tool version is a new
    row, and therefore new snapshots."""

    name = models.CharField(max_length=100)
    #: The tool's own version string ("2.48.0"); `version` is the row's version counter.
    tool_version = models.CharField(max_length=50)
    config = models.JSONField(default=dict, blank=True)

    class Meta(OwnedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name", "tool_version"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_active_extractor_per_owner",
            )
        ]

    @staticmethod
    def example() -> Extractor:
        return Extractor(name="plain-text", tool_version=unique("1"), config={})

    def __str__(self) -> str:
        return f"{self.name} {self.tool_version}"


# --- Document: the aggregate root and facade ------------------------------------------------------


@tracked
class Document(OwnedModel):
    """An uploaded file plus the metadata shown in listings, and the facade over its current
    extraction snapshot. Every property below is empty-safe."""

    #: Given at ingest (the upload's file name); `heading_title()` is the fallback.
    title = models.CharField(max_length=500, blank=True)
    #: The uploaded file's MD5, and the tenant's answer to "do I already hold this?" — an
    #: upload of a file already in the library is skipped rather than filed twice
    #: (`api.store_documents`). MD5 because it is what a scanner, a mail client and a file
    #: manager print beside a file, so a person can check by hand; the *content* key is the
    #: blob's sha256, and nothing here rests on the hash being hard to collide with.
    md5 = models.CharField(max_length=32)
    meta = models.JSONField(default=dict, blank=True)
    # The diagram says PROTECT; between owned models this project uses CASCADE, because only
    # tenant erasure ever hard-deletes and PROTECT would make that one delete fail.
    source_blob = models.ForeignKey(Blob, on_delete=models.CASCADE, related_name="+")
    thumbnail = models.ForeignKey(
        Blob, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    #: The read accelerator; `DocumentContent.is_current` (partial unique) is the truth.
    #: Only `snapshot.switch_current` writes either.
    current_content = models.ForeignKey(
        "DocumentContent", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta(OwnedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "md5"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_active_document_md5_per_owner",
            )
        ]

    @staticmethod
    def example() -> Document:
        blob = Blob.example()
        content = blob.file.read()
        blob.file.seek(0)  # the example is saved from here; leave the file where it was
        return Document(
            title="Expenses 2026",
            md5=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            source_blob=blob,
            meta={},
        )

    def clean(self) -> None:
        current = self.current_content
        if current is not None and (not current.is_current or current.document_id != self.pk):
            raise ValidationError({"current_content": "must be this document's current snapshot"})

    # -- facade: delegates to the current snapshot, empty-safe --

    @property
    def text(self) -> str:
        return self.current_content.text if self.current_content is not None else ""

    @property
    def html(self) -> str:
        return self.current_content.html if self.current_content is not None else ""

    @property
    def pages(self) -> DatedQuerySet[Page]:
        if self.current_content is None:
            return Page.objects.all().none()
        return self.current_content.pages.all()

    def outline(self) -> list[Node]:
        return self.current_content.outline() if self.current_content is not None else []

    def hit(self, page_no: int, x: float, y: float) -> Node | None:
        page = self.pages.filter(number=page_no).first()
        return page.hit(x, y) if page is not None else None

    def confidence(self) -> ConfStats | None:
        return self.current_content.conf_stats if self.current_content is not None else None

    @property
    def date(self) -> UncertainDate | None:
        """From when the document's content originates — None while nothing is extracted."""
        return self.current_content.date if self.current_content is not None else None

    def timeline(self) -> DatedQuerySet[Node]:
        """The current snapshot's dated nodes, earliest first (open starts last), then in
        document order — what a reader scrolls, and what a reviewer filters by source."""
        if self.current_content is None:
            return Node.objects.all().none()
        nodes = self.current_content.nodes.dated()
        return nodes.order_by(F("date_min").asc(nulls_last=True), "nid")

    def heading_title(self) -> str:
        """The highest heading of the current snapshot, first in document order; "" if none."""
        for level in range(1, 7):
            heading = next((n for n in self.outline() if n.level == level), None)
            if heading is not None:
                return heading.text()
        return ""

    def latest_content(self) -> DocumentContent | None:
        """The most recent snapshot, whatever its status — running, failed or current."""
        return self.contents.order_by("-created").first()

    def reextract(
        self, strategy: ExtractionStrategy | None = None, *, from_raw: bool = False
    ) -> DocumentContent | None:
        """Start a new extraction: a PENDING snapshot now, the rows when the task has run.

        `strategy=None` picks the one registered for the source blob's MIME type
        (`apps/documents/strategies.py`); None back means no strategy handles this kind of
        file. `from_raw` rebuilds from the latest snapshot's extractor output instead of
        extracting again (re-dating). See `snapshot.start_extraction`.
        """
        from apps.documents import snapshot  # noqa: PLC0415 - snapshot imports these models

        return snapshot.start_extraction(self, strategy, from_raw=from_raw)

    def __str__(self) -> str:
        return self.title or f"Document({self.pk})"


# --- The immutable snapshot -----------------------------------------------------------------------


class DocumentContentManager(DatedManager["DocumentContent"]):
    """`DocumentContent.objects` / `document.contents`: the tenant-scoped default plus the two
    status filters. Filters on a *method*, never on the default queryset — a filtered default
    manager breaks related managers, the admin and cascades."""

    def successful(self) -> DatedQuerySet[DocumentContent]:
        return self.get_queryset().filter(status=ExtractionStatus.SUCCEEDED)

    def terminal(self) -> DatedQuerySet[DocumentContent]:
        return self.get_queryset().filter(status__in=TERMINAL_STATUSES)


@tracked
class DocumentContent(Dated):
    """One extraction run and its result. Process fields (`status`, `error`, timestamps) live
    here deliberately; there is no separate job table. Frozen once `status` is terminal."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="contents")
    #: The input bytes this snapshot was produced from (pinned: a re-uploaded source does
    #: not change what an old snapshot says).
    blob = models.ForeignKey(Blob, on_delete=models.CASCADE, related_name="+")
    #: The extractor's native payload, so a schema change can re-project without paying for
    #: OCR again.
    raw_output = models.ForeignKey(
        Blob, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    extractor = models.ForeignKey(Extractor, on_delete=models.CASCADE, related_name="contents")
    status = models.CharField(
        max_length=10, choices=ExtractionStatus, default=ExtractionStatus.PENDING
    )
    is_current = models.BooleanField(default=False)
    #: What this reading of the document calls it — the last thing the pipeline does is read
    #: the assembled HTML and name it ("Arztbrief Orthopädie, 05.08.2026"). A file name says
    #: what a scanner called a file; this says what the document is. `switch_current` puts it
    #: on the `Document` unless a person has renamed it themselves.
    title = models.CharField(max_length=500, blank=True)
    html = models.TextField(blank=True)
    text = models.TextField(blank=True)
    conf_stats = SchemaField(ConfStats | None, null=True, blank=True, default=None)
    stats = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # `all_objects` is inherited; a manager declared here is the default one regardless of
    # creation order (Django sorts by inheritance depth first).
    objects: ClassVar[DocumentContentManager] = DocumentContentManager()

    class Meta(OwnedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["document"],
                condition=Q(is_current=True, deleted_at__isnull=True),
                name="one_current_content_per_document",
            ),
            date_range_constraint("content_date_range"),
        ]
        indexes = [
            *OwnedModel.Meta.indexes,
            # "Documents with information from period P": the corpus query, current only.
            # Leads with `owner`: under row-level security every query is tenant-scoped, so
            # an index that does not start with the tenant column is never the one chosen.
            models.Index(
                fields=["owner", "date_min", "date_max"],
                name="documents_content_dates_idx",
                condition=Q(is_current=True),
            ),
            # Full-text search over the plain projection. 'simple': documents are multilingual.
            # Known limit: a tsvector caps at 1 MB / 16383 positions — fine for v1; very large
            # books would need node-level chunks or pg_trgm.
            GinIndex(SearchVector("text", config="simple"), name="documents_content_text_fts"),
        ]

    @staticmethod
    def example() -> DocumentContent:
        document = Document.example()
        return DocumentContent(
            document=document,
            blob=document.source_blob,
            extractor=Extractor.example(),
            status=ExtractionStatus.SUCCEEDED,
            title="Expenses, March",
            html='<p data-nid="1">Rent, 1200</p>',
            text="Rent, 1200",
            stats={"pages": 0, "nodes": 1, "regions": 0},
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def outline(self) -> list[Node]:
        """The headings, in document order."""
        return list(self.nodes.filter(tag__in=HEADINGS).order_by("nid"))

    def node(self, nid: int) -> Node:
        return self.nodes.get(nid=nid)

    def node_at(self, offset: int) -> Node | None:
        """The deepest node whose text range contains `offset` — how a search hit becomes a
        node. Smallest range wins; uses the `(content, text_start)` index."""
        return (
            self.nodes.filter(text_start__lte=offset, text_end__gt=offset)
            .order_by(models.F("text_end") - models.F("text_start"), "-nid")
            .first()
        )

    def diff(self, other: DocumentContent) -> ContentDiff:
        """Status and stats deltas, node sets compared by `(path, tag, title)`, and a quick
        text similarity ratio (an upper bound; exact ratios are quadratic in the text)."""
        mine = {(n.path, n.tag, n.title) for n in self.nodes.all()}
        theirs = {(n.path, n.tag, n.title) for n in other.nodes.all()}
        return ContentDiff(
            status=(self.status, other.status),
            stats=(self.stats, other.stats),
            nodes_added=sorted(theirs - mine),
            nodes_removed=sorted(mine - theirs),
            text_similarity=SequenceMatcher(None, self.text, other.text).quick_ratio(),
        )

    def __str__(self) -> str:
        return f"{self.status} snapshot of {self.document_id}"


class PageManager(DatedManager["Page"]):
    pass


@tracked
class Page(Dated):
    """One page of a snapshot. An HTML source has none; everything tolerates that."""

    content = models.ForeignKey(DocumentContent, on_delete=models.CASCADE, related_name="pages")
    number = models.PositiveIntegerField()
    #: The printed label ("iv", "A-3") when it differs from the number.
    label = models.CharField(max_length=20, null=True, blank=True)
    #: Source resolution in points or pixels — what a crop of the render scales by.
    width = models.FloatField(null=True, blank=True)
    height = models.FloatField(null=True, blank=True)
    conf_stats = SchemaField(ConfStats | None, null=True, blank=True, default=None)
    meta = models.JSONField(default=dict, blank=True)
    thumbnail = models.ForeignKey(
        Blob, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    objects: ClassVar[PageManager] = PageManager()

    class Meta(OwnedModel.Meta):
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["content", "number"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_active_page_number_per_content",
            ),
            models.CheckConstraint(condition=Q(number__gte=1), name="page_number_from_one"),
            date_range_constraint("page_date_range"),
        ]
        indexes = [
            *OwnedModel.Meta.indexes,
            models.Index(fields=["content", "date_min"], name="documents_page_date_idx"),
        ]

    @staticmethod
    def example() -> Page:
        return Page(content=DocumentContent.example(), number=1, width=612.0, height=792.0)

    def candidates(self, x: float, y: float) -> list[PageRegion]:
        """Regions whose shape contains the point: envelope filter in SQL, polygon refine in
        Python, smallest area first."""
        # A region with no box has NULL bounds, and a NULL comparison is never true: it drops
        # out here without a special case.
        envelope_hits = self.regions.filter(
            x0__lte=x, x1__gte=x, y0__lte=y, y1__gte=y
        ).select_related("node")
        found = [region for region in envelope_hits if region.contains(x, y)]
        found.sort(key=lambda region: region.area())
        for region in found:
            region.node.content = self.content  # already loaded; no query per node
        return found

    def hit(self, x: float, y: float) -> Node | None:
        """The node drawn at a point of this page — page furniture returns None."""
        found = self.candidates(x, y)
        return found[0].node if found else None

    def hit_word(self, x: float, y: float) -> WordHit | None:
        """Word-level lookup: the winning region's `words` scanned for the containing box."""
        for region in self.candidates(x, y):
            for word in region.word_list():
                if word.contains(x, y):
                    return WordHit(
                        node=region.node,
                        region=region,
                        word=word,
                        text=self.content.text[word.text_start : word.text_end],
                    )
        return None

    def reduced_nodes(self) -> list[Node]:
        """The nodes drawn on this page, minus any whose ancestor is also drawn here, in
        document order. A paragraph straddling pages is listed on both — intended."""
        drawn = list(self.content.nodes.filter(regions__page=self).distinct().order_by("nid"))
        paths = {node.path for node in drawn}
        return [node for node in drawn if not any(a in paths for a in node.ancestor_paths())]

    def reduced_html(self) -> str:
        return "".join(node.html() for node in self.reduced_nodes())

    def text(self) -> str:
        return "\n\n".join(node.text() for node in self.reduced_nodes())

    def __str__(self) -> str:
        return f"page {self.number}"


class NodeManager(DatedManager["Node"]):
    pass


@tracked
class Node(Dated):
    """One structural tag of the snapshot's html — the SQL index into it.

    The *type is the tag* (`p`, `h2`, `table`, `section`, `li`, …). `level` is the semantic
    level (h1..h6, section depth); tree depth is derivable from `path`, a materialized path
    of zero-padded sibling positions (`0002.0011.0001`), so a subtree is `path LIKE 'prefix%'`
    and the whole outerHTML of a subtree is one string slice.
    """

    content = models.ForeignKey(DocumentContent, on_delete=models.CASCADE, related_name="nodes")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )
    #: Pre-order document-order number from 1; equals the `data-nid` attribute.
    nid = models.PositiveIntegerField()
    path = models.CharField(max_length=PATH_MAX_LENGTH)
    #: Position among siblings, from 0.
    order = models.PositiveIntegerField()
    tag = models.CharField(max_length=20)
    level = models.PositiveSmallIntegerField(null=True, blank=True)
    #: A heading's own text; a section's heading; else None.
    title = models.CharField(max_length=1000, null=True, blank=True)
    source = models.CharField(
        max_length=10, choices=StructureSource, default=StructureSource.DETECTED
    )
    conf_stats = SchemaField(ConfStats | None, null=True, blank=True, default=None)
    # Codepoint offsets, end-exclusive, into `content.html` / `content.text`.
    html_start = models.IntegerField()
    html_end = models.IntegerField()
    text_start = models.IntegerField()
    text_end = models.IntegerField()

    objects: ClassVar[NodeManager] = NodeManager()

    class Meta(OwnedModel.Meta):
        ordering = ["nid"]
        constraints = [
            models.UniqueConstraint(
                fields=["content", "nid"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_active_node_nid_per_content",
            ),
            models.CheckConstraint(
                condition=Q(html_start__lte=models.F("html_end")), name="node_html_range"
            ),
            models.CheckConstraint(
                condition=Q(text_start__lte=models.F("text_end")), name="node_text_range"
            ),
            date_range_constraint("node_date_range"),
        ]
        indexes = [
            *OwnedModel.Meta.indexes,
            models.Index(fields=["content", "date_min"], name="documents_node_date_idx"),
            # `varchar_pattern_ops` so `path LIKE 'prefix%'` (subtree) uses the index.
            models.Index(
                fields=["content", "path"],
                name="documents_node_path_idx",
                opclasses=["", "varchar_pattern_ops"],
            ),
            models.Index(fields=["content", "text_start"], name="documents_node_text_idx"),
            models.Index(fields=["content", "tag"], name="documents_node_tag_idx"),
        ]

    @staticmethod
    def example() -> Node:
        # Matches `DocumentContent.example()`'s html/text: the one paragraph in it.
        return Node(
            content=DocumentContent.example(),
            nid=1,
            path="0001",
            order=0,
            tag="p",
            source=StructureSource.EMBEDDED,
            html_start=0,
            html_end=len('<p data-nid="1">Rent, 1200</p>'),
            text_start=0,
            text_end=len("Rent, 1200"),
        )

    def html(self) -> str:
        return self.content.html[self.html_start : self.html_end]

    def text(self) -> str:
        return self.content.text[self.text_start : self.text_end]

    @property
    def depth(self) -> int:
        return self.path.count(PATH_SEPARATOR)

    def ancestor_paths(self) -> list[str]:
        parts = self.path.split(PATH_SEPARATOR)
        return [PATH_SEPARATOR.join(parts[:n]) for n in range(1, len(parts))]

    def subtree(self) -> DatedQuerySet[Node]:
        """This node and everything below it, in document order (`path LIKE 'prefix%'`)."""
        return self.content.nodes.filter(path__startswith=self.path).order_by("nid")

    def pages(self) -> list[int]:
        """The page numbers this node's subtree is drawn on, ascending."""
        numbers = PageRegion.objects.filter(
            node__content_id=self.content_id, node__path__startswith=self.path
        ).values_list("page__number", flat=True)
        return sorted(set(numbers))

    def polygons(self, width: float | None = None, height: float | None = None) -> list[Polygon]:
        """Where this node is drawn: one polygon per region, in document order. Normalized, or
        in pixels of a render of `width` × `height`."""
        sx = width if width is not None else 1.0
        sy = height if height is not None else 1.0
        polygons = []
        for region in self.regions.select_related("page"):
            ring = region.ring()
            if ring is None:
                continue  # the region says which page, not where on it
            polygons.append(Polygon(page=region.page, points=[(x * sx, y * sy) for x, y in ring]))
        return polygons

    def conf(self) -> ConfStats | None:
        return self.conf_stats

    def __str__(self) -> str:
        return f"<{self.tag}> #{self.nid}"


@tracked
class PageRegion(OwnedModel):
    """That a node occurs on a page — and, when the extractor knows it, where.

    `polygon` (a closed ring of normalized points) is the truth when set — it handles rotation
    and skew; the envelope `x0..y1` is derived from it for plain indexed SQL, and
    `polygon IS NULL` ⇒ the region *is* its envelope. **The geometry as a whole is optional**:
    an extractor that reads a page's text without laying it out (a PDF's own text layer, an
    HTML source, a model asked only for structure) still records which page a node is on, and
    a region with no box simply never wins a `hit()`.

    `words` holds the word boxes with their global text offsets and confidence; `conf_stats`
    summarizes them. `detect_conf` is a different quantity — the layout model's belief in the
    region itself — and is never mixed into text-quality rollups.
    """

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="regions")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="regions")
    x0 = models.FloatField(null=True, blank=True)
    y0 = models.FloatField(null=True, blank=True)
    x1 = models.FloatField(null=True, blank=True)
    y1 = models.FloatField(null=True, blank=True)
    polygon = models.JSONField(null=True, blank=True)
    words = models.JSONField(null=True, blank=True)
    conf_stats = SchemaField(ConfStats | None, null=True, blank=True, default=None)
    detect_conf = models.FloatField(null=True, blank=True)
    #: Reading order among the node's regions (a paragraph straddling two pages has two).
    order = models.PositiveIntegerField()
    # The part of the node's text drawn here; None for shapes without text (a figure's box).
    text_start = models.IntegerField(null=True, blank=True)
    text_end = models.IntegerField(null=True, blank=True)

    class Meta(OwnedModel.Meta):
        ordering = ["page__number", "order"]
        constraints = [
            # Either there is a box and it is a unit-square box, or there is none at all.
            models.CheckConstraint(
                condition=Q(x0__isnull=True, x1__isnull=True)
                | Q(x0__gte=0, x1__lte=1, x0__lte=models.F("x1")),
                name="region_x_in_unit",
            ),
            models.CheckConstraint(
                condition=Q(y0__isnull=True, y1__isnull=True)
                | Q(y0__gte=0, y1__lte=1, y0__lte=models.F("y1")),
                name="region_y_in_unit",
            ),
        ]

    @staticmethod
    def example() -> PageRegion:
        node = Node.example()
        page = Page(content=node.content, number=1, width=612.0, height=792.0)
        return PageRegion(
            node=node,
            page=page,
            x0=0.1,
            y0=0.1,
            x1=0.6,
            y1=0.15,
            order=0,
            text_start=0,
            text_end=len("Rent, 1200"),
            words=[[0.1, 0.1, 0.3, 0.15, 0, 5, 0.98], [0.32, 0.1, 0.6, 0.15, 6, 10, 0.91]],
            conf_stats=ConfStats.of([0.98, 0.91]),
        )

    @property
    def located(self) -> bool:
        """Whether this region knows *where* on the page it is, not only that it is there."""
        return self.box() is not None

    def box(self) -> tuple[float, float, float, float] | None:
        """The envelope, or None for a region that only says which page it is on."""
        if self.x0 is None or self.y0 is None or self.x1 is None or self.y1 is None:
            return None
        return (self.x0, self.y0, self.x1, self.y1)

    def ring(self) -> list[Point] | None:
        if self.polygon:
            return [(float(x), float(y)) for x, y in self.polygon]
        box = self.box()
        if box is None:
            return None
        x0, y0, x1, y1 = box
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    def contains(self, x: float, y: float) -> bool:
        box = self.box()
        ring = self.ring()
        if box is None or ring is None:
            return False
        x0, y0, x1, y1 = box
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        return point_in_ring(ring, x, y) if self.polygon else True

    def area(self) -> float:
        """The area a `hit()` compares; a region with no box never wins one."""
        ring = self.ring()
        return ring_area(ring) if ring is not None else float("inf")

    def word_list(self) -> list[Word]:
        return [Word(*entry) for entry in (self.words or [])]

    def text(self) -> str:
        if self.text_start is None or self.text_end is None:
            return ""
        return self.node.content.text[self.text_start : self.text_end]

    def __str__(self) -> str:
        return f"region {self.order} of node {self.node_id}"


# --- Work in flight ---------------------------------------------------------------------------


class PageState(PydanticModel):
    """One page an extractor has read: what a resumed run replays instead of asking again."""

    model_config = ConfigDict(extra="forbid")

    number: int
    #: The page in the extractor's own units — pdfium points, not the model's grid.
    width: float | None = None
    height: float | None = None
    #: The reading itself, as semantic page HTML.
    html: str


class ExtractionState(PydanticModel):
    """**The state of one extraction, in one object.** Every thread of a run shares it, it is
    stored as it grows and loaded again by the next run, and a run resumes from what it holds.

    It is deliberately one document rather than a row per page: a run is one computation, and
    "what has this computation got so far" should be one thing to read, write, log and reason
    about — not a query. It may be large; a hundred-page scan is a few hundred kilobytes of
    HTML, which is what one page image costs.

    **Evolving it**: new fields need a default so a stored state still loads, and anything
    that changes what an existing field *means* is a `SCHEMA_VERSION` bump — a state the
    current code would misread is discarded rather than migrated (`snapshot.load_draft`).
    Nothing in here is precious: it is scratch, and re-reading the document is always correct,
    only slower and dearer.
    """

    model_config = ConfigDict(extra="forbid")

    #: Bump when a field changes meaning; stored states from an older one are discarded.
    SCHEMA_VERSION: ClassVar[int] = 2

    schema_version: int = SCHEMA_VERSION
    #: The pages already read, by page number. A page that failed is not in here — reading it
    #: again is exactly what the next run is for.
    pages: dict[int, PageState] = Field(default_factory=dict)


# Deliberately not `@tracked` (`apps/core/history.py::HISTORY_EXEMPT` says why).
class DocumentContentDraft(OwnedModel):
    """The `ExtractionState` of one document + extractor, so a run can be resumed.

    An OCR run is one paid request per page and can take half an hour; a network error on page
    27 must not throw away the first 26. The run keeps its state here as it grows, and the next
    run over the same bytes with the same extractor starts from it — which is what makes "Read
    again" after a failure cheap rather than a second full bill.

    One row per (document, blob, extractor), holding one state object. Writing a snapshot
    discards it (`snapshot.discard_draft`): from then on the run's `raw_output` is *the* record
    of what the extractor said, and this was only scratch.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="drafts")
    #: The bytes that were read. A re-uploaded source invalidates nothing — it simply stops
    #: matching, and the document is read from scratch.
    blob = models.ForeignKey(Blob, on_delete=models.CASCADE, related_name="+")
    extractor = models.ForeignKey(Extractor, on_delete=models.CASCADE, related_name="+")
    state = SchemaField(ExtractionState, default=ExtractionState)

    class Meta(OwnedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["document", "blob", "extractor"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_active_content_draft",
            )
        ]

    @staticmethod
    def example() -> DocumentContentDraft:
        document = Document.example()
        return DocumentContentDraft(
            document=document,
            blob=document.source_blob,
            extractor=Extractor.example(),
            state=ExtractionState(
                pages={1: PageState(number=1, width=612.0, height=792.0, html="<p>Rent, 1200</p>")}
            ),
        )

    def __str__(self) -> str:
        return f"draft of {self.document_id} ({len(self.state.pages)} pages read)"
