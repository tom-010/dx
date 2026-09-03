"""The tesseract strategy: the TSV adapter (pure), and the strategy end to end where the
binary is installed.

The parsing half needs no tesseract and always runs — it is where the bugs are (offsets,
normalized boxes, paragraph grouping). The end-to-end test skips where the binary is missing,
which is the honest answer for a strategy whose dependency is an apt package.
"""

import shutil
from pathlib import Path

import pytest
from django.core.management import call_command

from apps.documents import strategies
from apps.documents.extraction import ExtractionError
from apps.documents.ocr import tesseract
from apps.documents.tests.conftest import synthetic_pdf

HAS_TESSERACT = shutil.which(tesseract.BINARY) is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract is not installed")

#: Two words of one paragraph and one of another, in tesseract's own column order.
TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t200\t100\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t95.5\tBefund\n"
    "5\t1\t1\t1\t1\t2\t50\t20\t40\t10\t88\tlinks\n"
    "5\t1\t2\t1\t1\t1\t10\t60\t60\t10\t70\tUnterschrift\n"
)


def test_the_tsv_becomes_one_block_per_paragraph() -> None:
    words = tesseract.parse_tsv(TSV)
    assert [w.text for w in words] == ["Befund", "links", "Unterschrift"]

    blocks = tesseract.blocks_of(words, width=200, height=100)
    assert [b.text for b in blocks] == ["Befund links", "Unterschrift"]
    # Every block is a paragraph: tesseract reports layout, not meaning.
    assert {b.tag for b in blocks} == {"p"}


def test_word_offsets_index_into_the_block_text() -> None:
    """The offsets are what makes a word box point at a word rather than at a guess."""
    blocks = tesseract.blocks_of(tesseract.parse_tsv(TSV), width=200, height=100)
    first = blocks[0]
    assert first.words is not None
    assert [first.text[w.start : w.end] for w in first.words] == ["Befund", "links"]


def test_boxes_are_normalized_and_the_block_envelope_covers_its_words() -> None:
    blocks = tesseract.blocks_of(tesseract.parse_tsv(TSV), width=200, height=100)
    first = blocks[0]
    assert first.words is not None
    assert first.words[0].x0 == pytest.approx(10 / 200)
    assert first.words[0].y1 == pytest.approx(30 / 100)
    assert first.box == pytest.approx((10 / 200, 20 / 100, 90 / 200, 30 / 100))


def test_confidence_is_a_fraction_and_minus_one_means_no_answer() -> None:
    words = tesseract.parse_tsv(TSV)
    blocks = tesseract.blocks_of(words, width=200, height=100)
    assert blocks[0].words is not None
    assert blocks[0].words[0].conf == pytest.approx(0.955)

    unsure = TSV.replace("\t95.5\tBefund", "\t-1\tBefund")
    blocks = tesseract.blocks_of(tesseract.parse_tsv(unsure), width=200, height=100)
    assert blocks[0].words is not None
    assert blocks[0].words[0].conf is None  # -1 is "no answer", not "certainly wrong"


def test_a_malformed_row_is_skipped_not_fatal() -> None:
    good = "5\t1\t1\t1\t1\t2\t50\t20\t40\t10\t88\tlinks"
    broken = "5\t1\tx\t1\t1\t2\t\t\t\t\t\t"
    assert [w.text for w in tesseract.parse_tsv(TSV.replace(good, broken))] == [
        "Befund",
        "Unterschrift",
    ]


def test_a_missing_binary_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(tesseract.TesseractMissing, match="apt install tesseract-ocr"):
        tesseract.require_binary()


def test_a_missing_language_pack_is_a_failure_not_a_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tesseract, "languages", lambda: ["eng"])
    assert tesseract.pick_language(["deu", "eng"]) == "eng"  # the first that is installed
    with pytest.raises(ExtractionError, match="tesseract-ocr-deu"):
        tesseract.pick_language(["deu"])


def test_it_is_registered_but_is_nobody_s_default() -> None:
    """A scan read as unstructured paragraphs is a worse artifact than one read with its
    headings, so choosing it has to be a decision."""
    assert "tesseract" in strategies.strategy_names()
    assert strategies.mime_types_of("tesseract") == []
    assert isinstance(strategies.strategy_named("tesseract"), strategies.TesseractStrategy)


@needs_tesseract
def test_it_reads_a_pdf_without_touching_the_database() -> None:
    """`read_file` is the whole strategy: bytes in, tree out, no `Document` and no rows."""
    strategy = strategies.strategy_named("tesseract")
    extraction = strategy.read_file(synthetic_pdf(2), "application/pdf")

    assert len(extraction.pages) == 2
    assert extraction.nodes
    assert "Befund" in "".join(node.text for node in extraction.nodes)


@needs_tesseract
def test_the_command_prints_the_reading_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(synthetic_pdf(1))

    call_command("extract", str(scan), strategy="tesseract")

    printed = capsys.readouterr().out
    assert "Befund" in printed
