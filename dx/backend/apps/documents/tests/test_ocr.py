"""Gemini-vision OCR (Agent Brief 03 §9): the page contract, the deterministic pipeline on
checked-in fixtures, the strategy's database path with a stub reader, replay parity, and a
live smoke test that only runs with a key.

The fixtures are page HTML as the model returns it — the contract this strategy is judged on.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from django.db import ProgrammingError, connection
from PIL import Image

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import api, pipeline, snapshot, strategies
from apps.documents.dating import DateSource
from apps.documents.management.commands import ocr as ocr_command
from apps.documents.models import (
    Document,
    DocumentContentDraft,
    ExtractionState,
    ExtractionStatus,
)
from apps.documents.ocr import gemini_client, page_html, render, run
from apps.documents.ocr.assembly import PageInput, page_contents, raw_payload
from apps.documents.ocr.gemini_client import PageFailed
from apps.documents.ocr.page_html import gemini_box, parse_page
from apps.documents.tests.conftest import synthetic_pdf, text_pdf, upload
from config.env import env

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
GOLDEN = FIXTURES / "golden"


def load_pages(name: str = "golden") -> list[PageInput]:
    records = [json.loads(p.read_text()) for p in sorted((FIXTURES / name / "raw").glob("*.json"))]
    return [PageInput.from_raw(record) for record in records]


def box(ymin: int, xmin: int, ymax: int, xmax: int) -> str:
    """`data-box` as the model writes it: y first, on a 0–1000 grid."""
    return f'data-box="{ymin},{xmin},{ymax},{xmax}"'


def page(number: int, html: str) -> PageInput:
    return PageInput(number=number, width=595.0, height=842.0, html=html)


def build(pages: list[PageInput] | None = None) -> tuple[str, pipeline.Report]:
    """The augmented document for some pages — the pipeline's own product."""
    contents, _ = page_contents(pages if pages is not None else load_pages())
    return pipeline.assemble_html(contents)


def built(pages: list[PageInput] | None = None) -> snapshot.Built:
    """…and the same document in the form the database stores."""
    inputs = pages if pages is not None else load_pages()
    contents, _ = page_contents(inputs)
    document, report = pipeline.assemble_html(contents)
    return snapshot.build(
        pipeline.extraction_from_html(
            document, contents, raw=raw_payload(inputs), stats={"pipeline": report.as_dict()}
        )
    )


# --- §9.2 the y-first guard ---------------------------------------------------------------------


def test_box_2d_is_y_first_on_a_thousand_grid() -> None:
    assert gemini_box("100,200,300,400") == (0.2, 0.1, 0.4, 0.3)
    assert gemini_box("-10, 0, 1200, 500") == (0.0, 0.0, 0.5, 1.0)  # clamped
    assert gemini_box("300,400,100,200") == (0.2, 0.1, 0.4, 0.3)  # inside out
    assert gemini_box("1,2,3") is None and gemini_box("a,b,c,d") is None


def test_a_page_is_read_as_blocks_with_their_boxes() -> None:
    blocks, problems = parse_page(
        f"<h2 {box(80, 100, 130, 500)}>Titel</h2>"
        f"<ul><li {box(150, 120, 180, 900)}>Eins</li></ul>"
        f'<p data-furniture="page-number" {box(960, 480, 985, 520)}>7</p>'
        f"<div>nope</div>",
        3,
    )
    assert problems == [
        "<div> is not part of the vocabulary",
        "text outside any tag: 'nope'",
    ]
    assert [(b.tag, b.text, b.level, b.furniture) for b in blocks] == [
        ("h2", "Titel", 2, None),
        ("li", "Eins", None, None),
        ("p", "7", None, "page-number"),
    ]
    assert blocks[0].box == (0.1, 0.08, 0.5, 0.13)


def test_a_fenced_or_empty_answer_is_handled() -> None:
    blocks, _ = parse_page("```html\n<p>Text</p>\n```", 1)
    assert [b.text for b in blocks] == ["Text"]
    assert parse_page("", 1) == ([], [])


# --- §9.1 / §9.11 the golden fixture --------------------------------------------------------------


def test_golden_fixture_assembles_byte_exactly() -> None:
    """Regenerate with `OCR_GOLDEN_WRITE=1 pytest apps/documents/tests/test_ocr.py -k golden`
    after an intended change to the pipeline — and read the diff."""
    document, _ = build()
    snapshot_built = built()
    files = {
        "document.html": document,
        "content.html": snapshot_built.html,
        "content.txt": snapshot_built.text,
        "nodes.json": json.dumps(snapshot.payload(snapshot_built), ensure_ascii=False, indent=2)
        + "\n",
    }
    expected = GOLDEN / "expected"
    if os.environ.get("OCR_GOLDEN_WRITE"):
        expected.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (expected / name).write_text(text)
    for name, text in files.items():
        assert (expected / name).read_text() == text, name
    again, _ = build()
    assert again == document  # deterministic


def test_the_document_is_sound_and_keeps_every_invariant() -> None:
    document, report = build()
    assert pipeline.review(document) == []
    assert report.problems == []
    assert report.as_dict()["failed_pages"] == []
    snapshot_built = built()
    for node in snapshot_built.nodes:
        for region in node.regions:
            start, end = region.text_start or 0, region.text_end or 0
            assert node.text_start <= start <= end <= node.text_end
        assert snapshot_built.html[node.html_start : node.html_end].startswith(f"<{node.tag}")


def test_review_reports_what_is_wrong_with_a_document() -> None:
    assert pipeline.review("") == ["the document has no content"]
    assert "<p> does not say which page it is on" in pipeline.review("<p>lost</p>")
    unclosed = pipeline.review('<section><p data-pages="1">x</section>')
    assert any("left open" in problem for problem in unclosed)
    assert any("outside any tag" in problem for problem in pipeline.review("loose text"))
    outside = pipeline.review('<p data-pages="1" data-box="1;0,0,2,1;0,1">x</p>')
    assert any("outside the page" in problem for problem in outside)


# --- §9.3 / §9.4 cross-page merge -----------------------------------------------------------------


def test_a_cross_page_paragraph_is_one_tag_on_two_pages() -> None:
    document, report = build()
    assert report.merged == 2  # the paragraph and the table
    start = document.index("Die Patientin klagt")
    tag = document.rindex("<p", 0, start)
    element = document[tag : document.index("</p>", start) + 4]
    assert 'data-pages="1,2"' in element
    assert (
        'data-box="1;0.1000,0.2900,0.9000,0.3400;0,47|2;0.1000,0.0800,0.9000,0.1300;47,99"'
        in element
    )
    assert "zunehmende Beschwerden im rechten Knie, vor allem nachts." in element

    node = next(n for n in built().nodes if "zunehmende" in n.item.text)
    assert [region.page for region in node.regions] == [1, 2]
    # The builder has made the spans global; they still tile the node's own text exactly.
    spans = [(r.text_start or 0, r.text_end or 0) for r in node.regions]
    assert [(start - node.text_start, end - node.text_start) for start, end in spans] == [
        (0, 47),
        (47, 99),
    ]


def _join(
    pages: list[PageInput], *, merge_tables: bool = True
) -> tuple[list[pipeline.Item], pipeline.Report]:  # noqa: E501
    """Stages 2 and 3 over some pages, for the tests that are about joining alone."""
    contents, _ = page_contents(pages)
    report = pipeline.Report()
    items = pipeline.join(pipeline.read_pages(contents, report), report, merge_tables=merge_tables)
    return items, report


def test_the_hyphen_join_drops_a_line_break_hyphen_and_keeps_a_space_otherwise() -> None:
    items, report = _join(
        [
            page(1, "<p>Die Ost-</p>"),
            page(2, '<p data-continues="true">West-Achse ist frei.</p>'),
            page(3, '<p data-continues="true">Wirklich.</p>'),
        ]
    )
    (item,) = items
    # Documented v1 error: a compound split at a real hyphen loses it ("Ost-West-Achse").
    assert item.text == "Die OstWest-Achse ist frei. Wirklich."
    assert [(f.page, f.start, f.end) for f in item.fragments] == [
        (1, 0, 7),
        (2, 7, 27),
        (3, 28, 37),
    ]
    assert report.merged == 2

    apart, _ = _join([page(1, "<p>Ende.</p>"), page(2, '<h2 data-continues="true">Neu</h2>')])
    assert len(apart) == 2  # a heading never continues a paragraph


def test_a_strategy_that_cannot_tell_is_read_from_the_text() -> None:
    """No `data-continues` (a PDF has no such flag): a sentence that breaks mid-word joins,
    one that ended does not."""
    joined, _ = _join([page(1, "<p>Die Untersuchung war</p>"), page(2, "<p>unauffällig.</p>")])
    assert [item.text for item in joined] == ["Die Untersuchung war unauffällig."]

    separate, _ = _join(
        [
            page(1, "<p>Die Untersuchung war unauffällig.</p>"),
            page(2, "<p>Danach ging es weiter.</p>"),
        ]
    )  # noqa: E501
    assert len(separate) == 2


# --- §9.5 / §9.6 the outline ----------------------------------------------------------------------


def test_two_headings_on_a_page_are_siblings_and_a_bottom_heading_holds_the_next_page() -> None:
    document, _ = build()
    sections = [(n.path, n.level()) for n in built().nodes if n.tag == "section"]
    assert sections == [
        ("0001", 1),  # Arztbrief
        ("0001.0003", 2),  # Anamnese
        ("0001.0004", 2),  # Befund
        ("0001.0004.0004", 4),  # Nebenbefund: the deep jump nests under Befund
        ("0001.0005", 2),  # Verlauf
        ("0001.0006", 2),  # Beurteilung, a heading at the bottom of page 4
    ]
    assessment = next(n for n in built().nodes if n.path == "0001.0006")
    body = [c for c in assessment.children if c.tag == "p"]
    text = built().text
    assert [text[c.text_start : c.text_end][:30] for c in body] == [
        "Es besteht der Verdacht auf ei",
        "gez. Dr. Muster",
    ]
    assert sorted(assessment.pages) == [4, 5]
    assert assessment.title(text) == "Beurteilung"
    assert document.count("<section data-nid=") == 6


# --- §9.7 tables ----------------------------------------------------------------------------------


def test_a_table_across_pages_concatenates_rows_and_drops_the_repeated_header() -> None:
    document, _ = build()
    table = document[document.index("<table") : document.index("</table>") + 8]
    assert table.count("<th>Parameter</th>") == 1 and "<td>Hb</td>" in table
    assert 'data-pages="2,3"' in table

    node = next(n for n in built().nodes if n.tag == "table")
    text = built().text
    assert text[node.text_start : node.text_end] == (
        "Parameter\tWert\nCRP\t12 mg/l\nLeukozyten\t9,1 /nl\nHb\t13,4 g/dl"
    )
    assert [r.page for r in node.regions] == [2, 3]

    parsed = pipeline.Table.parse(
        '<table><tr><th>a</th><th colspan="2">b</th></tr>'
        "<tr><td>1</td><td>2</td><td>3<br>x</td></tr></table>"
    )
    assert parsed.header and parsed.inner_html() == (
        '<thead><tr><th>a</th><th colspan="2">b</th></tr></thead>'
        "<tbody><tr><td>1</td><td>2</td><td>3 x</td></tr></tbody>"
    )
    unjoined, _ = _join(load_pages(), merge_tables=False)
    assert sum(1 for item in unjoined if item.table is not None) == 2


# --- §9.8 furniture -------------------------------------------------------------------------------


def test_furniture_is_dropped_and_page_numbers_become_labels() -> None:
    document, report = build()
    assert "Klinik Musterstadt" not in document and "Vertraulich" not in document
    assert report.furniture == 11
    assert [(p.number, p.item.label) for p in built().pages] == [
        (1, "1"),
        (2, "2"),
        (3, "3"),
        (4, "4"),
        (5, "5"),
    ]
    figure = next(n for n in built().nodes if n.tag == "figure")
    assert [c.tag for c in figure.children] == ["figcaption"] and figure.regions


def test_empty_and_failed_pages_are_kept_as_pages_and_nothing_else() -> None:
    pages = [
        page(1, '<p data-box="80,100,130,900">Etwas</p>'),
        page(2, ""),  # a blank sheet
        PageInput(number=3, error="refused"),  # unreadable
        page(4, '<p data-box="80,100,130,900">Weiter</p>'),
        # A scan of an empty sheet: whatever read it found a speck and called it text.
        page(5, '<p data-box="80,100,130,900">:\'</p>'),
    ]
    contents, _ = page_contents(pages)
    document, report = pipeline.assemble_html(contents)
    assert report.empty_pages == [2, 5] and report.failed_pages == [3]
    assert report.noise == 1  # the speck on page 5, not stored as a paragraph
    assert 'data-pages="1"' in document and 'data-pages="4"' in document
    assert pipeline.review(document) == []
    extraction = pipeline.extraction_from_html(document, contents)
    assert [p.number for p in extraction.pages] == [1, 2, 3, 4, 5]  # every page keeps its row
    assert extraction.failed_pages == [3]
    # A page break between two unrelated pages does not join them.
    assert len(snapshot.build(extraction).nodes) == 2


# --- §9.9 / §9.10 the strategy and replay parity --------------------------------------------------


class StubReader:
    """Answers from the fixture; the pages named in `fail` refuse, the pages named in `broken`
    come back with markup outside the vocabulary."""

    def __init__(
        self,
        pages: list[PageInput],
        *,
        fail: frozenset[int] = frozenset(),
        broken: frozenset[int] = frozenset(),
    ) -> None:
        # `calls` records *that* a page was sent, not in which order: `run.read_document` reads
        # pages in parallel, so the threads append as they get there.
        self.pages = {page.number: page for page in pages}
        self.fail = fail
        self.broken = broken
        self.calls: list[int] = []
        self.dated: list[str] = []
        self.dates: dict[int, tuple[str, float]] = {}
        self.named: list[str] = []
        self.title = ""

    def read(self, png: bytes, number: int, total: int) -> str:
        self.calls.append(number)
        assert png.startswith(b"\x89PNG") and total >= number
        if number in self.fail:
            raise PageFailed("refused")
        html = self.pages[number].html or ""
        # A tag outside the vocabulary is the kind of slip a parser cannot paper over: the
        # text inside it is lost until someone fixes the markup.
        return (
            html.replace("<p ", "<paragraph ").replace("</p>", "</paragraph>")
            if number in self.broken
            else html
        )

    def date(self, html: str) -> dict[int, tuple[str, float]]:
        self.dated.append(html)
        return dict(self.dates)

    def name(self, html: str) -> str:
        self.named.append(html)
        return self.title


@pytest.mark.django_db
def test_the_strategy_writes_a_snapshot_and_a_failed_page_makes_it_partial(user: User) -> None:
    fixture = load_pages()[:2]
    reader = StubReader(fixture, fail=frozenset({2}))
    strategy = strategies.GeminiOcrStrategy(reader=reader)
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategy)
        assert content.status == ExtractionStatus.PARTIAL, content.error
        assert sorted(reader.calls) == [1, 2]
        assert content.stats["pipeline"]["failed_pages"] == [2]
        assert content.stats["failed_pages"] == [2]
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


class DyingReader(StubReader):
    """A reader whose connection drops on one page — the run fails as a whole, which is what
    a `ReadTimeout` half way through a long scan looks like."""

    def __init__(self, pages: list[PageInput], *, dies_on: int) -> None:
        super().__init__(pages)
        self.dies_on = dies_on

    def read(self, png: bytes, number: int, total: int) -> str:
        if number == self.dies_on:
            self.calls.append(number)
            raise TimeoutError("the read operation timed out")
        return super().read(png, number, total)


def draft_of(document: Document) -> DocumentContentDraft:
    return DocumentContentDraft.objects.get(document=document)


def plant(draft_id: uuid.UUID, state: str) -> None:
    """Put a state the current code cannot read into a draft row, past every check the ORM
    and pydantic would apply — what a row written by an older build looks like."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE documents_documentcontentdraft SET state = %s::jsonb WHERE id = %s",
            [state, str(draft_id)],
        )


@pytest.mark.django_db
def test_a_run_that_dies_keeps_its_state_and_the_next_one_reads_only_the_rest(user: User) -> None:
    """The point of the draft: "Read again" after a failure pays for the missing pages only."""
    fixture = load_pages()[:2]
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        dying = DyingReader(fixture, dies_on=2)
        failed = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=dying))
        assert failed.status == ExtractionStatus.FAILED
        assert "timed out" in failed.error and dying.calls == [1, 2]
        # Page 1 was already back when the connection dropped, so the state holds it — and
        # only it: a page the model refused is read again rather than remembered as failed.
        state = draft_of(document).state
        assert list(state.pages) == [1] and state.pages[1].html == fixture[0].html
        assert state.schema_version == ExtractionState.SCHEMA_VERSION
        assert api.resumable_pages(failed) == 1

        again = StubReader(fixture)
        content = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=again))
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert again.calls == [2]  # the page that was missing, and nothing else
        assert content.raw_output is not None
        stored = json.loads(content.raw_output.read_bytes())
        assert [p["number"] for p in stored["pages"]] == [1, 2]  # the resumed page replayed
        assert not any(p.get("failed") for p in stored["pages"])
        # The snapshot is written, so the state it was assembled from is discarded — retired
        # and emptied, since `raw_output` now holds the same reading.
        assert not DocumentContentDraft.objects.filter(document=document).exists()
        retired = DocumentContentDraft.all_objects.get(document=document)
        assert retired.deleted_at is not None and retired.state.pages == {}
        assert api.resumable_pages(content) == 0


@pytest.mark.django_db
def test_a_page_the_model_refused_is_read_again_by_the_next_run(user: User) -> None:
    """A failed page never enters the state: the next run is the chance to read it again."""
    fixture = load_pages()[:2]
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        reader = DyingReader(fixture, dies_on=2)
        reader.fail = frozenset({1})  # page 1 refused before page 2 dropped the connection
        run_failed = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=reader))
        assert run_failed.status == ExtractionStatus.FAILED
        assert draft_of(document).state.pages == {}


@pytest.mark.django_db
def test_a_stored_state_this_code_cannot_read_is_discarded_and_the_run_starts_fresh(
    user: User,
) -> None:
    """Scratch is never repaired or migrated: reading the document again is always correct."""
    fixture = load_pages()[:2]
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        dying = DyingReader(fixture, dies_on=2)
        snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=dying))
        draft = draft_of(document)
        # Written by an older build: a shape this code does not have. Planted in SQL, because
        # every way through the ORM would validate it on the way in — which is the point.
        plant(draft.pk, '{"schema_version": 1, "pages": {"1": {"number": 1, "colour": "blue"}}}')
        latest = document.latest_content()
        assert latest is not None
        assert snapshot.stored_state(latest) is None
        assert api.resumable_pages(latest) == 0

        again = StubReader(fixture)
        content = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=again))
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert again.calls == [1, 2]  # nothing was resumed: both pages were read again


@pytest.mark.django_db
def test_a_draft_that_cannot_be_stored_does_not_cost_the_run(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scratch is never worth a reading: if the state cannot be written, the run still runs."""
    fixture = load_pages()[:2]
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")

    def refuse(*args: object, **kwargs: object) -> None:
        raise ProgrammingError("permission denied for table documents_documentcontentdraft")

    # Exactly what a table the runtime role may not write looks like, at the one place the
    # run touches it — the row it would create, and every state it would store on it.
    monkeypatch.setattr(DocumentContentDraft, "save", refuse)
    with acting_as(user):
        reader = StubReader(fixture)
        content = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=reader))
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert reader.calls == [1, 2]


@pytest.mark.django_db
def test_a_state_from_an_older_schema_version_is_discarded_too(user: User) -> None:
    fixture = load_pages()[:2]
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        dying = strategies.GeminiOcrStrategy(reader=DyingReader(fixture, dies_on=2))
        snapshot.extract_now(document, dying)
        draft = draft_of(document)
        stale = draft.state.model_dump()
        stale["schema_version"] = ExtractionState.SCHEMA_VERSION - 1
        plant(draft.pk, json.dumps(stale))
        latest = document.latest_content()
        assert latest is not None
        assert snapshot.stored_state(latest) is None


@pytest.mark.django_db
def test_an_unsound_page_is_stored_with_what_is_wrong_with_it(user: User) -> None:
    """Nothing goes back to a model to be fixed: the page is kept for what it is worth and the
    snapshot says what the parser had against it."""
    reader = StubReader(load_pages()[:2], broken=frozenset({1}))
    document = upload(user, "scan.pdf", synthetic_pdf(2), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=reader))

        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert sorted(reader.calls) == [1, 2]  # once each, and no second look
        problems = content.stats["meta"]["ocr_problems"]
        assert any("not part of the vocabulary" in problem for problem in problems)
        assert snapshot.verify_snapshot(content) == []
        # The rest of the document is there; only the broken page's text is lost.
        assert "Sehr geehrte Frau Kollegin" not in content.text


@pytest.mark.django_db
def test_a_model_may_date_every_node_and_a_dateline_still_wins(user: User) -> None:
    """The extra step: one call over the assembled document, `INFERRED` for whatever it can
    place. A printed dateline is a statement by the document and outranks a reading of it."""
    reader = StubReader(load_pages())
    # nid 3 is the letter's opening paragraph, nid 6 the one that spans pages 1 and 2.
    reader.dates = {3: ("1943-05-12", 0.9), 6: ("1943-05", 0.4), 99: ("nonsense", 0.9)}
    document = upload(user, "scan.pdf", synthetic_pdf(5), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=reader))

        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert reader.dated, "the document was never offered for dating"
        assert '<section data-nid="1"' in reader.dated[0]  # it was given the numbered document
        opening = content.node(3)
        assert (opening.date_edtf, opening.date_source) == ("1943-05-12", DateSource.INFERRED)
        assert opening.date_conf == 0.9
        spanning = content.node(6)
        assert (spanning.date_edtf, spanning.date_source) == ("1943-05", DateSource.INFERRED)
        # A date the parser cannot hold is dropped, not stored.
        assert content.stats["dating"]["inferred"] == {"offered": 2, "used": 2}
        assert content.date is not None  # the document's own envelope covers both
        assert snapshot.verify_snapshot(content) == []


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
    assert (out / "document.html").exists()
    assert (out / "preview" / "0001.html").exists() and (out / "nodes.json").exists()


def test_the_command_needs_a_key_and_raw_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "GEMINI_API_KEY", None)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(text_pdf(1))
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
    assert [p.number for p in first] == [1, 2] and sorted(reader.calls) == [1, 2]
    assert (out / "pages" / "0002.png").exists() and (out / "raw" / "0002.json").exists()
    again = list(run.read_document(pdf, reader, existing=run.existing_raw(out)))
    # Nothing sent again: the call list is the two from the first run and no more.
    assert sorted(reader.calls) == [1, 2] and [p.number for p in again] == [1, 2]
    assert render.parse_page_range("1-2, 9", 2) == [1, 2] and render.page_count(pdf) == 2
    assert len(render.thumbnail_bytes(Image.new("RGB", (2000, 1000)), 600)) > 0


def test_a_page_is_read_once() -> None:
    """One request per page and no second look: no review round, no re-reading."""
    reader = StubReader(load_pages()[:2])
    pages = list(run.read_document(synthetic_pdf(2), reader))
    assert sorted(reader.calls) == [1, 2]
    assert [page.html is not None for page in pages] == [True, True]


def test_the_pages_are_read_in_parallel() -> None:
    """Each page waits for the other before answering: read one after the other, the first
    wait would time out. The barrier is what makes this a test rather than a hope."""
    barrier = threading.Barrier(2, timeout=10)

    class Meeting(StubReader):
        def read(self, png: bytes, number: int, total: int) -> str:
            barrier.wait()
            return super().read(png, number, total)

    pages = list(run.read_document(synthetic_pdf(2), Meeting(load_pages()[:2]), workers=2))
    assert [page.number for page in pages] == [1, 2]


def test_prompt_identity() -> None:
    assert len(gemini_client.prompt_sha256()) == 64
    config = strategies.GeminiOcrStrategy.config
    assert config["prompt_sha256"] == gemini_client.prompt_sha256()
    assert config["schema_version"] == page_html.SCHEMA_VERSION


# --- §9.12 live -----------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.filterwarnings("ignore:'_UnionGenericAlias' is deprecated:DeprecationWarning")
def test_one_synthetic_page_end_to_end_with_gemini(tmp_path: Path) -> None:
    if not env.GEMINI_API_KEY:
        pytest.skip("GEMINI_API_KEY is not set")
    reader = gemini_client.GeminiPageReader(env.GEMINI_API_KEY)
    (result,) = run.read_document(synthetic_pdf(1), reader)
    assert not result.failed, result.error
    document, _ = build([result])
    assert pipeline.review(document) == []
    assert "beschwerdefrei" in built([result]).text.lower()


@pytest.mark.django_db
def test_the_document_is_named_from_what_it_says(user: User) -> None:
    """The last step of the pipeline: the finished document is read once more and named. A
    file name says what a scanner called a file; this says what the document is."""
    reader = StubReader(load_pages())
    reader.title = "Arztbrief Orthopädie, 12. Mai 1943"
    document = upload(user, "2609_scan.pdf", synthetic_pdf(5), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=reader))

        assert reader.named, "the document was never offered for naming"
        assert content.title == "Arztbrief Orthopädie, 12. Mai 1943"
        # The scanner's file name is replaced, and the original is kept to tell renames apart.
        assert Document.objects.get(pk=document.pk).title == content.title
        assert document.meta["filename"] == "2609_scan.pdf"

        # A name a person typed is never overwritten by a later run.
        renamed = Document.objects.get(pk=document.pk)
        renamed.title = "Knie links"
        renamed.save(operation=None, sources=[])
        reader.title = "Etwas ganz anderes"
        snapshot.extract_now(document, strategies.GeminiOcrStrategy(reader=reader))
        assert Document.objects.get(pk=document.pk).title == "Knie links"


def test_a_document_without_a_model_is_named_by_its_first_heading() -> None:
    document, _ = build()
    assert pipeline.heading_title(document) == "Arztbrief"
    assert pipeline.heading_title('<p data-pages="1">no heading here</p>') == ""


# --- Standing matter ------------------------------------------------------------------------------


def test_standing_matter_is_kept_marked_all_the_way_into_the_artifact() -> None:
    pages = [
        page(
            1,
            f'<p data-aside="letterhead" {box(40, 100, 90, 900)}>Dr. med. Anna Beispiel</p>'
            f'<p data-aside="contact" {box(90, 100, 130, 900)}>Hauptstr. 1, 87435 Kempten</p>'
            f"<p {box(200, 100, 260, 900)}>Sehr geehrte Frau Kollegin,</p>"
            f'<p data-aside="bank" {box(900, 100, 950, 900)}>IBAN DE04 3006 0601 0042 9200</p>',
        )
    ]
    document, report = build(pages)
    assert report.aside == 3
    assert 'data-aside="letterhead"' in document and 'data-aside="bank"' in document
    assert "Sehr geehrte Frau Kollegin," in document

    artifact = built(pages)
    marked = [
        node.item.aside for node in artifact.nodes if node.item.tag == "p" and node.item.aside
    ]
    assert marked == ["letterhead", "contact", "bank"]
    assert artifact.html.count("data-aside=") == 3
    # It is text like any other: the reader is only shown it on request.
    assert "IBAN DE04 3006 0601 0042 9200" in artifact.text


def test_standing_matter_does_not_swallow_the_block_after_it() -> None:
    pages = [
        page(1, f'<p data-aside="contact" {box(900, 100, 950, 900)}>Tel. 0831 12345,</p>'),
        page(2, f"<p {box(80, 100, 130, 900)}>befundet am 05.08.2026</p>"),
    ]
    _, report = build(pages)
    assert report.merged == 0


def test_a_tag_without_a_box_still_says_which_page_it_is_on() -> None:
    """The model sometimes returns a tag with no `data-box`. The page is still known, and a
    region with no geometry is how the snapshot records that — it is not a failed run."""
    pages = [page(1, "<h2>Befund</h2><p>Reizlose Narbe.</p>")]
    artifact = built(pages)
    boxes = [(r.page, r.x0, r.y0, r.x1, r.y1) for node in artifact.nodes for r in node.regions]
    assert boxes == [(1, None, None, None, None), (1, None, None, None, None)]
    assert "Reizlose Narbe." in artifact.text


# --- Retrying a flaky API -------------------------------------------------------------------------


def _reader(waits: list[float]) -> gemini_client.GeminiPageReader:
    """A reader that never sleeps: the schedule is asserted, not waited out."""
    return gemini_client.GeminiPageReader("test-key", sleep=waits.append)


def _models(reader: gemini_client.GeminiPageReader) -> object:
    """The SDK object the reader sends its requests through — the seam these tests replace."""
    return reader._client.models  # noqa: SLF001


def test_a_rate_limited_page_is_read_again_on_the_stated_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.genai import errors

    waits: list[float] = []
    reader = _reader(waits)
    attempts = 0

    def flaky(**_: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise errors.APIError(429, {"error": {"message": "slow down"}})
        return SimpleNamespace(text='{"html": "<p>Befund</p>", "blank": false}')

    monkeypatch.setattr(_models(reader), "generate_content", flaky)
    assert reader.read(b"png", 1, 1) == "<p>Befund</p>"
    assert attempts == 4
    assert waits == list(gemini_client.RETRY_WAITS[:3])  # 1, 2, 2 — and no more


def test_a_request_the_model_refuses_is_not_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 says the request is wrong; nine identical ones would be wrong nine times."""
    from google.genai import errors

    waits: list[float] = []
    reader = _reader(waits)
    attempts = 0

    def refused(**_: object) -> object:
        nonlocal attempts
        attempts += 1
        raise errors.APIError(400, {"error": {"message": "the image could not be decoded"}})

    monkeypatch.setattr(_models(reader), "generate_content", refused)
    with pytest.raises(PageFailed, match="400"):
        reader.read(b"png", 1, 1)
    assert attempts == 1 and waits == []


def test_a_page_that_never_comes_back_fails_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.genai import errors

    waits: list[float] = []
    reader = _reader(waits)

    def down(**_: object) -> object:
        raise errors.APIError(503, {"error": {"message": "unavailable"}})

    monkeypatch.setattr(_models(reader), "generate_content", down)
    with pytest.raises(PageFailed, match="503"):
        reader.read(b"png", 1, 1)
    assert waits == list(gemini_client.RETRY_WAITS)  # every wait, then it gives up
    assert len(waits) + 1 == 9  # nine attempts


def test_a_step_that_cannot_run_leaves_the_reading_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best effort: the pages are already read, and a dating or a naming that will not run is
    not a reason to lose them — but it says so in the log rather than passing for an answer."""
    import httpx

    waits: list[float] = []
    reader = _reader(waits)

    def broken(**_: object) -> object:
        raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(_models(reader), "generate_content", broken)
    assert reader.name("<p>Befund</p>") == "" and reader.date("<p>Befund</p>") == {}
    assert waits == list(gemini_client.RETRY_WAITS) * 2  # a dropped connection is worth retrying
