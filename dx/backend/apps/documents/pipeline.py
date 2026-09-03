"""The reading pipeline: structured pages in, one augmented HTML document out — and only
then the rows a snapshot is made of.

Every extractor reads *one page at a time*: that is what an OCR request, a PDF's text layer or
a layout model can actually see. Everything that spans pages is decided here instead, once, in
plain Python, for all of them:

1. **Read** (the strategy's own job): each page becomes semantic HTML — `PageHtml`. A page the
   extractor could not read is `failed`; a blank one is simply empty.
2. **Parse & prune**: each page's HTML is parsed into blocks. Furniture (`data-furniture`)
   never reaches the document, though a page number becomes the page's label; empty blocks and
   empty pages contribute nothing. Their `Page` rows survive — the document *has* that page,
   it just says nothing — so nothing downstream needs a special case. Standing matter — the
   letterhead, the address, the bank details, the signature block (`data-aside`) — is the
   opposite case: it *is* on the paper, so it is kept, marked, and carried all the way into
   the artifact, where a reader is shown it only on request.
3. **Join**: what a page break cut in two is put back together. A strategy that knows says so
   (`data-continues`, which the OCR model is asked outright); one that cannot know leaves it
   out and `looks_continued()` decides from the text. A joined block keeps one fragment per
   page, which is what its `data-box` and its regions are made of.
4. **Group & nest**: list items become a list, a caption joins its figure, and the heading
   stream becomes sections (`extraction.nest_by_headings`).
5. **Serialize**: one HTML document for the whole file, every tag carrying `data-pages` (the
   pages it is on — several when it spans them) and `data-box` (where, per page, and which
   slice of its text sits there). This is the artifact to look at while iterating on an
   extractor: `manage.py ocr assemble` writes it and `review()` says what is wrong with it.
6. **Convert**: the augmented HTML is parsed back into the tree the snapshot builder writes as
   rows (`extraction_from_html`). The HTML is the source of truth, not a view of one.

Offsets inside a block are relative to that block; the builder makes them global.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Any

from apps.documents.extraction import (
    Block,
    ExtractedNode,
    ExtractedPage,
    ExtractedRegion,
    ExtractedWord,
    Extraction,
    Point,
    nest_by_headings,
)
from apps.documents.models import HEADINGS, StructureSource

#: The kinds of page furniture a strategy may report (Brief 01 §7): never part of the content.
FURNITURE = frozenset({"header", "footer", "page-number", "page_number"})
#: A block this short with no letter in it is a scanner artefact, not content: a scan of an
#: empty sheet routinely yields a stray `:'` or `.` from whatever engine read it. A page with
#: nothing else on it is an empty page, and the document should say so.
NOISE_LENGTH = 4
#: A page number longer than this is not a page label.
LABEL_MAX = 20
#: Text that ends like this is a finished sentence — the next page starts something new.
SENTENCE_END = (".", "!", "?", ":", ";", "”", '"', "»", ")")


# --- Tables ------------------------------------------------------------------------------------


@dataclass
class Cell:
    tag: str
    text: str
    colspan: int = 1
    rowspan: int = 1

    def html(self) -> str:
        attrs = ""
        if self.colspan > 1:
            attrs += f' colspan="{self.colspan}"'
        if self.rowspan > 1:
            attrs += f' rowspan="{self.rowspan}"'
        return f"<{self.tag}{attrs}>{escape(self.text, quote=False)}</{self.tag}>"


@dataclass
class Table:
    """The rows of a table: header rows (a `<thead>`, or a first row of `th` cells) and body
    rows, kept apart so a repeated header can be dropped when a table continues."""

    header: list[list[Cell]] = field(default_factory=list)
    body: list[list[Cell]] = field(default_factory=list)

    @classmethod
    def parse(cls, html: str) -> Table:
        parser = _TableParser()
        parser.feed(html)
        parser.close()
        parser.flush()
        table = cls(header=parser.header, body=parser.body)
        if not table.header and table.body and all(c.tag == "th" for c in table.body[0]):
            table.header = [table.body.pop(0)]
        return table

    @classmethod
    def of_rows(cls, rows: Sequence[Sequence[str]], header: bool) -> Table:
        cells = [[Cell("td", text) for text in row] for row in rows]
        if header and cells:
            first = [Cell("th", cell.text) for cell in cells[0]]
            return cls(header=[first], body=cells[1:])
        return cls(body=cells)

    @property
    def rows(self) -> list[list[Cell]]:
        return self.header + self.body

    def rows_text(self) -> list[list[str]]:
        return [[cell.text for cell in row] for row in self.rows]

    def text(self) -> str:
        return "\n".join("\t".join(cell.text for cell in row) for row in self.rows)

    def inner_html(self) -> str:
        parts = []
        if self.header:
            parts.append("<thead>" + "".join(_row_html(r) for r in self.header) + "</thead>")
        parts.append("<tbody>" + "".join(_row_html(r) for r in self.body) + "</tbody>")
        return "".join(parts)

    def append(self, other: Table, *, drop_repeated_header: bool = True) -> int:
        """Rows of a continuation; a repeated header (the same cell texts as ours) is dropped
        — a table broken over two pages usually reprints it."""
        rows = other.rows
        first = [c.text for c in rows[0]] if rows else None
        ours = [c.text for c in self.header[0]] if self.header else None
        if drop_repeated_header and first is not None and first == ours:
            rows = rows[1:]
        self.body.extend(rows)
        return len(rows)


def _row_html(row: list[Cell]) -> str:
    return "<tr>" + "".join(cell.html() for cell in row) + "</tr>"


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.header: list[list[Cell]] = []
        self.body: list[list[Cell]] = []
        self.in_head = False
        self.row: list[Cell] | None = None
        self.cell: Cell | None = None
        self.buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "thead":
            self.in_head = True
        elif tag == "tr":
            self.flush_row()
            self.row = []
        elif tag in ("td", "th"):
            self.flush_cell()
            spans = dict(attrs)
            self.cell = Cell(
                tag=tag,
                text="",
                colspan=_span(spans.get("colspan")),
                rowspan=_span(spans.get("rowspan")),
            )
            self.buffer = []
        elif tag == "br" and self.cell is not None:
            self.buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self.flush_cell()
        elif tag == "tr":
            self.flush_row()
        elif tag == "thead":
            self.in_head = False

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.buffer.append(data)

    def flush_cell(self) -> None:
        if self.cell is not None and self.row is not None:
            self.cell.text = " ".join("".join(self.buffer).split())
            self.row.append(self.cell)
        self.cell = None
        self.buffer = []

    def flush_row(self) -> None:
        self.flush_cell()
        if self.row:
            (self.header if self.in_head else self.body).append(self.row)
        self.row = None

    def flush(self) -> None:
        self.flush_row()


def _span(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


# --- Items: a block after the pages have been joined ---------------------------------------------


@dataclass
class Fragment:
    """One occurrence of an item on a page: its box there, and the slice of the item's text
    that page holds."""

    page: int
    box: tuple[float, float, float, float] | None
    ring: list[Point] | None
    start: int
    end: int
    words: list[ExtractedWord] | None


@dataclass
class Item:
    tag: str
    level: int | None
    text: str
    table: Table | None
    source: StructureSource
    fragments: list[Fragment]
    #: Standing matter — see `Block.aside`.
    aside: str | None = None


_MERGEABLE: dict[str, frozenset[str]] = {
    "p": frozenset({"p"}),
    "li": frozenset({"li"}),
    "figcaption": frozenset({"figcaption"}),
    "blockquote": frozenset({"blockquote"}),
    "pre": frozenset({"pre"}),
    "table": frozenset({"table"}),
}


def compatible(previous: str, current: str, *, merge_tables: bool) -> bool:
    """Only a block of a kind that can *be* continued is ever joined to the previous page —
    a heading never continues a paragraph, whatever a model claims."""
    if current == "table" and not merge_tables:
        return False
    return current in _MERGEABLE.get(previous, frozenset())


def looks_continued(previous: Item, block: Block) -> bool:
    """No hint from the strategy: decide from the text. A paragraph that broke over a page
    ends mid-sentence and picks up in lower case — or ends on a hyphen."""
    if not compatible(previous.tag, block.tag, merge_tables=False):
        return False
    tail = previous.text.rstrip()
    head = block.text.lstrip()
    if not tail or not head:
        return False
    if tail.endswith("-"):
        return True
    if tail.endswith(SENTENCE_END):
        return False
    first = head[0]
    return first.islower() or first.isdigit()


def _item(block: Block, page: int) -> Item:
    table = _table_of(block)
    text = table.text() if table is not None else block.text
    return Item(
        tag=block.tag,
        level=block.level,
        text=text,
        table=table,
        source=block.source,
        aside=block.aside,
        fragments=[Fragment(page, block.box, block.ring, 0, len(text), block.words)],
    )


def _table_of(block: Block) -> Table | None:
    if block.table_html:
        return Table.parse(block.table_html)
    if block.rows is not None:
        return Table.of_rows(block.rows, block.header)
    return None


def _join(item: Item, block: Block, page: int) -> None:
    """Append the continuation on `page` to `item`, and remember where it starts."""
    table = _table_of(block)
    if item.table is not None and table is not None:
        before = len(item.text)
        item.table.append(table)
        item.text = item.table.text()
        start = min(before + 1, len(item.text))
        item.fragments.append(
            Fragment(page, block.box, block.ring, start, len(item.text), block.words)
        )
        return
    if item.text.endswith("-"):
        # A line-break hyphen at the page's end: dropped, the word runs on. (A compound split
        # at a real hyphen loses it here — an accepted, fixtured error.)
        item.fragments[-1].end = len(item.text) - 1
        start = len(item.text) - 1
        item.text = item.text[:-1] + block.text
    else:
        start = len(item.text) + 1
        item.text = f"{item.text} {block.text}"
    item.fragments.append(Fragment(page, block.box, block.ring, start, len(item.text), block.words))


# --- Stage 1's output, and the attributes that carry a page through the pipeline -----------------


@dataclass
class PageHtml:
    """One page as its extractor read it: semantic HTML, or nothing."""

    number: int
    html: str = ""
    width: float | None = None
    height: float | None = None
    label: str | None = None
    thumbnail: bytes | None = None
    failed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


#: Which tag this is: pre-order from 1, the same numbering the snapshot gives its nodes, so a
#: reviewer (a person or a model) and the database mean the same thing by "node 12".
NID_ATTR = "data-nid"
#: The pages a tag is on, ascending: `data-pages="1,2"`.
PAGES_ATTR = "data-pages"
#: Where a tag is, per page: `page;x0,y0,x1,y1;start,end`, entries separated by `|`, and the
#: geometry empty (`2;;0,48`) when the extractor knew the page but not the place.
BOX_ATTR = "data-box"
#: A page's first tag continues the previous page's last one.
CONTINUES_ATTR = "data-continues"
#: Running headers, footers and page numbers, which never reach the document.
FURNITURE_ATTR = "data-furniture"
#: Standing matter: kept, marked, and hidden until a reader asks for it.
ASIDE_ATTR = "data-aside"
#: Coordinates are written with this many decimals — enough for a 5000 px page, and stable.
BOX_DECIMALS = 4


def format_boxes(fragments: Sequence[Fragment]) -> str:
    parts = []
    for fragment in fragments:
        box = (
            ",".join(f"{value:.{BOX_DECIMALS}f}" for value in fragment.box)
            if fragment.box is not None
            else ""
        )
        parts.append(f"{fragment.page};{box};{fragment.start},{fragment.end}")
    return "|".join(parts)


def parse_boxes(value: str) -> list[Fragment]:
    """`data-box` back into fragments; anything unreadable is skipped rather than fatal."""
    fragments: list[Fragment] = []
    for entry in value.split("|"):
        parts = entry.split(";")
        if len(parts) != 3 or not parts[0].strip().isdigit():
            continue
        page = int(parts[0])
        box: tuple[float, float, float, float] | None = None
        if parts[1].strip():
            numbers = [float(piece) for piece in parts[1].split(",")]
            if len(numbers) == 4:
                box = (numbers[0], numbers[1], numbers[2], numbers[3])
        span = [int(piece) for piece in parts[2].split(",") if piece.strip().isdigit()]
        start, end = (span + [0, 0])[:2]
        fragments.append(Fragment(page, box, None, start, end, None))
    return fragments


def parse_pages(value: str) -> list[int]:
    return sorted({int(piece) for piece in value.split(",") if piece.strip().isdigit()})


# --- Reading HTML ---------------------------------------------------------------------------------

#: What a block-level tag may be. `ol` is read but written back as `ul` — the vocabulary the
#: artifact is sanitized against has both, but nothing detects numbering yet.
BLOCK_TAGS = frozenset(
    {"section", *HEADINGS, "p", "li", "blockquote", "pre", "figure", "figcaption", "table"}
)
CONTAINER_TAGS = frozenset({"section", "ul", "ol", "figure"})
#: Containers whose only job is grouping — the pipeline derives them again itself.
GROUPING_TAGS = frozenset({"section", "ul", "ol"})
#: Blocks that hold text, never other blocks — a browser closes them on the next block, and
#: so does this parser.
LEAF_TAGS = frozenset({*HEADINGS, "p", "li", "figcaption", "pre"})
#: Inline markup inside a block: dropped, its text kept.
INLINE_TAGS = frozenset({"b", "strong", "i", "em", "u", "span", "a", "sup", "sub", "small", "br"})


@dataclass
class Element:
    """A tag as it was read: its own text, its children, and the attributes that matter."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[Element] = field(default_factory=list)
    #: Tables keep their markup verbatim; nothing else needs it.
    inner_html: str = ""

    @property
    def nid(self) -> int | None:
        value = self.attrs.get(NID_ATTR, "")
        return int(value) if value.isdigit() else None

    @property
    def pages(self) -> list[int]:
        return parse_pages(self.attrs.get(PAGES_ATTR, ""))

    @property
    def fragments(self) -> list[Fragment]:
        return parse_boxes(self.attrs.get(BOX_ATTR, ""))

    @property
    def continues(self) -> bool | None:
        value = self.attrs.get(CONTINUES_ATTR)
        return None if value is None else value.strip().lower() in ("", "true", "1", "yes")

    @property
    def furniture(self) -> str | None:
        return self.attrs.get(FURNITURE_ATTR)

    @property
    def aside(self) -> str | None:
        return self.attrs.get(ASIDE_ATTR)


class HtmlDocument(HTMLParser):
    """A parser for *our own* HTML: the block vocabulary, its attributes, and nothing else.

    Strict where it matters and forgiving where it does not: an unknown tag is dropped and its
    text kept, inline markup is flattened, and a tag left open is reported — `review()` turns
    that report into the snapshot's `html_problems`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element(tag="#document")
        self.stack: list[Element] = [self.root]
        self.problems: list[str] = []
        self.table_depth = 0

    # -- helpers --

    @property
    def current(self) -> Element:
        return self.stack[-1]

    def _raw(self, markup: str) -> None:
        for element in self.stack:
            if element.tag == "table":
                element.inner_html += markup

    # -- parser events --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.table_depth:
            self.table_depth += 1 if tag == "table" else 0
            self._raw(self.get_starttag_text() or f"<{tag}>")
            return
        if tag in INLINE_TAGS:
            if tag == "br":
                self.current.text += "\n"
            return
        if tag == "table":
            self.table_depth = 1
        if tag not in BLOCK_TAGS and tag not in CONTAINER_TAGS:
            self.problems.append(f"<{tag}> is not part of the vocabulary")
            return
        # A leaf block cannot contain another block: a browser closes it, and so do we — a
        # missing `</p>` is the commonest thing a generator gets wrong, and it must not cost
        # the rest of the page.
        while self.current.tag in LEAF_TAGS or (tag == "li" and self.current.tag == "li"):
            self.stack.pop()
        element = Element(tag=tag, attrs={k: v or "" for k, v in attrs})
        self.current.children.append(element)
        self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.table_depth:
            self._raw(self.get_starttag_text() or f"<{tag}/>")
        elif tag == "br":
            self.current.text += "\n"

    def handle_endtag(self, tag: str) -> None:
        if self.table_depth:
            if tag == "table":
                self.table_depth -= 1
                if self.table_depth == 0:
                    self._close("table")
                    return
            self._raw(f"</{tag}>")
            return
        if tag in INLINE_TAGS or (tag not in BLOCK_TAGS and tag not in CONTAINER_TAGS):
            return
        self._close(tag)

    def _close(self, tag: str) -> None:
        if not any(element.tag == tag for element in self.stack[1:]):
            # Either it was never opened, or a following block closed it already (above).
            return
        while len(self.stack) > 1:
            element = self.stack.pop()
            if element.tag == tag:
                return
            self.problems.append(f"<{element.tag}> was left open inside <{tag}>")

    def handle_data(self, data: str) -> None:
        if self.table_depth:
            self._raw(data)
            return
        if self.current.tag == "#document":
            if data.strip():
                self.problems.append(f"text outside any tag: {data.strip()[:40]!r}")
            return
        self.current.text += data

    def finish(self) -> Element:
        self.close()
        while len(self.stack) > 1:
            element = self.stack.pop()
            self.problems.append(f"<{element.tag}> was never closed")
        return self.root


def read_html(html: str) -> tuple[Element, list[str]]:
    """Parse an HTML document (ours or an extractor's) into elements and problems."""
    parser = HtmlDocument()
    parser.feed(html)
    root = parser.finish()
    return root, parser.problems


def _collapse(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


#: How a `data-box` value is read. The default is the canonical `page;x0,y0,x1,y1;start,end`;
#: an extractor whose model writes boxes another way (Gemini's 0–1000 grid) passes its own.
type BoxReader = Callable[[str], tuple[float, float, float, float] | None]


def _canonical_box(value: str) -> tuple[float, float, float, float] | None:
    fragments = parse_boxes(value)
    return fragments[0].box if fragments else None


def blocks_of(
    element: Element, *, page: int, box_reader: BoxReader = _canonical_box
) -> list[Block]:
    """One page's elements as a flat block stream: a list becomes its items, a figure its
    caption beside it, a section its contents — the pipeline re-groups and re-nests them once
    it has every page."""
    blocks: list[Block] = []
    for child in element.children:
        if child.tag in GROUPING_TAGS:
            # A section or a list is re-derived once every page is in; only its contents
            # travel through the pipeline.
            blocks += blocks_of(child, page=page, box_reader=box_reader)
            continue
        blocks.append(_block_of(child, page, box_reader))
        if child.tag == "figure":
            # The figure itself is a block (it has the box); its caption follows it, whether
            # the extractor nested it or wrote it alongside.
            blocks += blocks_of(child, page=page, box_reader=box_reader)
    return blocks


def _block_of(element: Element, page: int, box_reader: BoxReader = _canonical_box) -> Block:
    raw_box = element.attrs.get(BOX_ATTR, "")
    box = box_reader(raw_box) if raw_box else None
    text = element.text if element.tag == "pre" else _collapse(element.text)
    return Block(
        tag=element.tag,
        text="" if element.tag in ("table", "figure") else text,
        level=int(element.tag[1]) if element.tag in HEADINGS else None,
        table_html=f"<table>{element.inner_html}</table>" if element.tag == "table" else None,
        box=box,
        continues=element.continues,
        furniture=element.furniture,
        aside=element.aside,
        source=StructureSource.DETECTED,
    )


# --- Writing HTML ---------------------------------------------------------------------------------


class Numbering:
    """Pre-order tag numbers for one document, handed out as it is written."""

    def __init__(self) -> None:
        self.next = 1

    def take(self) -> int:
        number = self.next
        self.next += 1
        return number


def _attrs(item: Item, nid: int) -> str:
    pages = sorted({fragment.page for fragment in item.fragments})
    parts = [f'{NID_ATTR}="{nid}"']
    if item.aside is not None:
        parts.append(f'{ASIDE_ATTR}="{escape(item.aside, quote=True)}"')
    if pages:
        parts.append(f'{PAGES_ATTR}="{",".join(str(page) for page in pages)}"')
    if item.fragments:
        parts.append(f'{BOX_ATTR}="{format_boxes(item.fragments)}"')
    return " " + " ".join(parts)


def _element_html(entry: Group, numbers: Numbering) -> str:
    match entry.kind:
        case "heading":
            item = entry.items[0]
            tag = f"h{item.level or 2}"
            attrs = _attrs(item, numbers.take())
            return f"<{tag}{attrs}>{escape(item.text, quote=False)}</{tag}>"
        case "block":
            item = entry.items[0]
            attrs = _attrs(item, numbers.take())
            if item.table is not None:
                return f"<table{attrs}>{item.table.inner_html()}</table>"
            return f"<{item.tag}{attrs}>{escape(item.text, quote=False)}</{item.tag}>"
        case "list":
            pages = sorted({f.page for item in entry.items for f in item.fragments})
            attrs = f' {NID_ATTR}="{numbers.take()}"'
            if pages:
                attrs += f' {PAGES_ATTR}="{",".join(str(page) for page in pages)}"'
            items = "".join(
                f"<li{_attrs(item, numbers.take())}>{escape(item.text, quote=False)}</li>"
                for item in entry.items
            )
            return f"<ul{attrs}>{items}</ul>"
        case "figure":
            attrs = _attrs(entry.items[0], numbers.take())
            caption = entry.caption
            inner = (
                f"<figcaption{_attrs(caption, numbers.take())}>"
                f"{escape(caption.text, quote=False)}</figcaption>"
                if caption is not None
                else ""
            )
            return f"<figure{attrs}>{inner}</figure>"
        case _:
            raise ValueError(f"not a group: {entry.kind}")


def block_html(block: Block, page: int) -> str:
    """One block as a page fragment writes it — the canonical form every strategy hands the
    pipeline, whatever it read the page with. No number here: a page does not know where its
    blocks will end up in the document."""
    attrs = f' {PAGES_ATTR}="{page}"'
    fragment = Fragment(page, block.box, block.ring, 0, len(block.text), block.words)
    attrs += f' {BOX_ATTR}="{format_boxes([fragment])}"'
    if block.continues:
        attrs += f' {CONTINUES_ATTR}="true"'
    if block.furniture is not None:
        attrs += f' {FURNITURE_ATTR}="{escape(block.furniture, quote=True)}"'
    if block.aside is not None:
        attrs += f' {ASIDE_ATTR}="{escape(block.aside, quote=True)}"'
    if block.table_html:
        inner = Table.parse(block.table_html).inner_html()
        return f"<table{attrs}>{inner}</table>"
    if block.rows is not None:
        inner = Table.of_rows(block.rows, block.header).inner_html()
        return f"<table{attrs}>{inner}</table>"
    if block.tag == "figure":
        return f"<figure{attrs}></figure>"
    return f"<{block.tag}{attrs}>{escape(block.text, quote=False)}</{block.tag}>"


def page_html(blocks: Sequence[Block], page: int) -> str:
    """A page's blocks as HTML — stage 1's output, and what a strategy is judged on."""
    return "".join(block_html(block, page) for block in blocks)


def document_html(groups: Sequence[Group]) -> str:
    """Stage 5: the whole document as one HTML string, sections and all, every tag numbered.

    The numbering is pre-order over the tree the sections make, which is exactly what the
    snapshot builder does to its nodes — so `data-nid` in this document and `Node.nid` in the
    database are the same number, and anything that reviews the document can point at a node.
    """
    numbers = Numbering()
    pieces: list[str] = []
    open_sections: list[int] = []
    for entry in groups:
        if entry.kind == "heading":
            level = entry.items[0].level or 2
            while open_sections and open_sections[-1] >= level:
                pieces.append("</section>")
                open_sections.pop()
            # The section takes the number before its heading: pre-order, parent first.
            pieces.append(f'<section {NID_ATTR}="{numbers.take()}">')
            open_sections.append(level)
        pieces.append(_element_html(entry, numbers))
    pieces += ["</section>"] * len(open_sections)
    return "".join(pieces)


# --- The pipeline --------------------------------------------------------------------------------


@dataclass
class Report:
    """What the stages did, for `DocumentContent.stats`."""

    pages: int = 0
    failed_pages: list[int] = field(default_factory=list)
    empty_pages: list[int] = field(default_factory=list)
    blocks: int = 0
    furniture: int = 0
    aside: int = 0
    dropped: int = 0
    noise: int = 0
    merged: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "failed_pages": self.failed_pages,
            "empty_pages": self.empty_pages,
            "blocks": self.blocks,
            "furniture": self.furniture,
            "aside": self.aside,
            "dropped": self.dropped,
            "noise": self.noise,
            "merged": self.merged,
            "problems": self.problems,
        }


def is_noise(block: Block) -> bool:
    """Whether a block is a scanner artefact rather than content (see `NOISE_LENGTH`)."""
    if block.rows or block.table_html or block.tag == "figure":
        return False
    text = block.text.strip()
    return len(text) <= NOISE_LENGTH and not any(character.isalnum() for character in text)


def read_pages(pages: Sequence[PageHtml], report: Report) -> dict[int, list[Block]]:
    """Stage 2: parse every page and keep what has content — furniture out, page label in."""
    kept: dict[int, list[Block]] = {}
    for page in sorted(pages, key=lambda p: p.number):
        report.pages += 1
        if page.failed:
            report.failed_pages.append(page.number)
            continue
        root, problems = read_html(page.html)
        report.problems += [f"page {page.number}: {problem}" for problem in problems]
        blocks: list[Block] = []
        for block in blocks_of(root, page=page.number):
            report.blocks += 1
            if block.furniture is not None:
                report.furniture += 1
                label = block.text.strip()
                if "number" in block.furniture and 0 < len(label) <= LABEL_MAX:
                    page.label = page.label or label
                continue
            has_content = bool(block.text.strip() or block.rows or block.table_html)
            if not has_content and block.tag != "figure":
                report.dropped += 1
                continue
            if is_noise(block):
                report.noise += 1
                continue
            if block.aside is not None:
                report.aside += 1
            blocks.append(block)
        if not blocks:
            report.empty_pages.append(page.number)
        kept[page.number] = blocks
    return kept


def join(kept: dict[int, list[Block]], report: Report, *, merge_tables: bool) -> list[Item]:
    """Stage 3: one stream of items, with what a page break split put back together."""
    items: list[Item] = []
    previous_page_had_content = False
    for number in sorted(kept):
        blocks = kept[number]
        for index, block in enumerate(blocks):
            last = items[-1] if items else None
            first_on_page = index == 0
            continues = (
                first_on_page
                and previous_page_had_content
                and last is not None
                and last.aside == block.aside
                and (
                    looks_continued(last, block)
                    if block.continues is None
                    else (
                        block.continues
                        and compatible(last.tag, block.tag, merge_tables=merge_tables)
                    )
                )
            )
            if continues and last is not None:
                _join(last, block, number)
                report.merged += 1
            else:
                items.append(_item(block, number))
        previous_page_had_content = bool(blocks)
    return items


@dataclass
class Group:
    """A stretch of items that becomes one top-level element."""

    kind: str  # heading | block | list | figure
    items: list[Item]
    caption: Item | None = None


def group(items: Sequence[Item]) -> list[Group]:
    """Stage 4: list items into one list, a caption onto its figure (after it, else before)."""
    groups: list[Group] = []
    index = 0
    while index < len(items):
        item = items[index]
        if item.tag == "li":
            run = [item]
            while index + 1 < len(items) and items[index + 1].tag == "li":
                index += 1
                run.append(items[index])
            groups.append(Group("list", run))
        elif item.tag == "figure":
            caption = None
            if index + 1 < len(items) and items[index + 1].tag == "figcaption":
                caption = items[index + 1]
                index += 1
            groups.append(Group("figure", [item], caption=caption))
        elif item.tag == "figcaption":
            if index + 1 < len(items) and items[index + 1].tag == "figure":
                groups.append(Group("figure", [items[index + 1]], caption=item))
                index += 1
            else:
                groups.append(Group("block", [_as(item, "p")]))
        elif item.tag in HEADINGS:
            groups.append(Group("heading", [item]))
        else:
            groups.append(Group("block", [item]))
        index += 1
    return groups


def _as(item: Item, tag: str) -> Item:
    item.tag = tag
    return item


def _regions(item: Item) -> list[ExtractedRegion]:
    return [
        ExtractedRegion(
            page=fragment.page,
            envelope=fragment.box,
            ring=fragment.ring,
            span=(fragment.start, fragment.end),
            words=fragment.words,
        )
        for fragment in item.fragments
        if fragment.box is not None or fragment.ring is not None
    ]


def _leaf(item: Item, tag: str | None = None) -> ExtractedNode:
    if item.table is not None:
        return ExtractedNode(
            tag="table",
            rows=item.table.rows_text(),
            header=bool(item.table.header),
            table_html=item.table.inner_html(),
            source=item.source,
            aside=item.aside,
            regions=_regions(item),
        )
    return ExtractedNode(
        tag=tag or item.tag,
        text=item.text,
        level=item.level,
        source=item.source,
        aside=item.aside,
        regions=_regions(item),
    )


def _node(entry: Group) -> ExtractedNode:
    match entry.kind:
        case "block":
            return _leaf(entry.items[0])
        case "list":
            return ExtractedNode(
                tag="ul",
                source=entry.items[0].source,
                children=[_leaf(item, "li") for item in entry.items],
            )
        case "figure":
            children = [_leaf(entry.caption, "figcaption")] if entry.caption else []
            return ExtractedNode(
                tag="figure",
                source=entry.items[0].source,
                regions=_regions(entry.items[0]),
                children=children,
            )
        case _:
            raise ValueError(f"not a body group: {entry.kind}")


def build_tree(groups: Sequence[Group]) -> list[ExtractedNode]:
    """Stage 5: the blocks in order, nested by their headings."""
    blocks: list[ExtractedNode] = []
    for entry in groups:
        if entry.kind == "heading":
            item = entry.items[0]
            level = item.level or 2
            blocks.append(_leaf(item, f"h{level}"))
        else:
            blocks.append(_node(entry))
    return nest_by_headings(blocks)


def assemble_html(pages: Iterable[PageHtml], *, merge_tables: bool = True) -> tuple[str, Report]:
    """Stages 2–5: the pages a strategy read, as one augmented HTML document."""
    ordered = sorted(pages, key=lambda page: page.number)
    report = Report()
    items = join(read_pages(ordered, report), report, merge_tables=merge_tables)
    return document_html(group(items)), report


def heading_title(html: str) -> str:
    """The document's first heading — what to call it when nothing smarter has a say."""
    root, _ = read_html(html)

    def first(element: Element) -> str:
        for child in element.children:
            if child.tag in HEADINGS and child.text.strip():
                return _collapse(child.text)
            found = first(child)
            if found:
                return found
        return ""

    return first(root)[:200]


def review(html: str) -> list[str]:
    """What is wrong with an augmented document, if anything — the gate before it is stored.

    Structure first (a tag left open, a tag outside the vocabulary, text loose in the
    document), then the attributes the rest of the pipeline relies on: every leaf says which
    page it is on, and every box is a box.
    """
    root, problems = read_html(html)
    if not root.children:
        problems.append("the document has no content")

    def walk(element: Element, depth: int) -> None:
        for child in element.children:
            leaf = child.tag not in CONTAINER_TAGS
            if leaf and not child.pages:
                problems.append(f"<{child.tag}> does not say which page it is on")
            for fragment in child.fragments:
                if fragment.box is None:
                    continue
                x0, y0, x1, y1 = fragment.box
                if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
                    problems.append(f"<{child.tag}> has a box outside the page: {fragment.box}")
            walk(child, depth + 1)

    walk(root, 0)
    return problems


def _regions_of(element: Element) -> list[ExtractedRegion]:
    return [
        ExtractedRegion(
            page=fragment.page,
            envelope=fragment.box,
            span=(fragment.start, fragment.end),
        )
        for fragment in element.fragments
    ] or [
        # No `data-box` at all: the tag still says which pages it is on, and a region with no
        # geometry is how the snapshot records that (`PageRegion`).
        ExtractedRegion(page=page, envelope=None, span=None)
        for page in element.pages
    ]


def _node_of(element: Element) -> ExtractedNode | None:
    """One element of the augmented document as a node of the extraction tree."""
    if element.tag in ("ul", "ol"):
        children = [node for node in map(_node_of, element.children) if node is not None]
        if not children:
            return None
        return ExtractedNode(tag="ul", source=StructureSource.DETECTED, children=children)
    if element.tag in ("section", "figure"):
        children = [node for node in map(_node_of, element.children) if node is not None]
        heading = next((c for c in children if c.tag in HEADINGS), None)
        return ExtractedNode(
            tag=element.tag,
            level=heading.level if heading is not None else None,
            source=StructureSource.DETECTED,
            children=children,
            regions=_regions_of(element) if element.tag == "figure" else [],
        )
    if element.tag == "table":
        table = Table.parse(f"<table>{element.inner_html}</table>")
        return ExtractedNode(
            tag="table",
            rows=table.rows_text(),
            header=bool(table.header),
            table_html=table.inner_html(),
            source=StructureSource.DETECTED,
            aside=element.aside,
            regions=_regions_of(element),
        )
    text = element.text if element.tag == "pre" else _collapse(element.text)
    if not text:
        return None
    return ExtractedNode(
        tag=element.tag,
        text=text,
        level=int(element.tag[1]) if element.tag in HEADINGS else None,
        source=StructureSource.DETECTED,
        aside=element.aside,
        regions=_regions_of(element),
    )


def extraction_from_html(
    html: str,
    pages: Iterable[PageHtml],
    *,
    meta: dict[str, Any] | None = None,
    raw: bytes | None = None,
    raw_mime: str = "application/json",
    stats: dict[str, Any] | None = None,
) -> Extraction:
    """Stage 6: the augmented document, in the form the snapshot builder writes as rows."""
    root, problems = read_html(html)
    ordered = sorted(pages, key=lambda page: page.number)
    nodes = [node for node in map(_node_of, root.children) if node is not None]
    return Extraction(
        nodes=nodes,
        pages=[
            ExtractedPage(
                number=page.number,
                label=page.label,
                width=page.width,
                height=page.height,
                meta=page.meta,
                thumbnail=page.thumbnail,
            )
            for page in ordered
        ],
        failed_pages=[page.number for page in ordered if page.failed],
        raw=raw,
        raw_mime=raw_mime,
        meta=meta or {},
        stats={**(stats or {}), "html_problems": problems},
    )


def assemble(
    pages: Iterable[PageHtml],
    *,
    merge_tables: bool = True,
    meta: dict[str, Any] | None = None,
    raw: bytes | None = None,
    raw_mime: str = "application/json",
    stats: dict[str, Any] | None = None,
    name: Callable[[str], str] | None = None,
) -> tuple[Extraction, str]:
    """The whole pipeline: pages in, and both the augmented HTML and the extraction out.

    What `review()` finds wrong with the assembled document is recorded in the report rather
    than sent to a model: an imperfect reading is stored as it is, and says so.

    `name` is the last step: the finished document goes in, what to call it comes out. Without
    one (or when it returns nothing) the first heading is the name.
    """
    ordered = sorted(pages, key=lambda page: page.number)
    html, report = assemble_html(ordered, merge_tables=merge_tables)
    report.problems += review(html)
    extraction = extraction_from_html(
        html,
        ordered,
        meta=meta,
        raw=raw,
        raw_mime=raw_mime,
        stats={**(stats or {}), "pipeline": report.as_dict()},
    )
    extraction.title = (name(html) if name is not None else "") or heading_title(html)
    return extraction, html
