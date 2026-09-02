"""The page contract: what one Gemini request returns, validated and normalized.

The response schema is declared twice on purpose — as the pydantic model that validates what
came back (`PageBlocks`) and as the `google.genai` `Schema` the request constrains the model
with (`response_schema()`); `SCHEMA_VERSION` ties the two together and is part of the
extractor's identity (`GeminiOcrStrategy.config`).

**Geometry — the y-first trap**: Gemini's `box_2d` is `[ymin, xmin, ymax, xmax]`, integers on
a 0–1000 grid over the image that was sent. `envelope()` turns that into the snapshot's
`(x0, y0, x1, y1)` in [0, 1] — clamped, swapped when inverted — and is unit-tested for the
axis order, because this is what gets mixed up otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from google.genai import types

SCHEMA_VERSION = 1
GRID = 1000
#: `page_number` text longer than this is not a page label.
LABEL_MAX = 20


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"
    PAGE_NUMBER = "page_number"
    OTHER = "other"


FURNITURE = frozenset({BlockKind.PAGE_HEADER, BlockKind.PAGE_FOOTER, BlockKind.PAGE_NUMBER})


class Block(BaseModel):
    """One block as the model returns it. Unknown keys are ignored; types are enforced."""

    model_config = ConfigDict(extra="ignore")

    kind: BlockKind
    level: int | None = Field(default=None, ge=1, le=6)
    text: str = ""
    table_html: str | None = None
    continues_from_previous_page: bool = False
    box_2d: list[float] = Field(min_length=4, max_length=4)


class PageBlocks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    blocks: list[Block]


def response_schema() -> types.Schema:
    """The request-side mirror of `PageBlocks` (imported lazily: the SDK is heavy)."""
    from google.genai import types  # noqa: PLC0415 - only when a request is made

    block = types.Schema(
        type=types.Type.OBJECT,
        required=["kind", "text", "continues_from_previous_page", "box_2d"],
        properties={
            "kind": types.Schema(type=types.Type.STRING, enum=[k.value for k in BlockKind]),
            "level": types.Schema(
                type=types.Type.INTEGER, description="Heading level 1..6; headings only."
            ),
            "text": types.Schema(
                type=types.Type.STRING, description="Verbatim text; empty for a figure."
            ),
            "table_html": types.Schema(
                type=types.Type.STRING,
                description="Tables only: <table>…</table> with thead/tbody/tr/th/td.",
            ),
            "continues_from_previous_page": types.Schema(type=types.Type.BOOLEAN),
            "box_2d": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.INTEGER),
                description="[ymin, xmin, ymax, xmax], integers 0..1000 over the image.",
            ),
        },
    )
    return types.Schema(
        type=types.Type.OBJECT,
        required=["blocks"],
        properties={"blocks": types.Schema(type=types.Type.ARRAY, items=block)},
    )


# --- Geometry ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    x0: float
    y0: float
    x1: float
    y1: float
    #: What had to be done to the model's box to make it a box: "", "clamped", "swapped".
    fixes: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


def envelope(box_2d: list[float]) -> Envelope:
    """`[ymin, xmin, ymax, xmax]` on the 0–1000 grid → `(x0, y0, x1, y1)` in [0, 1]."""
    ymin, xmin, ymax, xmax = (float(v) / GRID for v in box_2d)
    fixes: list[str] = []
    values = [xmin, ymin, xmax, ymax]
    clamped = [min(max(v, 0.0), 1.0) for v in values]
    if clamped != values:
        fixes.append("clamped")
    x0, y0, x1, y1 = clamped
    if x0 > x1:
        x0, x1 = x1, x0
        fixes.append("swapped")
    if y0 > y1:
        y0, y1 = y1, y0
        if "swapped" not in fixes:
            fixes.append("swapped")
    return Envelope(x0, y0, x1, y1, tuple(fixes))


# --- Normalized blocks --------------------------------------------------------------------------


@dataclass
class NormalizedBlock:
    """A block after validation: a kind, its text (tables: the cells' text comes later from
    `table_html`), the envelope on its page, and whether it continues the previous page."""

    kind: BlockKind
    level: int | None
    text: str
    table_html: str | None
    continues: bool
    box: Envelope
    #: Position in the model's block sequence on the page (reading order; never re-sorted).
    index: int


def normalize(raw: dict[str, Any], page: int) -> tuple[list[NormalizedBlock], list[str]]:
    """Validate one page's raw JSON; drop what cannot be used; report what was odd.

    Raises `ValidationError` when the whole response does not fit the schema (the caller
    retries or fails the page); an individual block that is empty (no text, not a figure or
    table) is dropped and listed in the anomalies instead.
    """
    parsed = PageBlocks.model_validate(raw)
    blocks: list[NormalizedBlock] = []
    anomalies: list[str] = []
    for index, block in enumerate(parsed.blocks):
        text = block.text.strip()
        table_html = block.table_html.strip() if block.table_html else None
        if block.kind == BlockKind.TABLE and not table_html:
            if not text:
                anomalies.append(f"page {page} block {index}: table without table_html, dropped")
                continue
            anomalies.append(f"page {page} block {index}: table without table_html, kept as text")
        if not text and not table_html and block.kind != BlockKind.FIGURE:
            anomalies.append(f"page {page} block {index}: empty {block.kind.value}, dropped")
            continue
        level = block.level if block.kind == BlockKind.HEADING else None
        if block.kind == BlockKind.HEADING and level is None:
            level = 2
            anomalies.append(f"page {page} block {index}: heading without level, assumed 2")
        box = envelope(block.box_2d)
        if box.fixes:
            anomalies.append(f"page {page} block {index}: box {', '.join(box.fixes)}")
        blocks.append(
            NormalizedBlock(
                kind=block.kind,
                level=level,
                text=text,
                table_html=table_html,
                continues=block.continues_from_previous_page,
                box=box,
                index=index,
            )
        )
    return blocks, anomalies


def validation_message(error: ValidationError) -> str:
    """The validator's complaints as one line, for the repair prompt and the stats."""
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())
