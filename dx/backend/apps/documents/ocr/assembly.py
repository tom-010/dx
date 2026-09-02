"""Assembly — the reduce step: per-page block lists → the extraction tree. Pure, deterministic.

Input: one `PageInput` per PDF page, in order — the raw per-page JSON the map step produced
(or its failure) and the page's size. Output: an `Extraction` for the snapshot builder, with
the same raw JSON as its `raw` payload so a run can be replayed from `raw_output` bit for bit.

Steps, in order (Brief 03 §3): validate and normalize every block → split page furniture off
(a `page_number` becomes the page's label) → merge cross-page continuations (one item, one
fragment per page; a trailing artifact hyphen is dropped, otherwise a space joins — a German
compound split at a real hyphen loses it, an accepted v1 error) → group list items into a
list and captions with their figure → derive the section tree from the heading stream (an
outline stack: a heading of level L closes every open section of level ≥ L and opens its
own) → hand the builder a tree whose leaves carry one region per fragment. The model is
never asked about any of this.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from typing import Any

from pydantic import ValidationError

from apps.documents.extraction import (
    ExtractedNode,
    ExtractedPage,
    ExtractedRegion,
    Extraction,
)
from apps.documents.models import StructureSource
from apps.documents.ocr.page_schema import (
    FURNITURE,
    LABEL_MAX,
    BlockKind,
    Envelope,
    NormalizedBlock,
    normalize,
    validation_message,
)

# --- Pages as the map step produced them ---------------------------------------------------------


@dataclass
class PageInput:
    """One page of a run: the raw JSON (`{"number", "width", "height", "blocks": […]}`, or
    `{"number", "failed": true, "error": "…"}`) — what `out/raw/NNNN.json` and the
    `raw_output` blob hold — plus, on the strategy path, a thumbnail PNG."""

    number: int
    width: float | None = None
    height: float | None = None
    blocks: list[dict[str, Any]] | None = None
    error: str | None = None
    thumbnail: bytes | None = None

    @property
    def failed(self) -> bool:
        return self.blocks is None

    @classmethod
    def from_raw(cls, record: dict[str, Any]) -> PageInput:
        number = int(record["number"])
        if record.get("failed"):
            return cls(number=number, error=str(record.get("error") or "failed"))
        blocks = record.get("blocks")
        return cls(
            number=number,
            width=float(record["width"]) if record.get("width") is not None else None,
            height=float(record["height"]) if record.get("height") is not None else None,
            blocks=list(blocks) if isinstance(blocks, list) else [],
        )

    def to_raw(self) -> dict[str, Any]:
        if self.blocks is None:
            return {"number": self.number, "failed": True, "error": self.error or "failed"}
        return {
            "number": self.number,
            "width": self.width,
            "height": self.height,
            "blocks": self.blocks,
        }


def raw_payload(pages: Sequence[PageInput]) -> bytes:
    """`{"pages": [...]}` — the bytes both the command and the strategy keep; canonical JSON
    so the same pages give the same bytes."""
    payload = {"pages": [page.to_raw() for page in sorted(pages, key=lambda p: p.number)]}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=None).encode()


def pages_from_raw(raw: bytes) -> list[PageInput]:
    payload = json.loads(raw)
    records = payload["pages"] if isinstance(payload, dict) else payload
    return [PageInput.from_raw(record) for record in records]


# --- Tables --------------------------------------------------------------------------------------


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
    """The rows of a `<table>` the model returned: header rows (from `<thead>`, or a first
    row of `th` cells) and body rows. Serialized back deterministically."""

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
        """Rows of a continuation; a repeated header (same cell texts as ours) is dropped.
        Returns how many rows were added."""
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
            spans = {k: v for k, v in attrs}
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


# --- Items: content blocks after the cross-page merge --------------------------------------------


@dataclass
class Fragment:
    """One occurrence of an item on a page: its box there and the slice of the item's text
    (relative offsets) that the page holds."""

    page: int
    box: Envelope
    start: int
    end: int
    order: int


@dataclass
class Item:
    kind: BlockKind
    level: int | None
    text: str
    table: Table | None
    fragments: list[Fragment]


_MERGEABLE = {
    BlockKind.PARAGRAPH: {BlockKind.PARAGRAPH, BlockKind.OTHER},
    BlockKind.OTHER: {BlockKind.PARAGRAPH, BlockKind.OTHER},
    BlockKind.LIST_ITEM: {BlockKind.LIST_ITEM},
    BlockKind.CAPTION: {BlockKind.CAPTION},
    BlockKind.HEADING: {BlockKind.HEADING},
    BlockKind.TABLE: {BlockKind.TABLE},
}


def _compatible(previous: BlockKind, current: BlockKind, *, merge_tables: bool) -> bool:
    if current == BlockKind.TABLE and not merge_tables:
        return False
    return current in _MERGEABLE.get(previous, set())


def _new_item(block: NormalizedBlock, page: int) -> Item:
    table = Table.parse(block.table_html) if block.table_html else None
    text = table.text() if table is not None else block.text
    return Item(
        kind=block.kind,
        level=block.level,
        text=text,
        table=table,
        fragments=[Fragment(page, block.box, 0, len(text), block.index)],
    )


def _merge(item: Item, block: NormalizedBlock, page: int) -> None:
    """The continuation of `item` on the next page: text joined, a fragment added."""
    if item.table is not None and block.table_html:
        before = item.text
        item.table.append(Table.parse(block.table_html))
        item.text = item.table.text()
        start = len(before) + 1 if before and len(item.text) > len(before) else len(before)
        item.fragments.append(Fragment(page, block.box, start, len(item.text), block.index))
        return
    if item.text.endswith("-"):
        # A line-break hyphen at the page's end: dropped, the word runs on. (A compound split
        # at a real hyphen loses it here — accepted; the fixture documents it.)
        item.fragments[-1].end = len(item.text) - 1
        start = len(item.text) - 1
        item.text = item.text[:-1] + block.text
    else:
        start = len(item.text) + 1
        item.text = f"{item.text} {block.text}"
    item.fragments.append(Fragment(page, block.box, start, len(item.text), block.index))


@dataclass
class MergeResult:
    items: list[Item]
    labels: dict[int, str]
    stats: dict[str, Any]


def merge_pages(pages: Sequence[PageInput], *, merge_tables: bool = True) -> MergeResult:
    """Steps 1–3: normalize, split furniture off, merge continuations across pages."""
    items: list[Item] = []
    labels: dict[int, str] = {}
    anomalies: list[str] = []
    failed: list[int] = []
    furniture = 0
    merged = 0
    blocks_seen = 0
    previous_ok = False
    for page in sorted(pages, key=lambda p: p.number):
        if page.blocks is None:
            failed.append(page.number)
            previous_ok = False
            continue
        try:
            blocks, page_anomalies = normalize({"blocks": page.blocks}, page.number)
        except ValidationError as exc:
            failed.append(page.number)
            anomalies.append(f"page {page.number}: {validation_message(exc)}")
            previous_ok = False
            continue
        anomalies += page_anomalies
        blocks_seen += len(blocks)
        content: list[NormalizedBlock] = []
        for block in blocks:
            if block.kind in FURNITURE:
                furniture += 1
                label = block.text.strip()
                if block.kind == BlockKind.PAGE_NUMBER and 0 < len(label) <= LABEL_MAX:
                    labels.setdefault(page.number, label)
                continue
            content.append(block)
        for index, block in enumerate(content):
            last = items[-1] if items else None
            if (
                index == 0  # the first *content* block; furniture does not count
                and block.continues
                and previous_ok
                and last is not None
                and _compatible(last.kind, block.kind, merge_tables=merge_tables)
            ):
                _merge(last, block, page.number)
                merged += 1
            else:
                items.append(_new_item(block, page.number))
        previous_ok = True
    stats = {
        "pages": len(pages),
        "failed_pages": failed,
        "blocks": blocks_seen,
        "furniture": furniture,
        "merged": merged,
        "anomalies": anomalies,
    }
    return MergeResult(items=items, labels=labels, stats=stats)


# --- Grouping and the outline ------------------------------------------------------------------


@dataclass
class Group:
    """A stretch of items that becomes one top-level element: a heading, a paragraph, a table,
    a list (several items), or a figure with its caption."""

    kind: str  # heading | paragraph | table | list | figure
    items: list[Item]
    caption: Item | None = None


def group(items: Sequence[Item]) -> list[Group]:
    """Step 4: list items into lists, captions with their figure (after it, else before)."""
    groups: list[Group] = []
    index = 0
    while index < len(items):
        item = items[index]
        if item.kind == BlockKind.LIST_ITEM:
            run = [item]
            while index + 1 < len(items) and items[index + 1].kind == BlockKind.LIST_ITEM:
                index += 1
                run.append(items[index])
            groups.append(Group("list", run))
        elif item.kind == BlockKind.FIGURE:
            caption = None
            if index + 1 < len(items) and items[index + 1].kind == BlockKind.CAPTION:
                caption = items[index + 1]
                index += 1
            groups.append(Group("figure", [item], caption=caption))
        elif item.kind == BlockKind.CAPTION:
            if index + 1 < len(items) and items[index + 1].kind == BlockKind.FIGURE:
                groups.append(Group("figure", [items[index + 1]], caption=item))
                index += 1
            else:
                groups.append(Group("paragraph", [item]))  # an orphan caption is a paragraph
        elif item.kind == BlockKind.HEADING:
            groups.append(Group("heading", [item]))
        elif item.kind == BlockKind.TABLE and item.table is not None:
            groups.append(Group("table", [item]))
        else:
            groups.append(Group("paragraph", [item]))
        index += 1
    return groups


def _regions(item: Item) -> list[ExtractedRegion]:
    return [
        ExtractedRegion(page=f.page, envelope=f.box.as_tuple(), span=(f.start, f.end))
        for f in item.fragments
    ]


def _leaf(tag: str, item: Item, level: int | None = None) -> ExtractedNode:
    if item.table is not None:
        return ExtractedNode(
            tag="table",
            rows=item.table.rows_text(),
            header=bool(item.table.header),
            table_html=item.table.inner_html(),
            source=StructureSource.DETECTED,
            regions=_regions(item),
        )
    return ExtractedNode(
        tag=tag,
        text=item.text,
        level=level,
        source=StructureSource.DETECTED,
        regions=_regions(item),
    )


def _node(entry: Group) -> ExtractedNode:
    match entry.kind:
        case "paragraph":
            return _leaf("p", entry.items[0])
        case "table":
            return _leaf("table", entry.items[0])
        case "list":
            return ExtractedNode(
                tag="ul",
                source=StructureSource.DETECTED,
                children=[_leaf("li", item) for item in entry.items],
            )
        case "figure":
            children = [_leaf("figcaption", entry.caption)] if entry.caption is not None else []
            return ExtractedNode(
                tag="figure",
                source=StructureSource.DETECTED,
                regions=_regions(entry.items[0]),
                children=children,
            )
        case _:
            raise ValueError(f"not a body group: {entry.kind}")


def build_tree(groups: Sequence[Group]) -> list[ExtractedNode]:
    """Step 5, the outline stack: a heading of level L closes every open section of level
    ≥ L and opens a new one with the heading as its first child; everything else goes into
    the innermost open section, or the top level when none is open."""
    root = ExtractedNode(tag="section")
    stack: list[tuple[int, ExtractedNode]] = []
    for entry in groups:
        holder = stack[-1][1] if stack else root
        if entry.kind == "heading":
            item = entry.items[0]
            level = item.level or 2
            while stack and stack[-1][0] >= level:
                stack.pop()
            holder = stack[-1][1] if stack else root
            section = ExtractedNode(
                tag="section",
                level=level,
                source=StructureSource.DETECTED,
                children=[_leaf(f"h{level}", item, level)],
            )
            holder.children.append(section)
            stack.append((level, section))
        else:
            holder.children.append(_node(entry))
    return root.children


def assemble(pages: Iterable[PageInput], *, merge_tables: bool = True) -> Extraction:
    """The whole reduce step: pages in, the extraction tree (with the raw payload) out."""
    ordered = sorted(pages, key=lambda p: p.number)
    merged = merge_pages(ordered, merge_tables=merge_tables)
    nodes = build_tree(group(merged.items))
    return Extraction(
        nodes=nodes,
        pages=[
            ExtractedPage(
                number=page.number,
                label=merged.labels.get(page.number),
                width=page.width,
                height=page.height,
                thumbnail=page.thumbnail,
            )
            for page in ordered
        ],
        failed_pages=[page.number for page in ordered if page.failed],
        raw=raw_payload(ordered),
        raw_mime="application/json",
        stats={"ocr": merged.stats},
    )
