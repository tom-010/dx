"""Extraction strategies: how a document becomes a snapshot.

    class DoclingStrategy(ExtractionStrategy):
        name = "docling"
        tool_version = "2.48.0"

        def extract(self, document: Document) -> DocumentContent:
            result = convert(document.source_blob.read_bytes())   # whatever the tool does
            tree = Extraction(nodes=[...], pages=[...])           # apps/documents/extraction.py
            return self.snapshot(document, tree)                  # rows, offsets, confidence

    register(DoclingStrategy(), "application/pdf", "image/tiff")

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
snapshot points at is a strategy's `name` + `tool_version`). Built in: `plain-text`, `html`,
`pypdf` (born-digital PDFs without OCR, so no confidence), and `gemini-ocr` (opt-in vision
OCR for scans, `apps/documents/ocr/`).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from io import BytesIO
from typing import Any, ClassVar

import pypdf

from apps.documents import extraction as tree
from apps.documents.extraction import (
    ExtractedNode,
    ExtractedPage,
    Extraction,
    ExtractionError,
    HtmlTree,
)
from apps.documents.models import Document, DocumentContent, StructureSource
from apps.documents.ocr import assembly, gemini_client, page_schema, render, run
from config.env import env


class ExtractionStrategy(ABC):
    """Turn a document into a snapshot. Subclass, set `name` and `tool_version`, implement
    `extract()`; `(name, tool_version)` is the `Extractor` row every snapshot points at, so a
    new tool version means new snapshots."""

    name: ClassVar[str]
    tool_version: ClassVar[str]
    #: Recorded on the `Extractor` row — what a run was configured with.
    config: ClassVar[dict[str, Any]] = {}

    @abstractmethod
    def extract(self, document: Document) -> DocumentContent:
        """The snapshot of `document` produced by this strategy: written, terminal (SUCCEEDED
        or PARTIAL), not yet current. Raise for a whole-document failure."""

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

    def extract(self, document: Document) -> DocumentContent:
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


class PdfStrategy(TreeStrategy):
    """`application/pdf` without OCR: each page becomes one paragraph placed by the box around
    its text runs. Scanned pages come out empty — a page row with no node. Confidence stays
    None: born-digital text is not recognised, it is read.

    The raw output keeps every page's size and text runs (and the file's metadata), so a
    rebuild reprojects from it without opening the PDF again.
    """

    name = "pypdf"
    tool_version = pypdf.__version__
    #: Estimated glyph advance as a fraction of the font size (no font metrics are read).
    config: ClassVar[dict[str, Any]] = {"advance": 0.5}

    def parse(self, data: bytes, mime_type: str) -> Extraction:
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
                    "chunks": [[c.text, c.x, c.y, c.font_size] for c in chunks],
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
        return self._tree({"pages": records, "meta": meta})

    def reproject(self, raw: bytes, mime_type: str) -> Extraction | None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
            return None
        return self._tree(payload)

    def _tree(self, payload: dict[str, Any]) -> Extraction:
        pages: list[ExtractedPage] = []
        nodes: list[ExtractedNode] = []
        failed: list[int] = []
        advance = float(self.config["advance"])
        for record in payload["pages"]:
            number = int(record["number"])
            if record.get("failed"):
                failed.append(number)
                pages.append(ExtractedPage(number=number))
                continue
            width, height = float(record["width"]), float(record["height"])
            pages.append(ExtractedPage(number=number, width=width, height=height))
            chunks = [
                tree.PdfChunk(text=str(t), x=float(x), y=float(y), font_size=float(size))
                for t, x, y, size in record["chunks"]
            ]
            node = tree.pdf_page_node(chunks, number, width, height, advance)
            if node is not None:
                nodes.append(node)
        stored = payload.get("meta")
        meta: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        return Extraction(
            nodes=nodes,
            pages=pages,
            failed_pages=failed,
            raw=json.dumps(payload).encode(),
            meta=meta,
        )


def _creation_date(info: pypdf.DocumentInformation) -> str | None:
    """The PDF's creation date as an ISO day, if the file carries a readable one."""
    try:
        created = info.creation_date
    except Exception:  # noqa: BLE001 - a malformed date string is not a failure
        return None
    return created.date().isoformat() if created is not None else None


class GeminiOcrStrategy(ExtractionStrategy):
    """Gemini-vision OCR for scans (`apps/documents/ocr/`): every page image goes to the model
    once, the assembly turns the answers into the tree, the builder does the rest. Opt-in
    (`reextract(strategy=...)`, `?strategy=gemini-ocr`): it costs money and sends page images
    to Google — never the default for a MIME type. A rebuild (`from_raw=True`) replays the
    stored per-page JSON without a single request.

    No per-word confidence exists here, so `conf_stats` stays NULL ("no per-word confidence
    data available"); the model is never asked to rate itself.
    """

    name = "gemini-ocr"
    #: Bump on any change to the prompt or the response schema: they are the extractor.
    tool_version = "1"
    config: ClassVar[dict[str, Any]] = {
        "model": gemini_client.MODEL,
        "dpi": render.DPI,
        "prompt_sha256": gemini_client.prompt_sha256(),
        "schema_version": page_schema.SCHEMA_VERSION,
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

    def extract(self, document: Document) -> DocumentContent:
        from apps.documents import snapshot  # noqa: PLC0415 - snapshot imports this module

        source = snapshot.rebuild_source()
        if source is not None:
            pages = assembly.pages_from_raw(source[0])
        else:
            pages = list(
                run.read_document(
                    document.source_blob.read_bytes(),
                    self.reader(),
                    dpi=int(self.config["dpi"]),
                    thumbnails=True,
                )
            )
        if not pages:
            raise ExtractionError("the PDF has no pages")
        if all(page.failed for page in pages):
            raise ExtractionError(f"no page could be read: {pages[0].error}")
        return self.snapshot(document, assembly.assemble(pages))


# --- Registry ------------------------------------------------------------------------------------


class UnknownStrategy(LookupError):
    """No strategy is registered under that name."""


STRATEGIES: dict[str, ExtractionStrategy] = {
    strategy.name: strategy
    for strategy in (PlainTextStrategy(), HtmlStrategy(), PdfStrategy(), GeminiOcrStrategy())
}

#: MIME type → strategy name: the default strategy for an upload of that type.
MIME_STRATEGIES: dict[str, str] = {
    "text/plain": "plain-text",
    "text/markdown": "plain-text",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pypdf",
}


def strategy_for_mime(mime_type: str) -> ExtractionStrategy | None:
    name = MIME_STRATEGIES.get(mime_type.split(";")[0].strip().lower())
    return STRATEGIES[name] if name else None


def strategy_named(name: str) -> ExtractionStrategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise UnknownStrategy(f"no extraction strategy named {name!r}") from None


def register(strategy: ExtractionStrategy, *mime_types: str) -> None:
    """Add a strategy, and make it the default for these MIME types."""
    STRATEGIES[strategy.name] = strategy
    for mime_type in mime_types:
        MIME_STRATEGIES[mime_type] = strategy.name
