"""Gemini-vision OCR (Agent Brief 03 §9): the page contract, the deterministic assembly on
checked-in fixtures, the strategy's database path with a stub reader, replay parity, and a
live smoke test that only runs with a key."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from PIL import Image, ImageDraw

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import snapshot, strategies
from apps.documents.management.commands import ocr as ocr_command
from apps.documents.models import ExtractionStatus
from apps.documents.ocr import assembly, gemini_client, page_schema, render, run
from apps.documents.ocr.assembly import PageInput, Table, assemble
from apps.documents.ocr.gemini_client import PageFailed
from apps.documents.ocr.page_schema import BlockKind, envelope, normalize
from apps.documents.tests.conftest import upload
from config.env import env

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
GOLDEN = FIXTURES / "golden"


def load_pages(name: str = "golden") -> list[PageInput]:
    records = [json.loads(p.read_text()) for p in sorted((FIXTURES / name / "raw").glob("*.json"))]
    return [PageInput.from_raw(record) for record in records]


def block(
    kind: str, text: str = "", box: tuple[int, int, int, int] = (0, 0, 100, 1000), **extra: object
) -> dict[str, Any]:
    ymin, xmin, ymax, xmax = box
    return {
        "kind": kind,
        "text": text,
        "continues_from_previous_page": False,
        "box_2d": [ymin, xmin, ymax, xmax],
        **extra,
    }


def page(number: int, *blocks: dict[str, Any]) -> PageInput:
    return PageInput(number=number, width=595.0, height=842.0, blocks=list(blocks))


def continues(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "continues_from_previous_page": True}


# --- §9.2 the y-first guard ---------------------------------------------------------------------


def test_box_2d_is_y_first_on_a_thousand_grid() -> None:
    box = envelope([100, 200, 300, 400])
    assert box.as_tuple() == (0.2, 0.1, 0.4, 0.3)
    assert box.fixes == ()
    clamped = envelope([-10, 0, 1200, 500])
    assert clamped.as_tuple() == (0.0, 0.0, 0.5, 1.0) and "clamped" in clamped.fixes
    swapped = envelope([300, 400, 100, 200])
    assert swapped.as_tuple() == (0.2, 0.1, 0.4, 0.3) and "swapped" in swapped.fixes


def test_normalize_validates_and_drops_what_it_cannot_use() -> None:
    blocks, anomalies = normalize(
        {
            "blocks": [
                block("paragraph", "  "),
                block("figure", ""),
                block("heading", "Titel"),
                block("table", "", table_html="<table></table>"),
                {**block("paragraph", "x"), "unknown": 1},
            ]
        },
        7,
    )
    assert [b.kind for b in blocks] == [
        BlockKind.FIGURE,
        BlockKind.HEADING,
        BlockKind.TABLE,
        BlockKind.PARAGRAPH,
    ]
    assert blocks[1].level == 2
    assert anomalies == [
        "page 7 block 0: empty paragraph, dropped",
        "page 7 block 2: heading without level, assumed 2",
    ]
    with pytest.raises(Exception, match="kind"):
        normalize({"blocks": [block("poem", "x")]}, 1)


# --- §9.1 / §9.11 the golden fixture ------------------------------------------------------------


def _build() -> snapshot.Built:
    return snapshot.build(assemble(load_pages()))


def test_golden_fixture_assembles_byte_exactly() -> None:
    """Regenerate with `OCR_GOLDEN_WRITE=1 pytest apps/documents/tests/test_ocr.py -k golden`
    after an intended assembly change — and read the diff."""
    built = _build()
    files = {
        "content.html": built.html,
        "content.txt": built.text,
        "nodes.json": json.dumps(snapshot.payload(built), ensure_ascii=False, indent=2) + "\n",
    }
    expected = GOLDEN / "expected"
    if os.environ.get("OCR_GOLDEN_WRITE"):
        expected.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (expected / name).write_text(text)
    for name, text in files.items():
        assert (expected / name).read_text() == text, name
    again = _build()
    assert (again.html, again.text) == (built.html, built.text)  # deterministic


def test_golden_fixture_keeps_every_invariant() -> None:
    built = _build()
    ocr = built.stats["ocr"]
    assert isinstance(ocr, dict) and ocr["anomalies"] == []
    assert built.stats["failed_pages"] == []
    for node in built.nodes:
        for region in node.regions:
            assert (
                node.text_start
                <= (region.text_start or 0)
                <= (region.text_end or 0)
                <= node.text_end
            )
        if not node.children and node.tag != "figure":
            assert node.regions, f"leaf #{node.nid} <{node.tag}> has no region"
        assert built.html[node.html_start : node.html_end].startswith(f"<{node.tag}")


# --- §9.3 / §9.4 cross-page merge -----------------------------------------------------------------


def test_a_cross_page_paragraph_is_one_node_with_two_regions() -> None:
    built = _build()
    (merged,) = [
        n
        for n in built.nodes
        if n.tag == "p" and "zunehmende" in built.text[n.text_start : n.text_end]
    ]
    text = built.text[merged.text_start : merged.text_end]
    assert (
        text == "Die Patientin klagt seit drei Wochen über zunehmende Beschwerden im rechten Knie, "
        "vor allem nachts."
    )
    assert [r.page for r in merged.regions] == [1, 2]
    first, second = merged.regions
    assert (
        built.text[first.text_start : first.text_end]
        == "Die Patientin klagt seit drei Wochen über zuneh"
    )
    assert (
        built.text[second.text_start : second.text_end]
        == "mende Beschwerden im rechten Knie, vor allem nachts."
    )
    assert 'data-pages="1,2"' in built.html[merged.html_start : merged.html_end]


def test_the_hyphen_join_drops_a_line_break_hyphen_and_keeps_a_space_otherwise() -> None:
    joined = assembly.merge_pages(
        [
            page(1, block("paragraph", "Die Ost-")),
            page(2, continues(block("paragraph", "West-Achse ist frei."))),
            page(3, continues(block("paragraph", "Wirklich."))),
        ]
    )
    (item,) = joined.items
    # Documented v1 error: a compound split at a real hyphen loses it ("Ost-West-Achse").
    assert item.text == "Die OstWest-Achse ist frei. Wirklich."
    assert [(f.page, f.start, f.end) for f in item.fragments] == [
        (1, 0, 7),
        (2, 7, 27),
        (3, 28, 37),
    ]
    assert joined.stats["merged"] == 2
    kept_apart = assembly.merge_pages(
        [page(1, block("paragraph", "Ende.")), page(2, continues(block("heading", "Neu", level=2)))]
    )
    assert len(kept_apart.items) == 2  # a heading never continues a paragraph


# --- §9.5 / §9.6 the outline ----------------------------------------------------------------------


def _outline(nodes: list[snapshot._Planned]) -> list[tuple[str, str, int | None]]:
    return [(n.path, n.tag, n.level()) for n in nodes if n.tag == "section"]


def test_two_headings_on_a_page_are_siblings_and_a_bottom_heading_holds_the_next_page() -> None:
    built = _build()
    sections = _outline(built.nodes)
    assert sections == [
        ("0001", "section", 1),  # Arztbrief
        ("0001.0003", "section", 2),  # Anamnese
        ("0001.0004", "section", 2),  # Befund
        ("0001.0004.0004", "section", 4),  # Nebenbefund: the deep jump nests under Befund
        ("0001.0005", "section", 2),  # Verlauf
        ("0001.0006", "section", 2),  # Beurteilung, heading at the bottom of page 4
    ]
    by_path = {n.path: n for n in built.nodes}
    assessment = by_path["0001.0006"]
    body = [c for c in assessment.children if c.tag == "p"]
    assert [built.text[c.text_start : c.text_end][:30] for c in body] == [
        "Es besteht der Verdacht auf ei",
        "gez. Dr. Muster",
    ]
    assert sorted(assessment.pages) == [4, 5]
    assert assessment.title(built.text) == "Beurteilung"


# --- §9.7 tables ----------------------------------------------------------------------------------


def test_a_table_across_pages_concatenates_rows_and_drops_the_repeated_header() -> None:
    built = _build()
    (table,) = [n for n in built.nodes if n.tag == "table"]
    assert (
        built.text[table.text_start : table.text_end]
        == "Parameter\tWert\nCRP\t12 mg/l\nLeukozyten\t9,1 /nl\nHb\t13,4 g/dl"
    )
    html = built.html[table.html_start : table.html_end]
    assert html.count("<th>Parameter</th>") == 1 and "<td>Hb</td>" in html
    assert [r.page for r in table.regions] == [2, 3]
    assert built.text[table.regions[1].text_start : table.regions[1].text_end] == "Hb\t13,4 g/dl"
    parsed = Table.parse(
        '<table><tr><th>a</th><th colspan="2">b</th></tr>'
        "<tr><td>1</td><td>2</td><td>3<br>x</td></tr></table>"
    )
    assert parsed.header and parsed.inner_html() == (
        '<thead><tr><th>a</th><th colspan="2">b</th></tr></thead>'
        "<tbody><tr><td>1</td><td>2</td><td>3 x</td></tr></tbody>"
    )
    apart = assembly.merge_pages(load_pages(), merge_tables=False)
    assert sum(1 for item in apart.items if item.table is not None) == 2


# --- §9.8 furniture -------------------------------------------------------------------------------


def test_furniture_is_dropped_and_page_numbers_become_labels() -> None:
    built = _build()
    assert "Klinik Musterstadt" not in built.html and "Vertraulich" not in built.text
    assert [(p.number, p.item.label) for p in built.pages] == [
        (1, "1"),
        (2, "2"),
        (3, "3"),
        (4, "4"),
        (5, "5"),
    ]
    ocr, dating = built.stats["ocr"], built.stats["dating"]
    assert isinstance(ocr, dict) and ocr["furniture"] == 11
    figure = next(n for n in built.nodes if n.tag == "figure")
    assert [c.tag for c in figure.children] == ["figcaption"] and figure.regions
    assert isinstance(dating, dict) and dating["anchors"] == []  # mid-sentence: no dateline


# --- §9.9 / §9.10 the strategy and replay parity --------------------------------------------------


class StubReader:
    """Answers from the fixture; page 2 refuses when asked to."""

    def __init__(self, pages: list[PageInput], *, fail: frozenset[int] = frozenset()) -> None:
        self.pages = {page.number: page for page in pages}
        self.fail = fail
        self.calls: list[int] = []

    def read(self, png: bytes, number: int, total: int, tail: str) -> dict[str, Any]:
        self.calls.append(number)
        assert png.startswith(b"\x89PNG") and total >= number
        if number in self.fail:
            raise PageFailed("refused")
        return {"blocks": self.pages[number].blocks or []}


def synthetic_pdf(pages: int = 2) -> bytes:
    """A small scan-like PDF: pages with a printed title and a paragraph, made with Pillow."""
    images = []
    for number in range(1, pages + 1):
        image = Image.new("RGB", (595, 842), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 60), f"Seite {number}: Befund", fill="black")
        draw.text((60, 120), "Die Patientin ist beschwerdefrei.", fill="black")
        images.append(image)
    from io import BytesIO

    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


@pytest.mark.django_db
def test_the_strategy_writes_a_snapshot_and_a_failed_page_makes_it_partial(user: User) -> None:
    fixture = load_pages()[:2]
    reader = StubReader(fixture, fail=frozenset({2}))
    strategy = strategies.GeminiOcrStrategy(reader=reader)
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategy)
        assert content.status == ExtractionStatus.PARTIAL, content.error
        assert reader.calls == [1, 2]
        assert content.stats["ocr"]["failed_pages"] == [2] and content.stats["failed_pages"] == [2]
        assert [p.number for p in content.pages.all()] == [1, 2]  # the failed page exists
        assert content.pages.get(number=2).regions.count() == 0
        assert content.pages.get(number=1).thumbnail is not None
        assert content.conf_stats is None and content.node(1).conf_stats is None
        assert snapshot.verify_snapshot(content) == []
        assert document.html.startswith('<section data-nid="1" data-pages="1"')
        assert document.thumbnail_id == content.pages.get(number=1).thumbnail_id
        assert content.raw_output is not None
        stored = json.loads(content.raw_output.read_bytes())
        assert [p["number"] for p in stored["pages"]] == [1, 2] and stored["pages"][1]["failed"]

        everything_fails = snapshot.extract_now(
            document,
            strategies.GeminiOcrStrategy(reader=StubReader(fixture, fail=frozenset({1, 2}))),
        )
        assert everything_fails.status == ExtractionStatus.FAILED
        assert "no page could be read" in everything_fails.error


@pytest.mark.django_db
def test_a_production_run_replays_bit_for_bit_through_assemble(user: User, tmp_path: Path) -> None:
    fixture = load_pages()[:2]
    strategy = strategies.GeminiOcrStrategy(reader=StubReader(fixture))
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategy)
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert content.raw_output is not None
        raw = content.raw_output.read_bytes()
        html = content.html
        # A rebuild replays the raw output: no reader call, the same html.
        reader = StubReader(fixture)
        rebuilt = snapshot.extract_now(
            document, strategies.GeminiOcrStrategy(reader=reader), from_raw=True
        )
        assert reader.calls == [] and rebuilt.html == html and rebuilt.pk != content.pk

    out = tmp_path / "out"
    for record in json.loads(raw)["pages"]:
        run.write_raw(out, PageInput.from_raw(record), None)
    result = CliRunner().invoke(ocr_command.command, ["assemble", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "content.html").read_text() == html
    assert (out / "content.txt").read_text() == content.text
    assert (out / "preview" / "0001.html").exists() and (out / "nodes.json").exists()


def test_the_command_needs_a_key_and_raw_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "GEMINI_API_KEY", None)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(synthetic_pdf(1))
    runner = CliRunner()
    result = runner.invoke(ocr_command.command, ["extract", str(pdf), "--out", str(tmp_path / "o")])
    assert result.exit_code == 1 and "GEMINI_API_KEY" in result.output
    result = runner.invoke(ocr_command.command, ["assemble", "--out", str(tmp_path / "o")])
    assert result.exit_code == 1 and "no raw pages" in result.output


def test_extract_resumes_from_existing_raw_json(tmp_path: Path) -> None:
    fixture = load_pages()[:2]
    reader = StubReader(fixture)
    pdf = synthetic_pdf(2)
    out = tmp_path / "out"
    first = list(run.read_document(pdf, reader, on_page=lambda p, png: run.write_raw(out, p, png)))
    assert [p.number for p in first] == [1, 2] and reader.calls == [1, 2]
    assert (out / "pages" / "0002.png").exists() and (out / "raw" / "0002.json").exists()
    again = list(run.read_document(pdf, reader, existing=run.existing_raw(out)))
    assert reader.calls == [1, 2] and [p.number for p in again] == [1, 2]  # nothing sent again
    assert render.parse_page_range("1-2, 9", 2) == [1, 2] and render.page_count(pdf) == 2
    assert len(render.thumbnail_png(Image.new("RGB", (2000, 1000)), 600)) > 0


def test_tail_context_and_prompt_identity() -> None:
    blocks, _ = normalize({"blocks": [block("paragraph", "x" * 900)]}, 1)
    tail = gemini_client.tail_context(blocks)
    assert (
        tail.startswith("paragraph: ")
        and len(tail) == len("paragraph: ") + gemini_client.TAIL_CHARS
    )
    assert gemini_client.tail_context([]) == "the previous page was blank"
    assert len(gemini_client.prompt_sha256()) == 64
    config = strategies.GeminiOcrStrategy.config
    assert config["prompt_sha256"] == gemini_client.prompt_sha256()
    assert config["schema_version"] == page_schema.SCHEMA_VERSION


# --- §9.12 live -----------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.filterwarnings("ignore:'_UnionGenericAlias' is deprecated:DeprecationWarning")
def test_one_synthetic_page_end_to_end_with_gemini(tmp_path: Path) -> None:
    if not env.GEMINI_API_KEY:
        pytest.skip("GEMINI_API_KEY is not set")
    reader = gemini_client.GeminiPageReader(env.GEMINI_API_KEY)
    (result,) = run.read_document(synthetic_pdf(1), reader)
    assert not result.failed, result.error
    built = snapshot.build(assemble([result]))
    assert "beschwerdefrei" in built.text.lower()
    assert built.stats["failed_pages"] == []
