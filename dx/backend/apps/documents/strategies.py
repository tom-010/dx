"""Extraction strategies: how a document becomes a snapshot.

    class DoclingStrategy(ExtractionStrategy):
        name = "docling"
        tool_version = "2.48.0"

        def extract(self, document: Document) -> DocumentContent:
            result = convert(document.source_blob.read_bytes())   # whatever the tool does
            tree = Extraction(nodes=[...], pages=[...])           # apps/documents/extraction.py
            return self.snapshot(document, tree)                  # rows, offsets, confidence

    register(DoclingStrategy, "application/pdf", "image/tiff")   # the class, not an instance

`ExtractionStrategy` is the abstract base: **takes a `Document`, returns its `DocumentContent`**
— the snapshot, written and terminal. `self.snapshot(document, tree)` does everything the
brief's write-path contract asks for (`snapshot.write_snapshot`): the text projection, the
sanitized html, the offsets measured on the stored string, the confidence rollups and the rows
in one transaction. A strategy that only has to *parse* bytes into the tree extends
`TreeStrategy` and implements `parse()` instead.

The framework around a strategy (`apps/documents/snapshot.py`): `Document.reextract()` queues
a PENDING row and a task; the task marks it RUNNING, calls `extract()`, and makes the result
current — or records FAILED with the exception's message when `extract()` raises. So raise for
a whole-document failure; report pages that failed through `Extraction.failed_pages`.

The registry at the bottom is the whole "which strategy for which file" decision:
`strategy_for_mime()` at upload, `strategy_named()` when the task runs (the `Extractor` row a
snapshot points at is a strategy's `name` + `tool_version`). It holds **factories, not
instances** — the class is the factory, since `name`/`tool_version`/`config` are class
variables — so every run constructs its own and nothing is shared between two runs in one
worker. Built in: `plain-text`, `html`, `pypdf` (born-digital PDFs without OCR, so no
confidence), `gemini-ocr` (the default for PDFs: vision OCR for scans,
`apps/documents/ocr/`) and `tesseract` (opt-in, local and offline, real per-word confidence
but no structure).

`read_file(data, mime_type) -> Extraction` is the strategy proper and needs no database, so
any strategy runs against a file on disk: `manage.py extract FILE [--strategy NAME]` prints
what it read and writes nothing.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, ClassVar

import pypdf
import structlog

from apps.documents import extraction as tree
from apps.documents import pipeline
from apps.documents.dating import DateEstimate, DateSource, InvalidDate, UncertainDate
from apps.documents.extraction import (
    ExtractedNode,
    Extraction,
    ExtractionError,
    HtmlTree,
)
from apps.documents.models import Document, DocumentContent, PageState, StructureSource
from apps.documents.ocr import (
    assembly,
    gemini_client,
    page_html,
    render,
    run,
    tesseract,
)
from config.env import env

log = structlog.get_logger(__name__)


def page_state(page: assembly.PageInput) -> PageState:
    """A page the extractor read, as the run's state keeps it (`models.ExtractionState`)."""
    return PageState(
        number=page.number,
        width=page.width,
        height=page.height,
        html=page.html or "",
    )


def page_input(page: PageState) -> assembly.PageInput:
    """…and back, for a run that resumes from it. Size and thumbnail are filled in again from
    the file itself, so what is replayed is only what the extractor was paid for."""
    return assembly.PageInput(
        number=page.number,
        width=page.width,
        height=page.height,
        html=page.html,
    )


def snapshot_progress(current: int, total: int) -> None:
    """`snapshot.report_progress`, imported where it is used (snapshot imports this module)."""
    from apps.documents import snapshot  # noqa: PLC0415

    snapshot.report_progress(current, total)


class ExtractionStrategy(ABC):
    """Turn a document into a snapshot. Subclass, set `name` and `tool_version`, implement
    `extract()`; `(name, tool_version)` is the `Extractor` row every snapshot points at, so a
    new tool version means new snapshots."""

    name: ClassVar[str]
    tool_version: ClassVar[str]
    #: Recorded on the `Extractor` row — what a run was configured with.
    config: ClassVar[dict[str, Any]] = {}

    @abstractmethod
    def read_file(self, data: bytes, mime_type: str) -> Extraction:
        """**The strategy proper**: these bytes, as the extraction tree.

        No `Document`, no snapshot, no database — a file and a MIME type in, a tree out. That
        is what makes an extractor runnable against a file on disk (`manage.py extract`), and
        testable without a row. `extract()` below is this plus the write.

        The ambient run helpers a strategy may consult (`snapshot.run_state`,
        `rebuild_source`, `report_progress`) are null objects outside a run, so the same code
        path serves both callers.
        """

    def extract(self, document: Document) -> DocumentContent:
        """The database path: the tree, then the snapshot — written, terminal (SUCCEEDED or
        PARTIAL), not yet current. Raise for a whole-document failure."""
        blob = document.source_blob
        return self.snapshot(document, self.read_file(blob.read_bytes(), blob.mime_type))

    def snapshot(self, document: Document, extraction: Extraction) -> DocumentContent:
        """Write `extraction` as this strategy's snapshot of `document` and return it.

        The row is the one the task queued for this run when there is one, else a new one;
        the builder does the rest (`snapshot.write_snapshot`).
        """
        from apps.documents import snapshot  # noqa: PLC0415 - snapshot imports this module

        return snapshot.write_extraction(document, self, extraction)

    def __str__(self) -> str:
        return f"{self.name} {self.tool_version}"


class TreeStrategy(ExtractionStrategy):
    """A strategy that only has to parse the file's bytes into the extraction tree.

    A run queued as a rebuild (`snapshot.start_extraction(..., from_raw=True)`) offers the
    previous snapshot's extractor output; `reproject()` turns that back into a tree without
    running the extractor — implement it when the output holds enough to do so.
    """

    def read_file(self, data: bytes, mime_type: str) -> Extraction:
        return self.parse(data, mime_type)

    def extract(self, document: Document) -> DocumentContent:
        """Overridden only for the rebuild shortcut: a run queued `from_raw=True` reprojects
        the previous extractor output instead of parsing the file again."""
        from apps.documents import snapshot  # noqa: PLC0415 - snapshot imports this module

        blob = document.source_blob
        source = snapshot.rebuild_source()
        found = self.reproject(*source) if source is not None else None
        if found is None:
            found = self.parse(blob.read_bytes(), blob.mime_type)
        return self.snapshot(document, found)

    @abstractmethod
    def parse(self, data: bytes, mime_type: str) -> Extraction: ...

    def reproject(self, raw: bytes, mime_type: str) -> Extraction | None:
        """The tree from a stored extractor output; None (the default) means "parse again"."""
        return None


@dataclass
class PagesRead:
    """What stage 1 produced: the pages, the bytes a rebuild replays, the file's own metadata."""

    pages: list[pipeline.PageHtml]
    raw: bytes | None = None
    raw_mime: str = "application/json"
    meta: dict[str, Any] = field(default_factory=dict)


class PagedStrategy(ExtractionStrategy):
    """A strategy that reads a file page by page and lets the pipeline do the rest.

    `read_pages()` is the only thing a subclass writes: the file in, one `PageHtml` per page
    out (semantic HTML, `data-box` where it knows, `data-furniture` and `data-continues` where
    it can tell). Everything after that — pruning, joining across pages, grouping, sections,
    the augmented document, the review — is `apps/documents/pipeline.py`, the same for every
    extractor.
    """

    @abstractmethod
    def read(self, data: bytes) -> PagesRead:
        """Stage 1: the file, page by page, as semantic HTML."""

    def date(self, html: str) -> dict[int, DateEstimate]:
        """A model's reading of when each node's information originates; empty by default."""
        return {}

    def name_document(self, html: str) -> str:
        """What to call the finished document; the pipeline falls back to its first heading."""
        return ""

    def read_file(self, data: bytes, mime_type: str) -> Extraction:
        read = self.read(data)
        extraction, html = pipeline.assemble(
            read.pages,
            meta=read.meta,
            raw=read.raw,
            raw_mime=read.raw_mime,
            name=self.name_document,
        )
        # The document is addressable now (every tag numbered), so a model can date it node by
        # node before the deterministic stage runs over the result.
        extraction.dates = self.date(html)
        return extraction


# --- Built-in strategies ------------------------------------------------------------------------


class PlainTextStrategy(TreeStrategy):
    """`text/plain`: every blank-line-separated block is a paragraph; no pages."""

    name = "plain-text"
    tool_version = "1"

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        return Extraction(
            nodes=[
                ExtractedNode(tag="p", text=block, source=StructureSource.EMBEDDED)
                for block in tree.paragraphs(tree.decode(data))
            ]
        )


class HtmlStrategy(TreeStrategy):
    """`text/html`: the source's own structure (`HtmlTree`), sanitized by the builder; no
    pages."""

    name = "html"
    tool_version = "1"

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        parser = HtmlTree()
        parser.feed(tree.decode(data))
        nodes = parser.finish()
        title = " ".join("".join(parser.title).split())
        return Extraction(nodes=nodes, meta={"title": title} if title else {})


def _chunks(records: list[Any]) -> list[tree.PdfChunk]:
    """The stored text runs of one page (`[text, x, y, size, font]`; the font is newer)."""
    return [
        tree.PdfChunk(
            text=str(record[0]),
            x=float(record[1]),
            y=float(record[2]),
            font_size=float(record[3]),
            font=str(record[4]) if len(record) > 4 else "",
        )
        for record in records
    ]


class PdfStrategy(PagedStrategy):
    """A **born-digital** PDF, read from its own text runs: no OCR, no network, no cost.

    Opt-in, and never the default for `application/pdf` (see `MIME_STRATEGIES`): this reads the
    text layer *inside* the file, which is the author's own words only when the file was
    generated rather than scanned. On a photocopy that layer is some scanner's OCR output and
    cannot be trusted, so a scan belongs to `gemini-ocr`, which reads the page image instead.

    A born-digital page carries no tags, so the structure comes from its geometry — line
    spacing tells paragraphs apart, font size and boldness mark headings, a bullet makes a
    list. What none of that can tell apart stays a paragraph, and joining across pages,
    grouping and sectioning are the pipeline's, not this strategy's. A page with no text layer
    at all comes out empty: a page row with no node. Confidence stays None — this text is
    read, not recognised.

    The raw output keeps every page's size and text runs (and the file's metadata), so a
    rebuild replays it without opening the PDF again.
    """

    name = "pypdf"
    #: The strategy's own reading of a page is part of its identity, not just the library's
    #: version: a snapshot from before the block detection must stay comparable.
    tool_version = f"{pypdf.__version__}+html1"

    def read(self, data: bytes) -> PagesRead:
        from apps.documents import snapshot  # noqa: PLC0415 - snapshot imports this module

        source = snapshot.rebuild_source()
        payload = _payload_of(source[0]) if source is not None else self._read_pdf(data)
        if payload is None:
            payload = self._read_pdf(data)
        return self._pages(payload)

    @staticmethod
    def _read_pdf(data: bytes) -> dict[str, Any]:
        """Every page's size and text runs — the payload a rebuild replays from."""
        try:
            reader = pypdf.PdfReader(BytesIO(data))
            if reader.is_encrypted:
                reader.decrypt("")
            count = len(reader.pages)
            info = reader.metadata
        except Exception as exc:
            raise ExtractionError(f"not a readable PDF: {type(exc).__name__}: {exc}") from exc
        records: list[dict[str, Any]] = []
        for index in range(count):
            number = index + 1
            snapshot_progress(number, count)
            try:
                page = reader.pages[index]
                width = float(page.mediabox.width)
                height = float(page.mediabox.height)
                chunks = tree.pdf_chunks(page)
            except Exception:  # noqa: BLE001 - one bad page must not fail the document
                records.append({"number": number, "failed": True})
                continue
            records.append(
                {
                    "number": number,
                    "width": width,
                    "height": height,
                    "chunks": [[c.text, c.x, c.y, c.font_size, c.font] for c in chunks],
                }
            )
        meta: dict[str, Any] = {}
        if info is not None:
            for key, value in (("title", info.title), ("author", info.author)):
                if value:
                    meta[key] = str(value)
            created = _creation_date(info)
            if created is not None:
                meta["created"] = created
        return {"pages": records, "meta": meta}

    @staticmethod
    def _pages(payload: dict[str, Any]) -> PagesRead:
        read: list[tuple[pipeline.PageHtml, list[tree.PdfChunk]]] = []
        for record in payload["pages"]:
            number = int(record["number"])
            if record.get("failed"):
                read.append((pipeline.PageHtml(number=number, failed=True), []))
                continue
            width, height = float(record["width"]), float(record["height"])
            page = pipeline.PageHtml(number=number, width=width, height=height)
            read.append((page, _chunks(record["chunks"])))

        # One body size for the whole document, so a heading is a heading on every page and a
        # page that happens to be all letterhead does not redefine "normal".
        lines = [line for _, chunks in read for line in tree.pdf_lines(chunks)]
        body = tree.body_size(lines)
        for page, chunks in read:
            if page.width is not None and page.height is not None:
                blocks = tree.pdf_page_blocks(chunks, page.number, page.width, page.height, body)
                page.html = pipeline.page_html(blocks, page.number)
        stored = payload.get("meta")
        meta: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        return PagesRead(
            pages=[page for page, _ in read],
            raw=json.dumps(payload).encode(),
            meta=meta,
        )


def _payload_of(raw: bytes) -> dict[str, Any] | None:
    """A stored PDF reading, if that is what these bytes are."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        return None
    first = payload["pages"][0] if payload["pages"] else {}
    return payload if "chunks" in first or first.get("failed") else None


def _creation_date(info: pypdf.DocumentInformation) -> str | None:
    """The PDF's creation date as an ISO day, if the file carries a readable one."""
    try:
        created = info.creation_date
    except Exception:  # noqa: BLE001 - a malformed date string is not a failure
        return None
    return created.date().isoformat() if created is not None else None


class GeminiOcrStrategy(PagedStrategy):
    """Gemini-vision OCR (`apps/documents/ocr/`): every page image goes to the model once and
    comes back as semantic HTML, which the pipeline joins into one document.

    **This is what reads a PDF**, because the documents this app is for are photocopied scans:
    the only trustworthy reading of one is a reading of the page image. It costs a request per
    page and it sends those images to Google, which is a decision to take before real records
    go through it. A rebuild (`from_raw=True`) replays the stored per-page HTML without a
    single request.

    **A page is read once**: no review round, no repair call. An answer that does not parse
    cleanly is stored for what it is worth, with the problems recorded in `stats` — a snapshot
    says what it knows about itself.

    No per-word confidence exists here, so `conf_stats` stays NULL; the model is never asked
    to rate itself.
    """

    name = "gemini-ocr"
    #: Bump the number on any change to the prompt, the page contract or anything else in
    #: `config`: they are the extractor, and `snapshot.extractor_row` refuses a row that says
    #: otherwise.
    tool_version = "5"
    config: ClassVar[dict[str, Any]] = {
        "model": gemini_client.MODEL,
        "dpi": render.DPI,
        "prompt_sha256": gemini_client.prompt_sha256(),
        "schema_version": page_html.SCHEMA_VERSION,
    }

    def __init__(self, reader: gemini_client.PageReader | None = None) -> None:
        self._reader = reader

    def reader(self) -> gemini_client.PageReader:
        if self._reader is None:
            if not env.GEMINI_API_KEY:
                raise ExtractionError("GEMINI_API_KEY is not set (backend/.env)")
            self._reader = gemini_client.GeminiPageReader(
                env.GEMINI_API_KEY, model=str(self.config["model"])
            )
        return self._reader

    def read(self, data: bytes) -> PagesRead:
        from apps.documents import snapshot  # noqa: PLC0415 - snapshot imports this module

        # The state of this run — the pages an earlier attempt already read included. Every
        # page goes into it as it comes back, so a run that dies is resumed and not repeated.
        state = snapshot.run_state()

        def keep(page: assembly.PageInput, png: bytes | None) -> None:
            """The page that just came back, before spending money on the next one. A failed
            page is not kept: reading it again is the whole point of the next run."""
            if page.html is not None:
                state.record_page(page_state(page))

        source = snapshot.rebuild_source()
        if source is not None:
            inputs = assembly.pages_from_raw(source[0])
        else:
            total = render.page_count(data)
            inputs = []
            for page in run.read_document(
                data,
                self.reader(),
                dpi=int(self.config["dpi"]),
                thumbnails=True,
                # Fresh objects, not the state's own: the workers fill in each page's size and
                # thumbnail on what they are given, and the state is not theirs to write to.
                existing={number: page_input(p) for number, p in state.pages.items()},
                on_page=keep,
            ):
                inputs.append(page)
                # One request per page is the slow part of this strategy; say how far it got.
                snapshot.report_progress(len(inputs), total)
        if not inputs:
            raise ExtractionError("the PDF has no pages")
        if all(page.failed for page in inputs):
            raise ExtractionError(f"no page could be read: {inputs[0].error}")
        pages, problems = assembly.page_contents(inputs)
        return PagesRead(
            pages=pages,
            raw=assembly.raw_payload(inputs),
            meta={"ocr_problems": problems} if problems else {},
        )

    def name_document(self, html: str) -> str:
        """The last call of the run: what this document is, in one line."""
        started = time.monotonic()
        title = self.reader().name(html)
        log.info("ocr_named", title=title, s=round(time.monotonic() - started, 1))
        return title

    def date(self, html: str) -> dict[int, DateEstimate]:
        """One extra call for the whole document: when does each node's information originate.

        What comes back is `INFERRED` — a reading, not a statement — so a printed dateline
        still wins, and a date the parser cannot hold is dropped rather than stored.
        """
        started = time.monotonic()
        estimates: dict[int, DateEstimate] = {}
        answered = self.reader().date(html)
        log.info("ocr_dated", tags=len(answered), s=round(time.monotonic() - started, 1))
        for nid, (edtf, confidence) in answered.items():
            try:
                found = UncertainDate.parse(edtf)
            except InvalidDate:
                continue
            estimates[nid] = DateEstimate(
                found, DateSource.INFERRED, min(max(confidence, 0.0), 1.0)
            )
        return estimates


class TesseractStrategy(PagedStrategy):
    """`tesseract`: local, offline OCR — the counterpart to `gemini-ocr` on the same file.

    Two OCR strategies for one MIME type is what the registry is for, and they fail in opposite
    directions: Gemini reads a page the way a person does (headings, tables, the letterhead)
    and costs a call per page to a third party; tesseract runs on this machine, sends nothing
    anywhere, and reports no structure at all — every block is a paragraph, and the pipeline's
    own stages do what can be done from text alone.

    Opt in with `?strategy=tesseract` on `reextract`, or `manage.py extract FILE --strategy
    tesseract`. It is **not** a default for any MIME type: a scan read as unstructured
    paragraphs is a worse artifact than one read with its headings, and choosing it should be a
    decision (usually: no network, or no legal basis for sending the page anywhere).

    The adapter is `apps/documents/ocr/tesseract.py`; this class is the page loop around it.
    """

    name = "tesseract"
    #: The engine *major* version this adapter targets, not the build on this machine. It has
    #: to be readable without running anything — `Extractor` rows and the strategy listing both
    #: take it off the class — and shelling out at import time would make a missing binary
    #: break Django's startup rather than this one run. The exact build (`5.3.4`) goes into the
    #: snapshot's `meta`, where it belongs: a fact about the run, not about the extractor.
    tool_version = "5"
    #: `lang` is a *preference*, not a setting: the first pack that is installed wins
    #: (`tesseract.pick_language`). German leads because that is what this corpus is.
    config: ClassVar[dict[str, Any]] = {"dpi": render.DPI, "lang": ["deu", "eng"], "psm": 1}

    def read(self, data: bytes) -> PagesRead:
        """Every page rendered and read, in order. A missing binary or language pack fails the
        whole document — that is a broken installation, not a page that could not be read."""
        tesseract.require_binary()
        language = tesseract.pick_language(list(self.config["lang"]))
        total = render.page_count(data)
        if total == 0:
            raise ExtractionError("the PDF has no pages")
        pages: list[pipeline.PageHtml] = []
        raw: dict[str, str] = {}
        for rendered in render.render_pages(data, dpi=int(self.config["dpi"])):
            image = rendered.image
            blocks, tsv = tesseract.read_page(
                render.png_bytes(image), image.width, image.height, language=language
            )
            raw[str(rendered.number)] = tsv
            pages.append(
                pipeline.PageHtml(
                    number=rendered.number,
                    html=pipeline.page_html(blocks, rendered.number),
                    width=rendered.width,
                    height=rendered.height,
                    thumbnail=render.thumbnail_bytes(image),
                )
            )
            snapshot_progress(len(pages), total)
        if not any(page.html for page in pages):
            raise ExtractionError("tesseract read no text on any page")
        return PagesRead(
            pages=pages,
            # The TSV per page: enough to rebuild the artifact without re-running the engine.
            raw=json.dumps(raw).encode(),
            meta={"tesseract": tesseract.version(), "lang": language},
        )


# --- Registry ------------------------------------------------------------------------------------


class UnknownStrategy(LookupError):
    """No strategy is registered under that name."""


#: name → the factory that builds one. **The class is the factory**: `name`, `tool_version` and
#: `config` are class variables, so the registry can be listed and described without
#: constructing anything, and `STRATEGIES[name]()` is the construction.
#:
#: Factories, not the instances this used to hold, because a strategy is *work in progress*
#: while it runs: `GeminiOcrStrategy` caches its API client on itself, and a shared singleton
#: means two runs in one worker share it. One run, one instance, made where the run starts.
STRATEGIES: dict[str, type[ExtractionStrategy]] = {
    strategy.name: strategy
    for strategy in (
        PlainTextStrategy,
        HtmlStrategy,
        PdfStrategy,
        GeminiOcrStrategy,
        TesseractStrategy,
    )
}

#: MIME type → strategy name: the default strategy for an upload of that type.
#:
#: **A PDF is read by OCR, not by its text layer.** The documents this app is for are
#: photocopied scans, and a scan's embedded text layer is whatever engine the scanner shipped
#: with made of it — `0; 2C2.L .';:. jahr&nge Anamnese` is a real line from one. That is worse
#: than no text at all, because it looks like content. `pypdf` stays available for a PDF that
#: was born digital (`?strategy=pypdf`), where the text layer is the author's own; it is
#: opt-in precisely because nothing in the file says which kind it is.
MIME_STRATEGIES: dict[str, str] = {
    "text/plain": "plain-text",
    "text/markdown": "plain-text",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "gemini-ocr",
}


def strategy_for_mime(mime_type: str) -> ExtractionStrategy | None:
    """A fresh strategy for this MIME type, or None where no factory claims it."""
    name = MIME_STRATEGIES.get(mime_type.split(";")[0].strip().lower())
    return STRATEGIES[name]() if name else None


def strategy_named(name: str) -> ExtractionStrategy:
    """A fresh strategy by name. Every run gets its own — see `STRATEGIES`."""
    try:
        factory = STRATEGIES[name]
    except KeyError:
        raise UnknownStrategy(f"no extraction strategy named {name!r}") from None
    return factory()


def strategy_names() -> list[str]:
    """Every registered name, sorted — what a picker offers and `--strategy` accepts."""
    return sorted(STRATEGIES)


def mime_types_of(name: str) -> list[str]:
    """The MIME types this strategy is the default for."""
    return sorted(mime for mime, chosen in MIME_STRATEGIES.items() if chosen == name)


def register(strategy: type[ExtractionStrategy], *mime_types: str) -> None:
    """Add a strategy factory, and make it the default for these MIME types."""
    STRATEGIES[strategy.name] = strategy
    for mime_type in mime_types:
        MIME_STRATEGIES[mime_type] = strategy.name
