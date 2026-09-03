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

from collections.abc import Callable, Sequence
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
    #: Standing matter — see `Block.aside`.
    aside: str | None = None
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
    #: What this reading calls the document (`DocumentContent.title`).
    title: str = ""
    #: Extractor-specific figures for `DocumentContent.stats` (`{"ocr": {...}}`).
    stats: dict[str, Any] = field(default_factory=dict)
    #: What a model made of each node's date, by `nid` — an `INFERRED` estimate the dating
    #: stage uses where the document itself carries no dateline (`apps/documents/dating.py`).
    dates: dict[int, Any] = field(default_factory=dict)


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


# --- What a strategy reads off one page ---------------------------------------------------------


@dataclass
class Block:
    """One piece of a page, as a strategy read it — the input to the pipeline.

    `tag` is the artifact's vocabulary (`p`, `h1`–`h6`, `li`, `table`, `figure`,
    `figcaption`, `blockquote`, `pre`) plus the furniture kinds `page_header`, `page_footer`
    and `page_number`, which never reach the document. Geometry is optional: a strategy
    without boxes (plain text, HTML) leaves it out and the block simply has no region.
    """

    tag: str
    text: str = ""
    level: int | None = None
    #: Tables: the cells, and the extractor's own markup when it has any.
    rows: list[list[str]] | None = None
    header: bool = False
    table_html: str | None = None
    #: Where the block sits on its page, normalized [0, 1].
    box: tuple[float, float, float, float] | None = None
    ring: list[Point] | None = None
    words: list[ExtractedWord] | None = None
    #: Whether this continues the previous page's last block. `None` = the strategy cannot
    #: tell, and the pipeline decides from the text.
    continues: bool | None = None
    #: Running header, footer or page number (`data-furniture`): never part of the document,
    #: though a page number becomes the page's label.
    furniture: str | None = None
    #: Standing matter (`data-aside`): the letterhead, the address, the bank details, the
    #: signature block. Kept, because it is on the page and someone will want it, but not what
    #: the document says — a reader is shown it only on request.
    aside: str | None = None
    source: StructureSource = StructureSource.DETECTED


# --- Sections: the outline stack ---------------------------------------------------------------


def heading_level(node: ExtractedNode) -> int | None:
    """The semantic level of a heading node, or None for anything else."""
    if node.level is not None and node.tag in HEADINGS:
        return node.level
    return int(node.tag[1]) if node.tag in HEADINGS else None


def nest_by_headings(nodes: Sequence[ExtractedNode]) -> list[ExtractedNode]:
    """A flat block stream becomes a tree: a heading of level L closes every open section of
    level ≥ L and opens its own, with the heading as its first child; everything else goes
    into the innermost open section.

    Sections are never asked for — not of a model, not of a PDF — they are derived from the
    heading stream, which is why a section can span pages, a page can hold several, and a
    heading at the bottom of one page keeps the body that follows on the next.
    """
    root = ExtractedNode(tag="section")
    stack: list[tuple[int, ExtractedNode]] = []
    for node in nodes:
        level = heading_level(node)
        if level is None:
            (stack[-1][1] if stack else root).children.append(node)
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        section = ExtractedNode(tag="section", level=level, source=node.source, children=[node])
        (stack[-1][1] if stack else root).children.append(section)
        stack.append((level, section))
    return root.children


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
    #: The run's base font (`Helvetica-Bold`), when the page names one — a heading hint.
    font: str = ""

    @property
    def bold(self) -> bool:
        name = self.font.lower()
        return "bold" in name or "black" in name or "heavy" in name


def pdf_chunks(page: pypdf.PageObject) -> list[PdfChunk]:
    """The text runs of a page with where they start (`visitor_text`: text matrix × CTM)."""
    chunks: list[PdfChunk] = []

    def visit(text: str, cm: list[float], tm: list[float], font: object, size: float) -> None:
        if not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        scale = abs(tm[3] * cm[3]) or abs(tm[0] * cm[0]) or 1.0
        chunks.append(
            PdfChunk(
                text=text,
                x=x,
                y=y,
                font_size=(size or 1.0) * scale,
                font=_font_name(font),
            )
        )

    visitor: Callable[..., None] = visit
    page.extract_text(visitor_text=visitor)
    return chunks


def _font_name(font: object) -> str:
    """`/ABCDEF+Helvetica-Bold` → `Helvetica-Bold`; "" when the page names no font."""
    base = font.get("/BaseFont") if isinstance(font, dict) else None
    name = str(base).lstrip("/") if base is not None else ""
    return name.split("+", 1)[-1]


@dataclass
class PdfLine:
    """Runs that sit on one baseline, joined in reading order."""

    text: str
    x0: float
    x1: float
    baseline: float
    size: float
    bold: bool
    runs: list[PdfChunk]

    @property
    def words(self) -> int:
        return len(self.text.split())


#: Runs within this fraction of the font size share a baseline.
BASELINE_TOLERANCE = 0.4
#: The pen has to have moved at least this far (in points) for a run to start a new word.
SPACE_GAP = 0.5
#: A line further from the previous one than this multiple of the usual leading starts a block.
BLOCK_GAP = 1.55
#: A line this much larger than the body text is a heading…
HEADING_SIZE = 1.12
#: …and so is a short bold line. Longer than this and it is a bold sentence, not a heading.
HEADING_WORDS = 9
#: What a list item starts with.
BULLETS = ("•", "▪", "◦", "-", "–", "—", "*")


def pdf_lines(chunks: Sequence[PdfChunk]) -> list[PdfLine]:
    """Group runs into lines, **in the order the page's content stream emits them**.

    Not sorted by position, and that is the point: a PDF reports the *last positioning
    operator* for every run, so several runs of one line share one x — sorting by it
    interleaves the words of neighbouring lines into nonsense. The stream order is the
    author's own reading order; y only says where one line ends and the next begins.
    """
    lines: list[PdfLine] = []
    for chunk in chunks:
        piece = chunk.text.strip()
        if not piece:
            continue
        current = lines[-1] if lines else None
        previous = current.runs[-1] if current is not None else None
        same_line = current is not None and abs(
            current.baseline - chunk.y
        ) <= BASELINE_TOLERANCE * max(chunk.font_size, 1.0)
        if current is not None and previous is not None and same_line:
            # A word boundary is either written into the run (the author's own leading space,
            # which `strip()` above has just removed) or implied by the pen having moved on.
            spaced = chunk.text.startswith(" ") or chunk.x > previous.x + SPACE_GAP
            current.text = f"{current.text}{' ' if spaced else ''}{piece}"
            current.x0 = min(current.x0, chunk.x)
            # Measured from the line's whole text, not from this run's own x: a PDF reports
            # the last positioning operator for every run of a line, so the runs all claim the
            # same x and a per-run maximum leaves the line far too narrow.
            current.x1 = current.x0 + _text_width(current.text, max(current.size, chunk.font_size))
            current.size = max(current.size, chunk.font_size)
            current.bold = current.bold or chunk.bold
            current.runs.append(chunk)
            continue
        lines.append(
            PdfLine(
                text=piece,
                x0=chunk.x,
                x1=chunk.x + _run_width(chunk),
                baseline=chunk.y,
                size=chunk.font_size,
                bold=chunk.bold,
                runs=[chunk],
            )
        )
    return lines


#: Estimated glyph advance as a fraction of the font size (no font metrics are read).
ADVANCE = 0.5


def _run_width(chunk: PdfChunk) -> float:
    return _text_width(chunk.text.strip(), chunk.font_size)


def _text_width(text: str, size: float) -> float:
    """How wide a run of text is, with no font metrics to ask.

    `ADVANCE` is the average glyph advance as a fraction of the font size. Measured against
    pdfium's own character boxes on a scanned letter, this lands within a few percent of the
    truth for body text; it is an estimate, and the exact boxes would mean reading the page a
    second time through pdfium's text API.
    """
    return ADVANCE * size * len(text)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def body_size(lines: Sequence[PdfLine]) -> float:
    """The document's body font size — the median line size, so a big letterhead or a stray
    footnote does not decide what "normal" is."""
    return _median([line.size for line in lines]) or 1.0


def pdf_paragraphs(lines: Sequence[PdfLine]) -> list[list[PdfLine]]:
    """Lines into blocks: a wider-than-usual vertical gap, a change of font size, or a bullet
    starts a new one. The usual gap is the page's own median leading, so single- and
    double-spaced documents are both read correctly."""
    if not lines:
        return []
    gaps = [
        previous.baseline - line.baseline
        for previous, line in zip(lines, lines[1:], strict=False)
        if 0 < previous.baseline - line.baseline
    ]
    leading = _median(gaps) or max(lines[0].size, 1.0)
    blocks: list[list[PdfLine]] = [[lines[0]]]
    for previous, line in zip(lines, lines[1:], strict=False):
        gap = previous.baseline - line.baseline
        starts_block = (
            gap > BLOCK_GAP * leading
            or gap < 0  # a column break: the next line sits higher up the page
            or abs(line.size - previous.size) > 0.15 * max(previous.size, 1.0)
            or line.bold != previous.bold
            or _bullet(line.text) is not None
        )
        if starts_block:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return blocks


def _bullet(text: str) -> str | None:
    """The bullet or numbering a line starts with, if any."""
    stripped = text.lstrip()
    for bullet in BULLETS:
        if stripped.startswith(f"{bullet} "):
            return bullet
    head = stripped.split(" ", 1)[0]
    if len(head) <= 4 and head[:-1].isdigit() and head.endswith((".", ")")):
        return head
    return None


def block_text(block: Sequence[PdfLine]) -> str:
    """The block's text, with a hyphen at a line end joined rather than kept."""
    parts: list[str] = []
    for line in block:
        piece = line.text
        if parts and parts[-1].endswith("-"):
            parts[-1] = parts[-1][:-1] + piece
        else:
            parts.append(piece)
    return "\n".join(parts)


def pdf_page_blocks(
    chunks: Sequence[PdfChunk], number: int, width: float, height: float, body: float
) -> list[Block]:
    """One page's runs as semantic blocks: headings, list items and paragraphs, each with the
    box it occupies and a word box per run.

    Structure comes from the geometry the PDF already carries — line spacing, font size,
    boldness, bullets — because a born-digital page has no tags to read. Anything this cannot
    tell apart stays a paragraph, which is the honest default; joining across pages, grouping
    and sectioning are the pipeline's job, not this function's.
    """
    blocks: list[Block] = []
    for lines in pdf_paragraphs(pdf_lines(chunks)):
        text = block_text(lines)
        if not text.strip() or _is_decoration(lines, body):
            continue
        bullet = _bullet(lines[0].text)
        if bullet is not None:
            text = text[len(bullet) :].lstrip()
        tag, level = _classify(lines, body, bullet is not None)
        region = _block_region(lines, text, number, width, height)
        blocks.append(
            Block(
                tag=tag,
                text=text,
                level=level,
                source=StructureSource.EMBEDDED,
                box=region.envelope,
                words=region.words,
            )
        )
    return blocks


#: A single glyph this much larger than the body text is a logo, not a word.
DECORATION_SIZE = 2.5


def _is_decoration(block: Sequence[PdfLine], body: float) -> bool:
    """A lone oversized character — the letterhead's monogram — is not content."""
    return (
        len(block) == 1
        and len(block[0].text.strip()) <= 1
        and block[0].size > DECORATION_SIZE * body
    )


def _classify(block: Sequence[PdfLine], body: float, is_item: bool) -> tuple[str, int | None]:
    if is_item:
        return "li", None
    first = block[0]
    single = len(block) == 1
    large = first.size >= HEADING_SIZE * body
    if single and (large or (first.bold and first.words <= HEADING_WORDS)):
        # Two sizes above the body text read as the document's title level, one as a section.
        level = 2 if first.size >= 1.5 * body else 3
        return f"h{level}", level
    return "p", None


def _block_region(
    block: Sequence[PdfLine], text: str, number: int, width: float, height: float
) -> ExtractedRegion:
    """The block's envelope and its runs as word boxes, in page coordinates."""
    words: list[ExtractedWord] = []
    cursor = 0
    for index, line in enumerate(block):
        if index:
            cursor += 1  # the newline between lines
        for run in line.runs:
            piece = run.text.strip()
            if not piece:
                continue
            start = text.find(piece, cursor)
            if start < 0:
                continue
            cursor = start + len(piece)
            x0, y0, x1, y1 = pdf_box(run.x, run.y, _run_width(run), run.font_size, width, height)
            words.append(ExtractedWord(x0, y0, x1, y1, start, cursor, None))
    boxes = [
        pdf_box(line.x0, line.baseline, line.x1 - line.x0, line.size, width, height)
        for line in block
    ]
    envelope = (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
    return ExtractedRegion(page=number, envelope=envelope, span=(0, len(text)), words=words or None)
