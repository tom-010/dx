"""The write path of a document: blobs, the snapshot builder, the current flip, one run.

    blob = store_blob(owner_id, upload, "application/pdf")       # content-addressed, deduplicated
    content = start_extraction(document)                          # a PENDING row + a task
    run_extraction(content.pk)                                    # the task: RUNNING → rows → flip
    extract_now(document, PlainTextStrategy())                    # the same, inline (shell, tests)

A run calls one `ExtractionStrategy` (`apps/documents/strategies.py`): it takes the document
and returns the snapshot. `write_snapshot()` is what its `snapshot()` helper lands in — the
contract every extraction tree has to meet, and the only writer of `Page`, `Node` and
`PageRegion` rows:

1. **Plan** the tree: pre-order `nid`s from 1, materialized `path`s from sibling order, and the
   plain-text projection — leaves joined by `"\\n\\n"`, every node's `[text_start, text_end)`
   in that one string, ancestors enclosing descendants.
2. **Render** the html from the tree, **sanitize** it with nh3 (the tag allowlist is the type
   vocabulary), and only *then* **measure** `html_start`/`html_end` on the exact string that is
   stored — never on anything that is mutated afterwards.
3. **Roll up** confidence bottom-up: words → region → node subtree → page → content, by plain
   addition of `ConfStats`. **Date** the tree (`dating.date_snapshot`): datelines, page
   envelopes, interpolation, aggregation, inheritance — before the html is rendered, so dated
   tags carry `data-date` and the offsets are measured afterwards.
4. **Check** the invariants (`_check`): a region's text range lies within its node's, a child's
   ranges within its parent's, and every html slice starts with `<tag`. Then write everything in
   **one transaction** — the content row with its terminal status, and the pages, nodes and
   regions with `bulk_create` (nodes level by level, because a child needs its parent's id).

After a terminal status the snapshot is frozen. `switch_current()` is the one place
`is_current` and `Document.current_content` change: its own small transaction, the document
row locked, the `content_switched` signal sent inside it.

Re-dating (a better dating stage, a reprojection from the extractor's native output) is a
rebuild: `start_extraction(document, from_raw=True)` queues a run whose PENDING row already
points at the previous `raw_output`, and a strategy that can `reproject()` from it skips the
extractor (`rebuild_source()`); the result is a new snapshot and a normal flip.

Offsets are Python codepoint offsets, end-exclusive. JavaScript counts UTF-16 units: a
frontend that highlights must convert at the API boundary, not in the client.
"""

from __future__ import annotations

import hashlib
import html
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING

import nh3
import structlog
from django.core.files.base import ContentFile, File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.core.history import history_context
from apps.core.lineage import deriving
from apps.documents import dating
from apps.documents.dating import DateEstimate
from apps.documents.extraction import ExtractedNode, ExtractedPage, ExtractedRegion, Extraction
from apps.documents.models import (
    ALLOWED_TAGS,
    CELL_ATTRIBUTES,
    HEADINGS,
    NODE_ATTRIBUTES,
    PATH_MAX_LENGTH,
    PATH_SEPARATOR,
    PATH_WIDTH,
    Blob,
    ConfStats,
    Dated,
    Document,
    DocumentContent,
    ExtractionStatus,
    Extractor,
    Node,
    Page,
    PageRegion,
    Point,
    blob_upload_path,
    content_switched,
    envelope_of,
)

if TYPE_CHECKING:
    from apps.documents.strategies import ExtractionStrategy

log = structlog.get_logger(__name__)

#: The name of the step on the lineage node of every snapshot (`VersionedModel.save`).
OPERATION = "extract document"
#: Longest `DocumentContent.error` kept.
ERROR_LIMIT = 4000
#: `Node.title` column width.
TITLE_LIMIT = 1000
#: Coordinates this far outside [0, 1] are clamped; further out is an extractor bug.
COORDINATE_SLACK = 0.01
HASH_CHUNK = 1 << 20


class SnapshotError(Exception):
    """The extractor's output does not fit the snapshot contract; the run is FAILED."""


# --- Blobs -------------------------------------------------------------------------------------


def sha256_of(file: File[bytes]) -> tuple[str, int]:
    """Hash while streaming; the file is rewound afterwards."""
    digest = hashlib.sha256()
    size = 0
    for chunk in file.chunks(HASH_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    file.seek(0)
    return digest.hexdigest(), size


def store_blob(owner_id: uuid.UUID, file: File[bytes], mime_type: str) -> Blob:
    """The tenant's blob for these bytes: the existing row, or a new one.

    Same bytes twice ⇒ one row and one object: the sha256 is the key
    (`blob_upload_path`), and an object that already exists is referenced, never rewritten.
    """
    sha, size = sha256_of(file)
    existing = Blob.objects.filter(owner_id=owner_id, sha256=sha).first()
    if existing is not None:
        return existing
    key = blob_upload_path(Blob(sha256=sha, owner_id=owner_id), "")
    content: File[bytes] | str = key if default_storage.exists(key) else file
    return Blob.create(
        operation=None,
        sources=[],
        owner_id=owner_id,
        sha256=sha,
        size=size,
        mime_type=mime_type,
        file=content,
    )


def store_bytes(owner_id: uuid.UUID, data: bytes, mime_type: str) -> Blob:
    """`store_blob` for bytes already in memory (an extractor's raw output)."""
    return store_blob(owner_id, ContentFile(data, name="blob"), mime_type)


# --- Extractor rows and starting a run ----------------------------------------------------------


def extractor_row(strategy: ExtractionStrategy) -> Extractor:
    """The tenant's row for a strategy at its current version, created on first use."""
    found = Extractor.objects.filter(name=strategy.name, tool_version=strategy.tool_version).first()
    if found is not None:
        return found
    return Extractor.create(
        operation=None,
        sources=[],
        name=strategy.name,
        tool_version=strategy.tool_version,
        config=dict(strategy.config),
    )


class NothingToRebuildFrom(LookupError):
    """`from_raw=True`, but no snapshot of the document kept an extractor output."""


def _pending_row(
    document: Document, extractor: Extractor, *, from_raw: bool
) -> DocumentContent | None:
    """A PENDING row for one run, or the identical run that is already queued. With
    `from_raw` the row points at the latest snapshot's `raw_output`, which is what tells the
    run to reproject rather than extract."""
    raw: Blob | None = None
    if from_raw:
        source = document.current_content or document.latest_content()
        if source is None or source.raw_output is None:
            raise NothingToRebuildFrom("no snapshot of this document kept its extractor output")
        raw = source.raw_output
    queued = document.contents.filter(
        blob=document.source_blob,
        extractor=extractor,
        status__in=[ExtractionStatus.PENDING, ExtractionStatus.RUNNING],
    ).first()
    if queued is not None:
        return queued
    return DocumentContent.create(
        operation=None,
        sources=[],
        document=document,
        blob=document.source_blob,
        extractor=extractor,
        raw_output=raw,
        status=ExtractionStatus.PENDING,
    )


def start_extraction(
    document: Document, strategy: ExtractionStrategy | None = None, *, from_raw: bool = False
) -> DocumentContent | None:
    """`Document.reextract()`: a PENDING snapshot now, the task enqueued when the caller's
    transaction commits. Returns None when no strategy handles the file's MIME type, and the
    already queued row when an identical run (document, blob, extractor) is pending.
    `from_raw` rebuilds from the latest snapshot's `raw_output` (re-dating, re-projection)."""
    from apps.documents import strategies  # noqa: PLC0415 - strategies import this module

    if strategy is None:
        strategy = strategies.strategy_for_mime(document.source_blob.mime_type)
        if strategy is None:
            return None
    content = _pending_row(document, extractor_row(strategy), from_raw=from_raw)
    if content is None or not content.status == ExtractionStatus.PENDING:
        return content
    from apps.documents.tasks import extract_content  # noqa: PLC0415 - tasks import this module

    owner_id, content_id = document.owner_id, content.pk
    transaction.on_commit(lambda: extract_content.delay(owner_id, content_id))
    return content


# --- One run -------------------------------------------------------------------------------------

#: The queued row the running strategy is expected to fill (`write_extraction` adopts it).
_running: ContextVar[DocumentContent | None] = ContextVar("running_content", default=None)


def rebuild_source() -> tuple[bytes, str] | None:
    """The extractor output the running strategy may rebuild from, as `(bytes, mime type)`:
    set when the run was queued with `from_raw=True`. `TreeStrategy.extract` consults it; a
    hand-rolled strategy may too."""
    content = _running.get()
    if content is None or content.raw_output is None:
        return None
    return content.raw_output.read_bytes(), content.raw_output.mime_type


def run_extraction(
    content_id: uuid.UUID, strategy: ExtractionStrategy | None = None
) -> DocumentContent:
    """The task body: RUNNING, the strategy, the flip — or FAILED with the error.

    The strategy is the registered one the row's `Extractor` names, unless the caller hands
    one in (`extract_now`; an unregistered strategy in a test). Idempotent for a redelivered
    task: a row that is already terminal is returned as it is. Runs inside the tenant context
    the task provides.
    """
    from apps.documents import strategies  # noqa: PLC0415 - strategies import this module

    content = DocumentContent.objects.select_related("blob", "extractor", "document").get(
        pk=content_id
    )
    if content.is_terminal:
        return content
    content.status = ExtractionStatus.RUNNING
    content.started_at = timezone.now()
    content.save(operation=None, sources=[], update_fields=["status", "started_at"])
    token = _running.set(content)
    try:
        if strategy is None:
            strategy = strategies.strategy_named(content.extractor.name)
        result = strategy.extract(content.document)
        if not result.is_terminal or result.document_id != content.document_id:
            raise SnapshotError(
                f"{strategy} returned a snapshot that is {result.status}, not terminal, "
                "or belongs to another document"
            )
    except Exception as exc:  # noqa: BLE001 - the job boundary: the row records whatever failed
        error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
        log.error("extraction_failed", content_id=str(content.pk), error=error)
        content.status = ExtractionStatus.FAILED
        content.error = error
        content.finished_at = timezone.now()
        content.save(operation=None, sources=[], update_fields=["status", "error", "finished_at"])
        return content
    finally:
        _running.reset(token)
    if result.pk != content.pk:
        # The strategy built its own row rather than filling the queued one: retire the
        # placeholder so it does not stay PENDING forever.
        content.soft_delete()
    switch_current(content.document, result)
    return result


def extract_now(
    document: Document, strategy: ExtractionStrategy | None = None, *, from_raw: bool = False
) -> DocumentContent:
    """Run a strategy inline and make the result current — a shell or a test, not a request
    (a request queues; `Document.reextract`)."""
    from apps.documents import strategies  # noqa: PLC0415 - strategies import this module

    if strategy is None:
        strategy = strategies.strategy_for_mime(document.source_blob.mime_type)
        if strategy is None:
            raise SnapshotError(f"no strategy handles {document.source_blob.mime_type!r}")
    content = _pending_row(document, extractor_row(strategy), from_raw=from_raw)
    assert content is not None
    result = run_extraction(content.pk, strategy)
    # The run flipped another instance of the row; keep the caller's facade in step.
    document.refresh_from_db(fields=["current_content", "meta", "thumbnail", "version", "modified"])
    return result


def write_extraction(
    document: Document, strategy: ExtractionStrategy, extraction: Extraction
) -> DocumentContent:
    """`ExtractionStrategy.snapshot()`: the tree into rows, on the queued row of this run when
    there is one (and it is this document's), else on a new one."""
    content = _running.get()
    if content is None or content.document_id != document.pk:
        content = DocumentContent.create(
            operation=None,
            sources=[],
            document=document,
            blob=document.source_blob,
            extractor=extractor_row(strategy),
            status=ExtractionStatus.RUNNING,
            started_at=timezone.now(),
        )
    raw = (
        store_bytes(content.owner_id, extraction.raw, extraction.raw_mime)
        if extraction.raw is not None
        else content.raw_output  # a rebuild keeps what it was rebuilt from
    )
    write_snapshot(content, extraction, raw_output=raw)
    return content


# --- The builder ----------------------------------------------------------------------------------


@dataclass
class _Region:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    ring: list[Point] | None
    text_start: int | None
    text_end: int | None
    words: list[list[float | int | None]] | None
    conf: ConfStats | None
    detect_conf: float | None


@dataclass
class _Planned:
    """A node as it will be written; `row` once it has been."""

    item: ExtractedNode
    nid: int
    path: str
    order: int
    depth: int
    parent: _Planned | None
    children: list[_Planned] = field(default_factory=list)
    text_start: int = -1
    text_end: int = -1
    html_start: int = -1
    html_end: int = -1
    pages: set[int] = field(default_factory=set)
    regions: list[_Region] = field(default_factory=list)
    conf: ConfStats | None = None
    date: DateEstimate | None = None
    row: Node | None = None

    @property
    def tag(self) -> str:
        return self.item.tag

    @property
    def positioned(self) -> bool:
        return self.text_start >= 0

    def own_text(self) -> str:
        """A leaf's text: the paragraph, or a table's cells (tab/newline separated)."""
        if self.item.rows is not None:
            return "\n".join("\t".join(row) for row in self.item.rows)
        return self.item.text

    def level(self) -> int | None:
        if self.item.level is not None:
            return self.item.level
        return int(self.tag[1]) if self.tag in HEADINGS else None

    def title(self, text: str) -> str | None:
        if self.tag in HEADINGS:
            return text[self.text_start : self.text_end][:TITLE_LIMIT]
        if self.tag == "section":
            heading = next((c for c in self.children if c.tag in HEADINGS), None)
            if heading is not None:
                return text[heading.text_start : heading.text_end][:TITLE_LIMIT]
        return None


@dataclass
class _PlannedPage:
    item: ExtractedPage
    number: int
    date: DateEstimate | None = None


def plan(extraction: Extraction) -> tuple[list[_Planned], str]:
    """The tree in pre-order, with nids, paths and text offsets, plus the text itself."""
    nodes: list[_Planned] = []
    parts: list[str] = []
    cursor = 0

    def walk(item: ExtractedNode, parent: _Planned | None, depth: int, order: int) -> _Planned:
        nonlocal cursor
        if item.tag not in ALLOWED_TAGS:
            raise SnapshotError(f"<{item.tag}> is not in the tag vocabulary")
        if item.children and (item.text or item.rows is not None):
            raise SnapshotError(f"<{item.tag}> #{len(nodes) + 1} is both a leaf and a container")
        if order >= 10**PATH_WIDTH:
            raise SnapshotError(f"more than {10**PATH_WIDTH} siblings under one node")
        segment = f"{order + 1:0{PATH_WIDTH}d}"
        path = f"{parent.path}{PATH_SEPARATOR}{segment}" if parent else segment
        if len(path) > PATH_MAX_LENGTH:
            raise SnapshotError("the tree is nested too deeply for a materialized path")
        node = _Planned(
            item=item, nid=len(nodes) + 1, path=path, order=order, depth=depth, parent=parent
        )
        nodes.append(node)
        if item.children:
            for index, child in enumerate(item.children):
                node.children.append(walk(child, node, depth + 1, index))
            placed = [c for c in node.children if c.positioned]
            if placed:
                node.text_start = min(c.text_start for c in placed)
                node.text_end = max(c.text_end for c in placed)
        else:
            if parts:
                parts.append("\n\n")
                cursor += 2
            own = node.own_text()
            node.text_start = cursor
            parts.append(own)
            cursor += len(own)
            node.text_end = cursor
        return node

    for index, item in enumerate(extraction.nodes):
        walk(item, None, 0, index)
    text = "".join(parts)
    _place_empty(nodes, [n for n in nodes if n.parent is None], len(text))
    return nodes, text


def _place_empty(nodes: list[_Planned], top: list[_Planned], end: int) -> None:
    """A subtree without any leaf gets a zero-width range at the nearest leaf boundary among
    its siblings — inside its parent's range, so ancestors still enclose descendants."""

    def settle(siblings: list[_Planned], fallback: int) -> None:
        for index, node in enumerate(siblings):
            if node.positioned:
                continue
            after = next((s for s in siblings[index + 1 :] if s.positioned), None)
            before = next((s for s in reversed(siblings[:index]) if s.positioned), None)
            if after is not None:
                point = after.text_start
            elif before is not None:
                point = before.text_end
            else:
                point = fallback
            node.text_start = node.text_end = point

    settle(top, end)
    for node in nodes:
        if node.children:
            settle(node.children, node.text_start)


def _unit(value: float, what: str) -> float:
    if value < -COORDINATE_SLACK or value > 1 + COORDINATE_SLACK:
        raise SnapshotError(f"{what} coordinate {value} is outside [0, 1]")
    return min(max(value, 0.0), 1.0)


def _region(node: _Planned, item: ExtractedRegion, pages: set[int], text: str) -> _Region:
    if item.page not in pages:
        raise SnapshotError(
            f"node #{node.nid} has a region on page {item.page}, which the extraction has no "
            "page for"
        )
    ring: list[Point] | None = None
    if item.ring is not None:
        if len(item.ring) < 3:
            raise SnapshotError(f"node #{node.nid}: a polygon needs at least 3 points")
        ring = [(_unit(x, "polygon x"), _unit(y, "polygon y")) for x, y in item.ring]
        x0, y0, x1, y1 = envelope_of(ring)
    elif item.envelope is not None:
        x0, y0, x1, y1 = (_unit(v, "envelope") for v in item.envelope)
    else:
        raise SnapshotError(f"node #{node.nid}: a region needs a polygon or an envelope")
    if x0 > x1 or y0 > y1:
        raise SnapshotError(f"node #{node.nid}: envelope ({x0}, {y0}, {x1}, {y1}) is inverted")
    length = node.text_end - node.text_start
    start = end = None
    if item.span is not None:
        rel_start, rel_end = item.span
        if not 0 <= rel_start <= rel_end <= length:
            raise SnapshotError(
                f"node #{node.nid}: region span {item.span} outside its text (0..{length})"
            )
        start, end = node.text_start + rel_start, node.text_start + rel_end
    words = None
    if item.words is not None:
        words = []
        for word in item.words:
            if not 0 <= word.start <= word.end <= length:
                raise SnapshotError(
                    f"node #{node.nid}: word span {word.start}..{word.end} outside its text "
                    f"(0..{length})"
                )
            words.append(
                [
                    _unit(word.x0, "word x"),
                    _unit(word.y0, "word y"),
                    _unit(word.x1, "word x"),
                    _unit(word.y1, "word y"),
                    node.text_start + word.start,
                    node.text_start + word.end,
                    word.conf,
                ]
            )
    return _Region(
        page=item.page,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        ring=ring,
        text_start=start,
        text_end=end,
        words=words,
        conf=ConfStats.of(word.conf for word in item.words) if item.words else None,
        detect_conf=item.detect_conf,
    )


def place(nodes: list[_Planned], extraction: Extraction, text: str) -> None:
    """Regions into the global coordinate space, then pages and confidence bottom-up."""
    numbers = {page.number for page in extraction.pages}
    if len(numbers) != len(extraction.pages) or any(n < 1 for n in numbers):
        raise SnapshotError("page numbers must be unique and start at 1")
    for node in nodes:
        node.regions = [_region(node, item, numbers, text) for item in node.item.regions]
    for node in reversed(nodes):  # children come after their parent in pre-order
        node.pages = {r.page for r in node.regions}.union(*(c.pages for c in node.children))
        node.conf = ConfStats.merge(
            [r.conf for r in node.regions] + [c.conf for c in node.children]
        )


def render(nodes: list[_Planned]) -> str:
    out: list[str] = []
    for node in nodes:
        if node.parent is None:
            _render(node, out)
    return "".join(out)


def _render(node: _Planned, out: list[str]) -> None:
    item = node.item
    attrs = f' data-nid="{node.nid}"'
    if node.pages:
        attrs += f' data-pages="{",".join(str(p) for p in sorted(node.pages))}"'
    if node.date is not None:
        attrs += f' data-date="{html.escape(node.date.date.edtf, quote=True)}"'
    out.append(f"<{item.tag}{attrs}>")
    if item.table_html is not None:
        out.append(item.table_html)  # the extractor's own cell markup; nh3 checks it
    elif item.rows is not None:
        rows = item.rows
        if item.header and rows:
            out.append("<thead><tr>")
            out.extend(f"<th>{html.escape(cell, quote=False)}</th>" for cell in rows[0])
            out.append("</tr></thead>")
            rows = rows[1:]
        out.append("<tbody>")
        for row in rows:
            out.append("<tr>")
            out.extend(f"<td>{html.escape(cell, quote=False)}</td>" for cell in row)
            out.append("</tr>")
        out.append("</tbody>")
    elif node.children:
        for child in node.children:
            _render(child, out)
    else:
        if item.tag == "pre":
            out.append("\n")  # the parser drops one newline after <pre>; keep the text's own
        out.append(html.escape(item.text, quote=False))
    out.append(f"</{item.tag}>")


_ATTRIBUTES = {tag: set(NODE_ATTRIBUTES) for tag in ALLOWED_TAGS}
_ATTRIBUTES["td"] |= CELL_ATTRIBUTES
_ATTRIBUTES["th"] |= CELL_ATTRIBUTES


def sanitize(raw: str) -> str:
    """The allowlist is the vocabulary: any other tag or attribute is stripped here, before
    the offsets are measured."""
    return nh3.clean(raw, tags=set(ALLOWED_TAGS), attributes=_ATTRIBUTES)


class _Locator(HTMLParser):
    """Finds `[start, end)` of every `data-nid` element in the final string. The parser
    reports `(line, column)`; a table of line starts turns that into codepoint offsets."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self.text = text
        self.line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        self.open: list[tuple[str, int | None, int]] = []
        self.spans: dict[int, tuple[int, int]] = {}

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        nid = next((value for key, value in attrs if key == "data-nid"), None)
        self.open.append((tag, int(nid) if nid else None, self._offset()))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.open.pop()

    def handle_endtag(self, tag: str) -> None:
        while self.open:
            open_tag, nid, start = self.open.pop()
            if open_tag == tag:
                end = self._offset() + len(tag) + 3  # "</tag>"
                if self.text[end - len(tag) - 3 : end] != f"</{tag}>":
                    raise SnapshotError(f"cannot measure the end of <{tag}> at {start}")
                if nid is not None:
                    self.spans[nid] = (start, end)
                return


def locate(clean: str) -> dict[int, tuple[int, int]]:
    locator = _Locator(clean)
    locator.feed(clean)
    locator.close()
    return locator.spans


def _check(nodes: list[_Planned], clean: str, text: str) -> None:
    """The invariants, on what is about to be written (the same checks `verify_snapshot`
    runs on what was)."""
    for node in nodes:
        if not clean[node.html_start :].startswith(f"<{node.tag}"):
            raise SnapshotError(f"node #{node.nid}: html slice does not start with <{node.tag}>")
        if not 0 <= node.text_start <= node.text_end <= len(text):
            raise SnapshotError(f"node #{node.nid}: text range outside the text")
        parent = node.parent
        if parent is not None and not (
            parent.html_start <= node.html_start
            and node.html_end <= parent.html_end
            and parent.text_start <= node.text_start
            and node.text_end <= parent.text_end
        ):
            raise SnapshotError(f"node #{node.nid} is not enclosed by its parent #{parent.nid}")
        for region in node.regions:
            if region.text_start is not None and region.text_end is not None:
                inside = node.text_start <= region.text_start <= region.text_end <= node.text_end
                if not inside:
                    raise SnapshotError(f"node #{node.nid}: a region's text range leaves the node")


def _dated[RowT: Dated](row: RowT, estimate: DateEstimate | None) -> RowT:
    """The date columns of a bulk-created row — `bulk_create` skips `save()`, so the check
    that guards every other write runs here."""
    row.set_date(estimate)
    row.check_date()
    return row


@dataclass
class Built:
    """A snapshot before it is written: the strings, the planned rows, the dating report."""

    nodes: list[_Planned]
    pages: list[_PlannedPage]
    regions: list[_Region]
    text: str
    html: str
    report: dating.DatingReport
    page_conf: dict[int, ConfStats | None]
    stats: dict[str, object]


def build(
    extraction: Extraction, *, hint: str | None = None, metadata_date: str | None = None
) -> Built:
    """The pure half of `write_snapshot`: plan, date, render, sanitize, measure, check —
    everything that needs no database. `manage.py ocr assemble` writes its result to files;
    `write_snapshot` to rows."""
    nodes, text = plan(extraction)
    place(nodes, extraction, text)
    planned_pages = [_PlannedPage(item=page, number=page.number) for page in extraction.pages]
    report = dating.date_snapshot(
        nodes, planned_pages, text, hint=hint, metadata_date=metadata_date
    )
    dated = dating.check_dating(nodes, planned_pages, report.content)
    if dated:
        raise SnapshotError("dating invariants: " + "; ".join(dated))
    clean = sanitize(render(nodes))
    spans = locate(clean)
    for node in nodes:
        try:
            node.html_start, node.html_end = spans[node.nid]
        except KeyError:
            raise SnapshotError(f"the sanitizer dropped node #{node.nid} <{node.tag}>") from None
    _check(nodes, clean, text)
    regions = [r for node in nodes for r in node.regions]
    page_conf = {
        page.number: ConfStats.merge(r.conf for r in regions if r.page == page.number)
        for page in extraction.pages
    }
    stats: dict[str, object] = {
        "pages": len(extraction.pages),
        "failed_pages": sorted(extraction.failed_pages),
        "nodes": len(nodes),
        "regions": len(regions),
        "words": sum(len(r.words or []) for r in regions),
        "html_chars": len(clean),
        "text_chars": len(text),
        "meta": extraction.meta,
        "dating": report.stats,
        **extraction.stats,
    }
    return Built(
        nodes=nodes,
        pages=planned_pages,
        regions=regions,
        text=text,
        html=clean,
        report=report,
        page_conf=page_conf,
        stats=stats,
    )


def payload(built: Built) -> list[dict[str, object]]:
    """The would-be `Node` and `PageRegion` rows of a build, as plain data (`nodes.json`)."""
    rows = []
    for node in built.nodes:
        rows.append(
            {
                "nid": node.nid,
                "path": node.path,
                "parent": node.parent.nid if node.parent is not None else None,
                "order": node.order,
                "tag": node.tag,
                "level": node.level(),
                "title": node.title(built.text),
                "source": str(node.item.source),
                "text_start": node.text_start,
                "text_end": node.text_end,
                "html_start": node.html_start,
                "html_end": node.html_end,
                "pages": sorted(node.pages),
                "date": (
                    {
                        "edtf": node.date.date.edtf,
                        "source": node.date.source.value,
                        "conf": node.date.conf,
                    }
                    if node.date is not None
                    else None
                ),
                "regions": [
                    {
                        "page": region.page,
                        "order": index,
                        "x0": region.x0,
                        "y0": region.y0,
                        "x1": region.x1,
                        "y1": region.y1,
                        "text_start": region.text_start,
                        "text_end": region.text_end,
                    }
                    for index, region in enumerate(node.regions)
                ],
            }
        )
    return rows


def write_snapshot(
    content: DocumentContent, extraction: Extraction, *, raw_output: Blob | None = None
) -> None:
    """Turn an extraction into the frozen rows of `content`, all in one transaction."""
    meta = content.document.meta if isinstance(content.document.meta, dict) else {}
    hint = meta.get("date_hint")
    created = extraction.meta.get("created")
    built = build(
        extraction,
        hint=str(hint) if hint else None,
        metadata_date=str(created) if created else None,
    )
    nodes, planned_pages, regions, report = built.nodes, built.pages, built.regions, built.report
    text, clean, page_conf = built.text, built.html, built.page_conf
    finished = timezone.now()
    started = content.started_at or finished
    stats = {**built.stats, "duration_s": round((finished - started).total_seconds(), 3)}
    owner = content.owner_id
    label = f"{content.extractor.name} {content.extractor.tool_version}"
    with (
        transaction.atomic(),
        history_context(OPERATION, extractor=label),
        deriving(content.blob, content.extractor),
    ):
        content.html = clean
        content.text = text
        content.conf_stats = ConfStats.merge(r.conf for r in regions)
        content.stats = stats
        content.raw_output = raw_output
        content.status = (
            ExtractionStatus.PARTIAL if extraction.failed_pages else ExtractionStatus.SUCCEEDED
        )
        content.finished_at = finished
        content.set_date(report.content)
        content.save(operation=None, sources=None)

        pages = Page.objects.bulk_create(
            _dated(
                Page(
                    owner_id=owner,
                    content=content,
                    number=page.number,
                    label=page.item.label,
                    width=page.item.width,
                    height=page.item.height,
                    meta=page.item.meta,
                    conf_stats=page_conf[page.number],
                    thumbnail=(
                        store_bytes(owner, page.item.thumbnail, "image/png")
                        if page.item.thumbnail is not None
                        else None
                    ),
                ),
                page.date,
            )
            for page in planned_pages
        )
        by_number = {page.number: page for page in pages}
        for depth in range(max((n.depth for n in nodes), default=-1) + 1):
            batch = [n for n in nodes if n.depth == depth]
            rows = Node.objects.bulk_create(
                _dated(
                    Node(
                        owner_id=owner,
                        content=content,
                        parent=n.parent.row if n.parent is not None else None,
                        nid=n.nid,
                        path=n.path,
                        order=n.order,
                        tag=n.tag,
                        level=n.level(),
                        title=n.title(text),
                        source=n.item.source,
                        conf_stats=n.conf,
                        html_start=n.html_start,
                        html_end=n.html_end,
                        text_start=n.text_start,
                        text_end=n.text_end,
                    ),
                    n.date,
                )
                for n in batch
            )
            for planned, row in zip(batch, rows, strict=True):
                planned.row = row
        PageRegion.objects.bulk_create(
            PageRegion(
                owner_id=owner,
                node=node.row,
                page=by_number[region.page],
                x0=region.x0,
                y0=region.y0,
                x1=region.x1,
                y1=region.y1,
                polygon=[[x, y] for x, y in region.ring] if region.ring is not None else None,
                words=region.words,
                conf_stats=region.conf,
                detect_conf=region.detect_conf,
                order=index,
                text_start=region.text_start,
                text_end=region.text_end,
            )
            for node in nodes
            for index, region in enumerate(node.regions)
        )


# --- The flip -------------------------------------------------------------------------------------


def switch_current(document: Document, new: DocumentContent) -> None:
    """Make `new` the document's current snapshot. The only writer of `is_current` and
    `Document.current_content`; `content_switched` is sent inside the transaction, so a
    receiver that raises keeps the old snapshot current."""
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        previous = locked.current_content
        if previous is not None and previous.pk == new.pk:
            return
        locked.contents.filter(is_current=True).exclude(pk=new.pk).update(is_current=False)
        new.is_current = True
        new.save(operation=None, sources=[], update_fields=["is_current"])
        locked.current_content = new
        extracted = new.stats.get("meta") if isinstance(new.stats, dict) else None
        if isinstance(extracted, dict):
            locked.meta = {**extracted, **locked.meta}
        if locked.thumbnail_id is None:
            first = new.pages.exclude(thumbnail=None).order_by("number").first()
            if first is not None:
                locked.thumbnail = first.thumbnail
        locked.save(
            operation="switch document content",
            sources=[new],
            update_fields=["current_content", "meta", "thumbnail"],
        )
        document.current_content = new
        document.meta = locked.meta
        document.thumbnail = locked.thumbnail
        document.version = locked.version
        content_switched.send(sender=Document, document=locked, content=new, previous=previous)


# --- Verification ---------------------------------------------------------------------------------


def verify_snapshot(content: DocumentContent) -> list[str]:
    """The §4.7 assertions against the rows as stored — empty when the snapshot is sound."""
    problems: list[str] = []
    nodes = list(content.nodes.order_by("nid"))
    by_pk = {node.pk: node for node in nodes}
    regions = list(PageRegion.objects.filter(node__content=content).select_related("page"))
    if content.conf_stats != ConfStats.merge(r.conf_stats for r in regions):
        problems.append("content.conf_stats != Σ regions")
    for page in content.pages.all():
        expected = ConfStats.merge(r.conf_stats for r in regions if r.page_id == page.pk)
        if page.conf_stats != expected:
            problems.append(f"page {page.number}: conf_stats != Σ its regions")
    for node in nodes:
        html_slice = content.html[node.html_start : node.html_end]
        if not html_slice.startswith(f"<{node.tag}") or not html_slice.endswith(f"</{node.tag}>"):
            problems.append(f"node #{node.nid}: html slice is not a <{node.tag}> element")
        if f'data-nid="{node.nid}"' not in html_slice[: html_slice.find(">") + 1]:
            problems.append(f"node #{node.nid}: html slice does not carry its data-nid")
        if node.parent_id is not None:
            parent = by_pk[node.parent_id]
            if not (parent.text_start <= node.text_start <= node.text_end <= parent.text_end):
                problems.append(f"node #{node.nid}: text range leaves parent #{parent.nid}")
            if not (parent.html_start <= node.html_start <= node.html_end <= parent.html_end):
                problems.append(f"node #{node.nid}: html range leaves parent #{parent.nid}")
        subtree = [r for r in regions if by_pk[r.node_id].path.startswith(node.path)]
        if node.conf_stats != ConfStats.merge(r.conf_stats for r in subtree):
            problems.append(f"node #{node.nid}: conf_stats != Σ subtree regions")
    for region in regions:
        node = by_pk[region.node_id]
        if region.text_start is not None and region.text_end is not None:
            if not (node.text_start <= region.text_start <= region.text_end <= node.text_end):
                problems.append(f"region {region.pk}: text range leaves node #{node.nid}")
        if region.conf_stats != ConfStats.of(w.conf for w in region.word_list()):
            problems.append(f"region {region.pk}: conf_stats != Σ words")
    problems += _verify_dates(content, nodes, by_pk)
    return problems


def _verify_dates(
    content: DocumentContent, nodes: list[Node], by_pk: dict[uuid.UUID, Node]
) -> list[str]:
    """`dating.check_dating` over the stored rows: containment, tight envelopes, round trips,
    and `data-date` on exactly the dated tags."""
    problems: list[str] = []
    envelope = content.date
    rows: list[Dated] = [content, *content.pages.all(), *nodes]
    for row in rows:
        try:
            row.check_date()
        except dating.InvalidDate as exc:
            problems.append(f"{row}: {exc}")
    for node in nodes:
        opening = node.html()[: node.html().find(">") + 1]
        if (node.date is not None) != ('data-date="' in opening):
            problems.append(f"node #{node.nid}: data-date does not match its date columns")
        if node.date is not None and f'data-date="{node.date.edtf}"' not in opening:
            problems.append(f"node #{node.nid}: data-date is not its date_edtf")
        if node.date is None:
            continue
        if envelope is not None and not envelope.contains(node.date):
            problems.append(
                f"node #{node.nid}: {node.date.edtf} leaves the content {envelope.edtf}"
            )
        if node.parent_id is not None:
            parent = by_pk[node.parent_id].date
            if parent is not None and not parent.contains(node.date):
                problems.append(
                    f"node #{node.nid}: {node.date.edtf} leaves its parent {parent.edtf}"
                )
        if node.date_source == dating.DateSource.AGGREGATED:
            children = [c.date for c in nodes if c.parent_id == node.pk and c.date is not None]
            if dating.UncertainDate.envelope(children) != node.date:
                problems.append(
                    f"node #{node.nid}: aggregated range is not its children's envelope"
                )
    for page in content.pages.all():
        if page.date is not None and envelope is not None and not envelope.contains(page.date):
            problems.append(
                f"page {page.number}: {page.date.edtf} leaves the content {envelope.edtf}"
            )
    return problems
