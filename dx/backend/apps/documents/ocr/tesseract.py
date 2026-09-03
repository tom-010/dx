"""Tesseract as an extraction strategy: the local, offline counterpart to `gemini-ocr`.

Two OCR strategies for the same file is the point of the registry. Gemini reads a page the way
a person would — it sees the letterhead, the table, the signature block — and costs a network
call per page. Tesseract sees words and boxes and nothing else, runs on this machine, and is
free; what it gives up is structure, so every block comes out a paragraph.

The seam is the same for both (`apps/documents/pipeline.py`): a page becomes a list of `Block`s
with geometry, and the pipeline does the rest. So this module is only two steps —

    render the page (pdfium, `render.py`) → tesseract TSV → group words into paragraphs

— and everything after it, the pruning and joining and sectioning, is shared.

**TSV, not hOCR.** Tesseract's TSV output is one row per word with its box *and* its
confidence, which is exactly the shape `ExtractedWord` wants, and the words are what the
paragraph's envelope is computed from.

Known limit: those per-word boxes do **not** reach the snapshot. A `PagedStrategy` hands the
pipeline page *HTML*, and `data-box` carries one geometry per block (`page;x0,y0,x1,y1;
start,end`) — there is nowhere in that format to put a word. So `stats["words"]` is 0 and
`ConfStats` stays NULL, exactly as for `gemini-ocr`, and the real confidence tesseract has is
thrown away at the seam rather than by this module. Carrying it would mean extending the
interchange format, which is a change to every strategy, not to this one.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from io import StringIO

from apps.documents.extraction import Block, ExtractedWord, ExtractionError
from apps.documents.models import StructureSource

#: The binary. Not a Python binding: `pytesseract` is a `subprocess` call with a temp file
#: around it, and this way the dependency is the thing that actually has to be installed.
BINARY = "tesseract"
#: Page segmentation 1 = automatic with orientation and script detection — the right default
#: for a scan of unknown provenance. `--oem 1` is the LSTM engine.
ARGS = ("--psm", "1", "--oem", "1")
#: A word tesseract is this unsure of is noise (the scale is 0–100). It stays in the text —
#: dropping words silently would be worse — but the number travels with it.
TSV_COLUMNS = 12
#: Seconds for one page. A scan that takes longer than this is a scan that is not going to work.
TIMEOUT = 120


class TesseractMissing(ExtractionError):
    """The `tesseract` binary is not on PATH. Install it (Debian/Ubuntu:
    `apt install tesseract-ocr`, plus a language pack such as `tesseract-ocr-deu`)."""


def require_binary() -> str:
    """The path to `tesseract`, or a failure that says how to get one."""
    found = shutil.which(BINARY)
    if found is None:
        raise TesseractMissing(
            f"{BINARY!r} is not on PATH — install it "
            "(Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-deu)"
        )
    return found


def version() -> str:
    """`5.3.4` — part of the extractor's identity, so a snapshot says which tesseract read it."""
    out = subprocess.run(  # noqa: S603 - the binary is resolved by `shutil.which`
        [require_binary(), "--version"], capture_output=True, text=True, timeout=TIMEOUT
    )
    first = (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else ""
    return first.replace("tesseract", "").strip() or "unknown"


def pick_language(preferred: Sequence[str]) -> str:
    """The first of `preferred` that is actually installed.

    The corpus this app is for is German, so `deu` leads — but a language pack is an `apt
    install` away and the run must not fail because one is missing. Reading German with `eng`
    is visibly worse (`Miinchen` for `München`), which is why the choice is reported in the
    snapshot's `meta` rather than hidden.
    """
    installed = set(languages())
    for candidate in preferred:
        if candidate in installed:
            return candidate
    raise ExtractionError(
        f"tesseract has none of {list(preferred)} installed (it has: "
        f"{', '.join(sorted(installed)) or 'nothing'}) — "
        "apt install tesseract-ocr-deu"
    )


def languages() -> list[str]:
    """The language packs installed, so a run can fail on a language it does not have."""
    out = subprocess.run(  # noqa: S603 - the binary is resolved by `shutil.which`
        [require_binary(), "--list-langs"], capture_output=True, text=True, timeout=TIMEOUT
    )
    lines = (out.stdout or "").splitlines()[1:]
    return sorted(line.strip() for line in lines if line.strip())


@dataclass
class Word:
    """One row of tesseract's TSV: a word, where it is, and how sure the engine is."""

    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float
    block: int
    par: int
    line: int


def run_tsv(png: bytes, *, language: str) -> str:
    """One page image through tesseract, as TSV on stdout. Nothing touches the disk."""
    binary = require_binary()
    try:
        done = subprocess.run(  # noqa: S603 - the binary is resolved by `shutil.which`
            [binary, "stdin", "stdout", "-l", language, *ARGS, "tsv"],
            input=png,
            capture_output=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"tesseract timed out after {TIMEOUT}s") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ExtractionError(f"tesseract failed: {detail[-1] if detail else done.returncode}")
    return done.stdout.decode("utf-8", "replace")


def parse_tsv(tsv: str) -> list[Word]:
    """The word rows of a TSV page (`level == 5`), in reading order as tesseract gives them."""
    words: list[Word] = []
    for row in csv.DictReader(StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            words.append(
                Word(
                    text=text,
                    left=int(row["left"]),
                    top=int(row["top"]),
                    width=int(row["width"]),
                    height=int(row["height"]),
                    # -1 means "no confidence for this row"; treat it as no answer, not as 0.
                    conf=float(row["conf"]),
                    block=int(row["block_num"]),
                    par=int(row["par_num"]),
                    line=int(row["line_num"]),
                )
            )
        except KeyError, ValueError:  # a malformed row is a row, not a failed page
            continue
    return words


def blocks_of(words: list[Word], width: int, height: int) -> list[Block]:
    """Tesseract's words as the pipeline's blocks: one paragraph per (block, paragraph).

    Every block is a `<p>` and `source` is `DETECTED` rather than a claim about structure —
    tesseract reports layout, not meaning, and calling a line a heading because it is short
    and near the top is exactly the kind of guess that makes an artifact untrustworthy. The
    pipeline's own stages do what can be done from text alone.
    """
    if width <= 0 or height <= 0:
        return []
    blocks: list[Block] = []
    for key in dict.fromkeys((word.block, word.par) for word in words):
        group = [word for word in words if (word.block, word.par) == key]
        parts: list[str] = []
        boxes: list[ExtractedWord] = []
        cursor = 0
        for word in group:
            if parts:
                # A line break inside a paragraph is a space: the paragraph is the unit, and
                # where the scan broke the line is a fact about the paper, not about the text.
                parts.append(" ")
                cursor += 1
            start = cursor
            parts.append(word.text)
            cursor += len(word.text)
            boxes.append(
                ExtractedWord(
                    x0=word.left / width,
                    y0=word.top / height,
                    x1=(word.left + word.width) / width,
                    y1=(word.top + word.height) / height,
                    start=start,
                    end=cursor,
                    conf=word.conf / 100 if word.conf >= 0 else None,
                )
            )
        text = "".join(parts).strip()
        if not text:
            continue
        blocks.append(
            Block(
                tag="p",
                text=text,
                box=(
                    min(b.x0 for b in boxes),
                    min(b.y0 for b in boxes),
                    max(b.x1 for b in boxes),
                    max(b.y1 for b in boxes),
                ),
                words=boxes,
                source=StructureSource.DETECTED,
            )
        )
    return blocks


def read_page(png: bytes, width: int, height: int, *, language: str) -> tuple[list[Block], str]:
    """One page image as blocks, plus the raw TSV a rebuild can replay."""
    tsv = run_tsv(png, language=language)
    return blocks_of(parse_tsv(tsv), width, height), tsv
