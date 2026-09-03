"""The page contract: what Gemini is asked for, and how its answer becomes a page's blocks.

One request per page image, one answer: **semantic HTML for that page**, with a box on every
tag. HTML rather than a block schema because it is what a language model writes best and what
the artifact is anyway — nesting, tables and lists come out right, and the vocabulary is the
same one the sanitizer allows.

The model's boxes are Gemini's own convention, `[ymin, xmin, ymax, xmax]` on a 0–1000 grid over
the image it was sent. `parse_page` converts them once, here, into the pipeline's normalized
`(x0, y0, x1, y1)` — the y-first trap, guarded by its own test.

A page is read once. An answer that does not parse leaves its problems in the snapshot's stats;
nothing is sent back to a model to be reviewed or repaired.
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
SCHEMA_VERSION = 5
#: Gemini's boxes are integers on this grid, over the image that was sent.
GRID = 1000
#: What the model may mark as page furniture.
FURNITURE_KINDS = ("header", "footer", "page-number")

PROMPT = """You read one scanned page of a document and return it as semantic HTML.

Return HTML and nothing else: no <html>, <head> or <body>, no markdown fence, no commentary.

Tags — use only these: h1, h2, h3, h4, h5, h6, p, ul, ol, li, table, thead, tbody, tr, th, td,
figure, figcaption, blockquote, pre. Never <div>, <span>, <b>, <i>, <img> or <section>.

Transcribe what is on the page, in its original language (mostly German), in full and in
reading order: nothing translated, summarized, rephrased, reordered, added or left out.
Numbers, dates, codes and amounts exactly as printed. Handwriting is content, not decoration —
a margin note, a value written into a form, a remark beside a signature, a whole page of notes
are transcribed in place like any other block. Write [?] where a word is truly illegible.

**Let the markup carry what the layout says.**

- Columns whose lines line up across the page are a <table>, one row per line across, whether
  or not the paper prints any rules: a label and its value, a finding and its result, a drug
  and its dose. Writing them out one after another throws away what the page states. Use <th>
  for what the paper prints as a label, and colspan/rowspan where it merges cells.
- A heading is <h1>…<h6> by its visual rank; indented or bulleted matter is <ul>/<ol> with
  <li>, without the bullet glyph or the item number; a photo, stamp or signature is an empty
  <figure>, with any text beside it as <figcaption>.
- One tag per block. Join words a line break hyphenates, and lines a wrapped sentence
  continues; keep a trailing hyphen only where the word carries on to the next page.

**Attributes on every tag:**

- data-box="ymin,xmin,ymax,xmax": four integers from 0 to 1000 over the whole image, y first.
- data-furniture="header", "footer" or "page-number" on a running header, footer or page
  number. Transcribe them; just mark them.
- data-aside="letterhead", "contact", "bank", "legal" or "signature" on standing matter — the
  letterhead and practice name, the postal address, telephone, fax, email and web lines, bank
  details, tax and registration numbers, standard legal notices, the closing signature block.
  Never mark the substance: findings, diagnoses, history, measurements, the body of the
  letter, its salutation and its date stay unmarked.
- data-continues="true" on the page's first tag when it plainly begins in mid-sentence.

Set blank=true and return no html when the page carries nothing at all: an empty sheet, a
separator, the back of a page, scanner artefacts only. A page with one line on it is not blank.
"""

#: A model that wraps its answer in a fence anyway — strip it rather than fail the page.
_FENCE = re.compile(r"^\s*```(?:html)?\s*(?P<html>.*?)\s*```\s*$", re.DOTALL)


def strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group("html") if match else text.strip()


def prompt_for(number: int, total: int) -> str:
    """The message beside the page image."""
    return f"Page {number} of {total}."


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

Below is one document as HTML. Every tag carries data-nid, its number. For each tag that
states something, say when that information **originates** — the day, month or year it was
recorded, observed, decided or written — not the dates the text talks about. A note written in
1943 about the war of 1918 originates in 1943.

Take the first that applies: a date printed with the tag itself; the date governing its
section; the document's own date. In a dated letter that gives nearly every tag a date, most
of them the document's own. Leave a tag out when no date in the document could govern it.
Never invent a date the document does not support.

One entry per tag you can date:
- nid: the tag's data-nid
- edtf: EDTF (ISO 8601-2) — a day 1943-05-12, a month 1943-05, a year 1943, a range
  1943-05-12/1943-05-20, "before" ../1943-05-12, "after" 1943-05-12/... Never invent precision.
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

NAMING_PROMPT = """You name a document for the folder it is filed in.

Below is one document as HTML. Give it the label a person would write on the tab of that
folder, 3 to 8 words on one line, in the document's own language, no trailing punctuation:

1. what kind of document it is — Arztbrief, Befund, MRT-Befund, OP-Bericht, Laborbefund,
   Entlassungsbrief, Überweisung, Attest, Rezept, Rechnung, and so on;
2. its subject in two or three words: the body part, the diagnosis, the procedure, the matter;
3. who it is from: the specialty, the practice, the clinic or the company.

**Never name the person the folder belongs to** — these are one person's own documents, so the
patient's or addressee's name says nothing. Name the sender instead. **Leave the date out**; it
is shown beside the name already. Every word comes from the document: leave a part out rather
than filling it in. No "Scan of", no file extension.

Examples of the shape: "Arztbrief Orthopädie, Vorfußschmerzen links", "MRT-Befund linker
Vorfuß, Radiologie Kempten", "OP-Bericht Hallux valgus rechts", "Rechnung Physiotherapie".

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
