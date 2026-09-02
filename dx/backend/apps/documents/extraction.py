"""The extraction tree: what a strategy hands the snapshot builder, and parsers that make one.

An `Extraction` is a tree of `ExtractedNode`s (the structure, typed by HTML tag —
`apps/documents/models.py::ALLOWED_TAGS` is the vocabulary), the pages, and per node the
regions that place it on those pages. Offsets inside a node (`ExtractedRegion.span`,
`ExtractedWord.start/end`) are relative to *that node's text*; the builder
(`apps/documents/snapshot.py`) turns them into the global coordinate space of the snapshot.
Coordinates are normalized to [0, 1] with the origin top-left and y down — a strategy for a
bottom-left format (PDF) flips once, with `pdf_box`, and nowhere else.

The strategies that produce these trees live in `apps/documents/strategies.py`; the parsers
below (`HtmlTree`, `pdf_chunks`) are the reusable parts of the built-in ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import pypdf

from apps.documents.models import HEADINGS, StructureSource

Point = tuple[float, float]


class ExtractionError(Exception):
    """The adapter could not read the file at all (a whole-document failure)."""


@dataclass
class ExtractedWord:
    """A word box, offsets relative to the node's text; `conf` None for born-digital input."""

    x0: float
    y0: float
    x1: float
    y1: float
    start: int
    end: int
    conf: float | None = None


@dataclass
class ExtractedRegion:
    """Where (part of) a node sits on one page. Give `ring` (a closed polygon, first point
    not repeated) or `envelope` `(x0, y0, x1, y1)`; the builder derives the envelope from the
    ring when both are absent from the caller's side of the story."""

    page: int
    envelope: tuple[float, float, float, float] | None = None
    ring: list[Point] | None = None
    #: The slice of the node's text drawn here, relative; None for a shape without text.
    span: tuple[int, int] | None = None
    words: list[ExtractedWord] | None = None
    detect_conf: float | None = None


@dataclass
class ExtractedNode:
    """One structural element. A leaf carries `text` (or `rows` for a table); a container
    carries `children`. Both may carry regions."""

    tag: str
    text: str = ""
    children: list[ExtractedNode] = field(default_factory=list)
    level: int | None = None
    source: StructureSource = StructureSource.DETECTED
    regions: list[ExtractedRegion] = field(default_factory=list)
    #: Table cells, row-major; the first row is the header when `header` is set. Cells are
    #: not nodes: clicking a table selects the whole table.
    rows: list[list[str]] | None = None
    header: bool = False
    #: A table's inner markup (thead/tbody/tr/th/td with colspan/rowspan), when the extractor
    #: has it: emitted instead of rendering `rows`, which still give the text projection.
    table_html: str | None = None


@dataclass
class ExtractedPage:
    number: int
    label: str | None = None
    width: float | None = None
    height: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: A PNG of the page, scaled down, stored as a blob → `Page.thumbnail`.
    thumbnail: bytes | None = None


@dataclass
class Extraction:
    nodes: list[ExtractedNode]
    pages: list[ExtractedPage] = field(default_factory=list)
    #: Pages the adapter could not read; a non-empty list makes the snapshot PARTIAL.
    failed_pages: list[int] = field(default_factory=list)
    #: The extractor's native payload, kept as a blob (`DocumentContent.raw_output`).
    raw: bytes | None = None
    raw_mime: str = "application/json"
    #: Document-level metadata the file carried (title, author, language).
    meta: dict[str, Any] = field(default_factory=dict)
    #: Extractor-specific figures for `DocumentContent.stats` (`{"ocr": {...}}`).
    stats: dict[str, Any] = field(default_factory=dict)


def decode(data: bytes) -> str:
    """UTF-8 first (with the BOM stripped), then Latin-1 — which never fails."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def paragraphs(text: str) -> list[str]:
    """Blocks separated by blank lines, whitespace trimmed, empty ones dropped."""
    blocks = []
    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        cleaned = "\n".join(line.strip() for line in block.split("\n")).strip()
        if cleaned:
            blocks.append(cleaned)
    return blocks


# --- HTML: the source's own structure -------------------------------------------------------------

#: Source tags that become one leaf node each (their whole text, inline markup flattened).
_LEAF_TAGS = frozenset({*HEADINGS, "p", "li", "pre", "figcaption"})
#: Source tags that become a container node of the same (or the mapped) tag.
_CONTAINER_TAGS = {
    "section": "section",
    "article": "section",
    "main": "section",
    "ul": "ul",
    "ol": "ol",
    "figure": "figure",
    "blockquote": "blockquote",
}
#: Source subtrees that hold no content.
_SKIP_TAGS = frozenset({"head", "script", "style", "template", "noscript", "svg", "title"})
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "wbr"})
#: A block starting inside one of these closes it, as the HTML parser would (`<p>a<p>b`).
_AUTO_CLOSE = frozenset({"p", *HEADINGS})
_BLOCK_TAGS = frozenset(
    {*_LEAF_TAGS, *_CONTAINER_TAGS, "table", "div", "tr", "dl", "dt", "dd", "address"}
)


class _Frame:
    """One open source element on the parse stack."""

    def __init__(self, tag: str, node: ExtractedNode | None, *, leaf: bool = False) -> None:
        self.tag = tag
        self.node = node  # the container node children attach to, or the leaf being filled
        self.leaf = leaf
        self.text: list[str] = []  # a leaf's text, or loose text waiting to become a <p>
        self.rows: list[list[str]] = []  # a table's cells
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.header = False


class HtmlTree(HTMLParser):
    """Builds the node tree from a source document's own structure (`StructureSource.EMBEDDED`).

    Blocks the vocabulary knows become nodes; `div`, `span`, inline markup and everything else
    are transparent — their text flows into the enclosing leaf, and loose text directly inside a
    container becomes a paragraph. Blocks nested inside an `li` or `pre` are flattened into
    its text, one per line. Table cells are text, not nodes.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = ExtractedNode(tag="section")  # a holder; its children are the top level
        self.stack: list[_Frame] = [_Frame("root", self.root)]
        self.skip_depth = 0
        self.title: list[str] = []
        self.in_title = False

    # -- helpers --

    def _holder(self) -> _Frame:
        """The innermost frame that holds nodes (never a leaf or a table)."""
        for frame in reversed(self.stack):
            if frame.node is not None and not frame.leaf and frame.tag != "table":
                return frame
        return self.stack[0]

    def _leaf(self) -> _Frame | None:
        """The leaf (or table) being filled, if the innermost node frame is one."""
        for frame in reversed(self.stack):
            if frame.leaf or frame.tag == "table":
                return frame
            if frame.node is not None:
                return None
        return None

    def _flush_loose_text(self, frame: _Frame) -> None:
        text = _collapse("".join(frame.text))
        frame.text = []
        if text and frame.node is not None:
            frame.node.children.append(
                ExtractedNode(tag="p", text=text, source=StructureSource.EMBEDDED)
            )

    def _append(self, node: ExtractedNode, frame: _Frame) -> None:
        holder = self._holder()
        self._flush_loose_text(holder)
        if holder.node is not None:
            holder.node.children.append(node)
        self.stack.append(frame)

    # -- parser events --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth or tag in _SKIP_TAGS:
            if tag == "title":
                self.in_title = True
            if tag not in _VOID_TAGS:
                self.skip_depth += 1
            return
        if tag == "br":
            self.handle_data("\n")
            return
        if tag in _VOID_TAGS:
            return
        leaf = self._leaf()
        if leaf is not None and leaf.leaf and leaf.tag in _AUTO_CLOSE and tag in _BLOCK_TAGS:
            self.handle_endtag(leaf.tag)
            leaf = self._leaf()
        if leaf is not None and leaf.tag == "table":
            if tag == "tr":
                leaf.row = []
            elif tag in ("td", "th"):
                leaf.cell = []
                if tag == "th" and not leaf.rows:
                    leaf.header = True
            self.stack.append(_Frame(tag, None))
            return
        if leaf is not None:
            if tag in _BLOCK_TAGS:
                leaf.text.append("\n")
            self.stack.append(_Frame(tag, None))  # inline (or nested block) markup: flattened
            return
        if tag in _LEAF_TAGS:
            level = int(tag[1]) if tag in HEADINGS else None
            node = ExtractedNode(tag=tag, level=level, source=StructureSource.EMBEDDED)
            self._append(node, _Frame(tag, node, leaf=True))
        elif tag in _CONTAINER_TAGS:
            node = ExtractedNode(tag=_CONTAINER_TAGS[tag], source=StructureSource.EMBEDDED)
            self._append(node, _Frame(tag, node))
        elif tag == "table":
            node = ExtractedNode(tag="table", rows=[], source=StructureSource.EMBEDDED)
            self._append(node, _Frame("table", node))
        else:
            if tag in _BLOCK_TAGS:
                self._flush_loose_text(self._holder())
            self.stack.append(_Frame(tag, None))  # div, span, header, …: transparent

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if self.skip_depth:
            if tag not in _VOID_TAGS:
                self.skip_depth -= 1
            return
        if tag in _VOID_TAGS:
            return
        if not any(frame.tag == tag for frame in self.stack[1:]):
            return  # never opened (or already closed): ignore
        while len(self.stack) > 1:
            frame = self.stack.pop()
            self._close(frame)
            if frame.tag == tag:
                break

    def _close(self, frame: _Frame) -> None:
        table = self._leaf()
        if table is not None and table.tag == "table" and frame.node is None:
            if frame.tag in ("td", "th") and table.cell is not None:
                if table.row is None:
                    table.row = []
                table.row.append(_collapse("".join(table.cell)))
                table.cell = None
            elif frame.tag == "tr" and table.row is not None:
                table.rows.append(table.row)
                table.row = None
            return
        if frame.leaf and frame.node is not None:
            frame.node.text = _block_text(frame.node.tag, "".join(frame.text))
        elif frame.tag == "table" and frame.node is not None:
            frame.node.rows = [row for row in frame.rows if row]
            frame.node.header = frame.header
        elif frame.node is not None:
            self._flush_loose_text(frame)
        elif frame.tag in _BLOCK_TAGS:
            leaf = self._leaf()
            if leaf is None:
                self._flush_loose_text(self._holder())
            elif leaf.tag != "table":
                leaf.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        if self.skip_depth:
            return
        leaf = self._leaf()
        if leaf is None:
            self._holder().text.append(data)
        elif leaf.tag == "table":
            if leaf.cell is not None:
                leaf.cell.append(data)
        else:
            leaf.text.append(data)

    def finish(self) -> list[ExtractedNode]:
        self.close()
        while len(self.stack) > 1:
            self._close(self.stack.pop())
        self._flush_loose_text(self.stack[0])
        return [node for node in self.root.children if _has_content(node)]


def _collapse(text: str) -> str:
    """HTML whitespace rules for non-`pre` text: runs collapse to one space, trimmed."""
    return " ".join(text.split())


def _block_text(tag: str, text: str) -> str:
    if tag == "pre":
        return text.strip("\n")
    lines = [_collapse(line) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _has_content(node: ExtractedNode) -> bool:
    if node.rows is not None:
        return bool(node.rows)
    if node.children:
        node.children = [child for child in node.children if _has_content(child)]
        return bool(node.children)
    return bool(node.text.strip())


# --- PDF: text runs with their positions (pypdf) --------------------------------------------------


def pdf_box(
    x: float, baseline: float, width: float, font_size: float, page_w: float, page_h: float
) -> tuple[float, float, float, float]:
    """A text box in PDF user space → normalized page coordinates, origin top-left, y down.

    PDF's origin is bottom-left with y pointing up, so the flip happens here, once. `baseline`
    is where the glyphs sit; they rise about 0.8 em above it and drop 0.2 em below.
    """
    if page_w <= 0 or page_h <= 0:
        raise ExtractionError(f"page size must be positive, got {page_w}×{page_h}")
    top = baseline + 0.8 * font_size
    bottom = baseline - 0.2 * font_size
    x0 = _unit(x / page_w)
    x1 = _unit((x + width) / page_w)
    y0 = _unit(1 - top / page_h)
    y1 = _unit(1 - bottom / page_h)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)


@dataclass
class PdfChunk:
    """One text run of a page, in PDF user space (origin bottom-left, y up)."""

    text: str
    x: float
    y: float
    font_size: float


def pdf_chunks(page: pypdf.PageObject) -> list[PdfChunk]:
    """The text runs of a page with where they start (`visitor_text`: text matrix × CTM)."""
    chunks: list[PdfChunk] = []

    def visit(text: str, cm: list[float], tm: list[float], font: object, size: float) -> None:
        if not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        scale = abs(tm[3] * cm[3]) or abs(tm[0] * cm[0]) or 1.0
        chunks.append(PdfChunk(text=text, x=x, y=y, font_size=(size or 1.0) * scale))

    visitor: Callable[..., None] = visit
    page.extract_text(visitor_text=visitor)
    return chunks


def pdf_page_node(
    chunks: list[PdfChunk], number: int, width: float, height: float, advance: float
) -> ExtractedNode | None:
    """One paragraph for a page: its runs one per line, each run a word box (no confidence:
    born-digital), the region the envelope of them. None for a page without text."""
    text_parts: list[str] = []
    words: list[ExtractedWord] = []
    cursor = 0
    for chunk in chunks:
        piece = chunk.text.strip()
        if not piece:
            continue
        if text_parts:
            text_parts.append("\n")
            cursor += 1
        start = cursor
        text_parts.append(piece)
        cursor += len(piece)
        run_width = advance * chunk.font_size * len(piece)
        x0, y0, x1, y1 = pdf_box(chunk.x, chunk.y, run_width, chunk.font_size, width, height)
        words.append(ExtractedWord(x0, y0, x1, y1, start, cursor, None))
    text = "".join(text_parts)
    if not text:
        return None
    region = ExtractedRegion(
        page=number,
        envelope=(
            min(w.x0 for w in words),
            min(w.y0 for w in words),
            max(w.x1 for w in words),
            max(w.y1 for w in words),
        ),
        span=(0, len(text)),
        words=words,
    )
    return ExtractedNode(tag="p", text=text, source=StructureSource.EMBEDDED, regions=[region])
