"""The page contract: what Gemini is asked for, and how its answer becomes a page's blocks.

One request per page image, one answer: **semantic HTML for that page**, with a box on every
tag. HTML rather than a block schema because it is what a language model writes best and what
the artifact is anyway — nesting, tables and lists come out right, and the vocabulary is the
same one the sanitizer allows.

The model's boxes are Gemini's own convention, `[ymin, xmin, ymax, xmax]` on a 0–1000 grid over
the image it was sent. `parse_page` converts them once, here, into the pipeline's normalized
`(x0, y0, x1, y1)` — the y-first trap, guarded by its own test.

An answer that does not parse is not thrown away: `gemini_client.repair_html` asks a flash
model to fix it, and only a second failure fails the page.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from apps.documents import pipeline
from apps.documents.extraction import Block
from apps.documents.models import ALLOWED_TAGS, HEADINGS, StructureSource

if TYPE_CHECKING:
    from google.genai import types

#: Bumped with the prompt: both are the extractor's identity (`GeminiOcrStrategy.config`).
SCHEMA_VERSION = 3
#: Gemini's boxes are integers on this grid, over the image that was sent.
GRID = 1000
#: What the model may mark as page furniture.
FURNITURE_KINDS = ("header", "footer", "page-number")

PROMPT = """You read one scanned page of a document and return it as semantic HTML.

Return the page as HTML, nothing else: no <html>, <head> or <body>, no markdown fence, no
commentary.

Tags — use only these: section, h1, h2, h3, h4, h5, h6, p, ul, ol, li, table, thead, tbody, tr,
th, td, figure, figcaption, blockquote, pre. Never <div>, <span>, <b>, <i> or <img>.

**Read the page, do not decode it glyph by glyph.** You know the language, the vocabulary and
the kind of document in front of you: use that knowledge to read what is printed, exactly as a
person reads through a bad photocopy. A smudged or broken word is still the word it plainly is:
"Bankverbindunq" is "Bankverbindung", "Är.?tbank" is "Ärztebank", "Grui'!dgelenk" is
"Grundgelenk", "Vofußschmerzen" is "Vorfußschmerzen". Domain terms, drug names, anatomical
terms, addresses and numbers are to be read with the same care.

**But never invent.** Transcribe what is on the page in its original language (mostly German),
in full: no translation, no summary, no rephrasing, nothing added and nothing left out. If a
word truly cannot be made out, keep your best reading of the characters rather than guessing at
the meaning. Numbers, dates, codes and amounts are copied exactly as printed — never corrected,
never rounded, never reformatted.

Rules:
- Keep the reading order. Several columns: column by column, top to bottom within a column.
- One tag per block: a paragraph is <p>, a heading is <h1>…<h6> by its visual rank, a list item
  is <li> inside <ul> (or <ol> when the items are numbered), a table is <table> with
  thead/tbody/tr/th/td, colspan and rowspan allowed, cell text verbatim.
- Leave the bullet glyph or the item number out of the <li> text.
- Do not invent <section> tags: return the blocks in order and nothing around them.
- Within the page, join words that a line break hyphenates. Keep a hyphen at the very end of
  the last block only when the word continues on the next page.
- A figure, photo, stamp, signature or handwriting you cannot read is an empty <figure></figure>;
  a caption beside it is <figcaption>.
- Running headers, footers and page numbers are transcribed too, as
  <p data-furniture="header">, <p data-furniture="footer"> or
  <p data-furniture="page-number">.
- Every tag directly in the page carries data-box="ymin,xmin,ymax,xmax": four integers from 0
  to 1000 over the whole image, y first.
- The page's first tag carries data-continues="true" when it grammatically continues the last
  block of the previous page, quoted in the message. Otherwise leave the attribute out.
- Set blank=true and return no html when the page carries no content at all: an empty sheet, a
  separator, a scan of the back of a page, speckles and scanner artefacts only. A page with a
  single line of text on it is not blank.
"""

REPAIR_PROMPT = """The following HTML is the transcription of one page, but it is broken:

{problems}

Return the same content as valid HTML, changing nothing but the markup: keep every word, every
attribute (data-box, data-furniture, data-continues) and the order. Use only these tags:
section, h1-h6, p, ul, ol, li, table, thead, tbody, tr, th, td, figure, figcaption, blockquote,
pre. Return the HTML alone.

{html}
"""

#: A model that wraps its answer in a fence anyway — strip it rather than fail the page.
_FENCE = re.compile(r"^\s*```(?:html)?\s*(?P<html>.*?)\s*```\s*$", re.DOTALL)


def strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group("html") if match else text.strip()


def prompt_for(number: int, total: int, tail: str) -> str:
    return f"Page {number} of {total}.\nLast block of the previous page — {tail}"


def gemini_box(value: str) -> tuple[float, float, float, float] | None:
    """`ymin,xmin,ymax,xmax` on the 0–1000 grid → `(x0, y0, x1, y1)` in [0, 1].

    Clamped, and swapped when the model returns a box inside out. Returns None for anything
    that is not four numbers — a box is an aid, never a reason to lose the text.
    """
    numbers = [piece.strip() for piece in value.replace(" ", ",").split(",") if piece.strip()]
    if len(numbers) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (float(number) / GRID for number in numbers)
    except ValueError:
        return None
    x0, x1 = sorted((_unit(xmin), _unit(xmax)))
    y0, y1 = sorted((_unit(ymin), _unit(ymax)))
    return (x0, y0, x1, y1)


def _unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def parse_page(html: str, number: int) -> tuple[list[Block], list[str]]:
    """One page's HTML from the model as blocks, with what was wrong with it.

    The blocks are the pipeline's own kind, so from here on an OCR'd scan and a born-digital
    PDF go through exactly the same stages.
    """
    root, problems = pipeline.read_html(strip_fence(html))
    blocks: list[Block] = []
    for block in pipeline.blocks_of(root, page=number, box_reader=gemini_box):
        if block.tag not in ALLOWED_TAGS and block.tag not in HEADINGS:
            problems.append(f"<{block.tag}> is not part of the vocabulary")
            continue
        blocks.append(block)
    return blocks, problems


def as_page_html(html: str, number: int) -> tuple[str, list[str]]:
    """The model's page as the canonical page fragment the pipeline reads: our own `data-box`,
    our own furniture names, everything else untouched."""
    blocks, problems = parse_page(html, number)
    for block in blocks:
        block.source = StructureSource.DETECTED
    return pipeline.page_html(blocks, number), problems


def response_schema() -> types.Schema:
    """The page as HTML, and whether it carries anything at all. Structured output guarantees
    the shape of the *answer*; what is inside it is what `parse_page` checks (imported lazily
    — the SDK is heavy)."""
    from google.genai import types  # noqa: PLC0415 - only when a request is made

    return types.Schema(
        type=types.Type.OBJECT,
        required=["html", "blank"],
        properties={
            "html": types.Schema(
                type=types.Type.STRING,
                description="The page as semantic HTML; empty when the page is blank.",
            ),
            "blank": types.Schema(
                type=types.Type.BOOLEAN,
                description="True when the page carries no content: an empty or scanned-back "
                "sheet, a separator, scanner artefacts only.",
            ),
        },
    )


# --- Dating the document -------------------------------------------------------------------------

DATING_PROMPT = """You date the information in a document.

Below is one document as HTML. Every tag carries data-nid, its number.

**Date every tag that states something.** For each one, say when that information
**originates**: the day, month or year the content was recorded, observed, decided or written.
This is not about the dates the text talks *about*. A note written in 1943 about the war of
1918 originates in 1943. A history taken today that recalls an operation in 2022 originates
today; only a finding *printed* with its own date originates on that date.

Work out each tag's date in this order, and stop at the first that applies:
1. a date printed with the tag itself, or at the start of its own line;
2. the date governing the section or list it sits in;
3. the document's own date — a letter's dateline, a report's date, the date on the letterhead.

In a dated letter or report that means nearly every tag gets a date, most of them the
document's own. Leave a tag out only when the document carries no date that could govern it at
all, or when the tag is a heading with nothing under it yet. Never invent a date the document
does not support, and never take a date out of a sentence that merely mentions one.

Answer with one entry per tag you can date:
- nid: the tag's data-nid
- edtf: the date in EDTF (ISO 8601-2). A day is 1943-05-12, a month 1943-05, a year 1943, a
  range 1943-05-12/1943-05-20, "before" ../1943-05-12, "after" 1943-05-12/... Never invent
  precision the document does not give.
- confidence: 0 to 1, how sure you are that this date governs this tag.

{html}
"""


def dating_schema() -> types.Schema:
    """One date per tag the model can place (imported lazily — the SDK is heavy)."""
    from google.genai import types  # noqa: PLC0415 - only when a request is made

    return types.Schema(
        type=types.Type.OBJECT,
        required=["dates"],
        properties={
            "dates": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    required=["nid", "edtf", "confidence"],
                    properties={
                        "nid": types.Schema(type=types.Type.INTEGER),
                        "edtf": types.Schema(type=types.Type.STRING),
                        "confidence": types.Schema(type=types.Type.NUMBER),
                    },
                ),
            )
        },
    )


# --- Naming the document -------------------------------------------------------------------------

NAMING_PROMPT = """You name a document.

Below is one document as HTML. Give it the name a person filing it would write on the tab: what
kind of document it is, who or what it is about, and when — in its own language, in one line.

- 3 to 8 words, no trailing punctuation, no quotation marks.
- Take the words from the document. Never invent a fact it does not state.
- A date belongs in the name when the document is dated; write it as it appears.
- No file extension, no "Scan of", no "Document about".

Examples of the shape: "Arztbrief Orthopädie Kempten, 05.08.2026", "Tagebucheintrag 12. Mai
1943", "Rechnung Stadtwerke März 2026".

{html}
"""


def naming_schema() -> types.Schema:
    """One line: the document's name (imported lazily — the SDK is heavy)."""
    from google.genai import types  # noqa: PLC0415 - only when a request is made

    return types.Schema(
        type=types.Type.OBJECT,
        required=["title"],
        properties={"title": types.Schema(type=types.Type.STRING)},
    )
