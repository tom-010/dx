"""The built-in strategies and the parsers behind them (`apps/documents/strategies.py`,
`apps/documents/extraction.py`), including the PDF y-flip (brief §9.10)."""

import math
from collections.abc import Sequence

import pytest

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import extraction, pipeline, snapshot, strategies
from apps.documents.extraction import Extraction, ExtractionError
from apps.documents.models import ExtractionStatus, StructureSource
from apps.documents.ocr import render
from apps.documents.tests.conftest import text_pdf, upload

pytestmark = pytest.mark.django_db


def close(actual: Sequence[float] | None, expected: Sequence[float], *, scale: float = 1.0) -> bool:
    """Elementwise float comparison (mypy's strict equality rejects `pytest.approx` on tuples).

    The tolerance is the pipeline's own: a box travels through the augmented HTML with
    `pipeline.BOX_DECIMALS` decimals, which on a 600 pt page is a twentieth of a point.
    """
    return actual is not None and all(
        math.isclose(a, b, abs_tol=scale * 10**-pipeline.BOX_DECIMALS)
        for a, b in zip(actual, expected, strict=True)
    )


# --- plain text ---


def test_plain_text_splits_paragraphs_on_blank_lines() -> None:
    tree = strategies.PlainTextStrategy().parse(b"One\r\nline\r\n\r\n  Two  \n\n\n", "text/plain")
    assert [(n.tag, n.text, n.source) for n in tree.nodes] == [
        ("p", "One\nline", StructureSource.EMBEDDED),
        ("p", "Two", StructureSource.EMBEDDED),
    ]
    assert tree.pages == [] and tree.failed_pages == []
    assert extraction.decode(b"caf\xe9") == "café"  # not UTF-8: Latin-1, never an error
    assert extraction.decode("﻿bom".encode()) == "bom"


# --- html ---

SAMPLE_HTML = b"""<!doctype html><html><head><title> My  Doc </title><style>p{}</style></head>
<body><header><h1>Title</h1></header><div>Loose <b>text</b> here</div>
<p>Para <span>one</span></p><p>Para two
<ul><li>A</li><li>B<ul><li>B1</li></ul></li></ul>
<table><tr><th>H1</th><th>H2</th></tr><tr><td>1<br>x</td><td>2</td></tr></table>
<blockquote>Quoted</blockquote><pre>
  code
  more</pre><figure><figcaption>Cap</figcaption></figure><script>alert()</script>
<p></p></body></html>"""


def test_html_keeps_the_sources_structure() -> None:
    tree = strategies.HtmlStrategy().parse(SAMPLE_HTML, "text/html")
    assert tree.meta == {"title": "My Doc"}

    def shape(node: extraction.ExtractedNode) -> object:
        if node.rows is not None:
            return (node.tag, node.rows, node.header)
        if node.children:
            return (node.tag, [shape(c) for c in node.children])
        return (node.tag, node.text)

    assert [shape(n) for n in tree.nodes] == [
        ("h1", "Title"),
        ("p", "Loose text here"),
        ("p", "Para one"),
        ("p", "Para two"),  # closed by the list, as a browser would
        ("ul", [("li", "A"), ("li", "B\nB1")]),
        ("table", [["H1", "H2"], ["1 x", "2"]], True),
        ("blockquote", [("p", "Quoted")]),
        ("pre", "  code\n  more"),
        ("figure", [("figcaption", "Cap")]),
    ]
    assert tree.nodes[0].level == 1
    assert all(n.source == StructureSource.EMBEDDED for n in tree.nodes)


def test_an_html_source_becomes_a_snapshot_without_pages(user: User) -> None:
    document = upload(user, "doc.html", SAMPLE_HTML, "text/html")
    with acting_as(user):
        content = snapshot.extract_now(document)
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert snapshot.verify_snapshot(content) == []
        assert content.pages.count() == 0
        assert content.text.startswith("Title\n\nLoose text here\n\nPara one")
        assert '<pre data-nid="11">  code\n  more</pre>' in content.html
        assert "<script" not in content.html and "alert" not in content.text
        assert document.heading_title() == "Title"
        assert document.meta == {"title": "My Doc"}
        assert content.node(8).text() == "H1\tH2\n1 x\t2"


# --- pdf ---


def test_pdf_boxes_flip_the_y_axis_once() -> None:
    # PDF: origin bottom-left, y up. A 12 pt run whose baseline sits at y=700 on a 792 pt page
    # rises to 709.6 and drops to 697.6 — which is 82.4..94.4 pt from the *top*.
    box = extraction.pdf_box(72, 700, 30, 12, 612, 792)
    assert close(box, (72 / 612, 82.4 / 792, 102 / 612, 94.4 / 792))
    assert close(extraction.pdf_box(-5, 800, 9999, 12, 612, 792), (0.0, 0.0, 1.0, 0.0))
    with pytest.raises(ExtractionError):
        extraction.pdf_box(0, 0, 1, 1, 0, 792)


def _pdf_tree(data: bytes) -> Extraction:
    """What `PdfStrategy` reads, without a document: pages of HTML through the pipeline."""
    strategy = strategies.PdfStrategy()
    read = strategy._pages(strategy._read_pdf(data))  # noqa: SLF001 - the strategy's own stages
    extraction, _html = pipeline.assemble(read.pages, meta=read.meta, raw=read.raw)
    return extraction


def test_a_born_digital_pdf_places_its_text_with_no_confidence(user: User) -> None:
    tree = _pdf_tree(text_pdf())
    assert [(p.number, p.width, p.height) for p in tree.pages] == [(1, 612.0, 792.0)]
    (node,) = tree.nodes
    assert (node.tag, node.text) == ("p", "Hello")
    (region,) = node.regions
    assert region.page == 1 and region.span == (0, 5)
    assert close(region.envelope, (72 / 612, 82.4 / 792, 102 / 612, 94.4 / 792))
    # Word boxes do not survive the augmented HTML — a block's box does, and that is what the
    # region overlay needs. `words` stays schema-ready for an engine that reports per word.
    assert region.words is None
    assert tree.raw is not None and b'"Hello"' in tree.raw

    document = upload(user, "hello.pdf", text_pdf(), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategies.PdfStrategy())
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert content.conf_stats is None  # born-digital: never a fake 1.0
        assert snapshot.verify_snapshot(content) == []
        # Drawn over a render at the page's own size, the box sits where the text is.
        (polygon,) = content.node(1).polygons(612, 792)
        assert polygon.page.number == 1
        assert close(polygon.points[0], (72, 82.4), scale=792)  # points, not fractions
        assert close(polygon.points[2], (102, 94.4), scale=792)
        assert document.hit(1, 80 / 612, 88 / 792) == content.node(1)
        assert content.raw_output is not None


def test_an_unreadable_pdf_is_an_extraction_error(user: User) -> None:
    with pytest.raises(ExtractionError, match="not a readable PDF"):
        _pdf_tree(b"%PDF-1.4 garbage")
    document = upload(user, "bad.pdf", b"%PDF-1.4 garbage", "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document, strategies.PdfStrategy())
        assert content.status == ExtractionStatus.FAILED
        assert content.error.startswith("ExtractionError: not a readable PDF")


# --- registry ---


def test_the_registry_maps_mime_types_to_strategies() -> None:
    plain = strategies.STRATEGIES["plain-text"]
    assert strategies.strategy_for_mime("text/plain; charset=utf-8") is plain
    # A PDF is a scan until proven otherwise, and a scan's own text layer cannot be trusted.
    assert strategies.strategy_for_mime("application/PDF") is strategies.STRATEGIES["gemini-ocr"]
    assert strategies.strategy_named("pypdf").name == "pypdf"  # still there, opt-in
    assert strategies.strategy_for_mime("image/png") is None
    assert strategies.strategy_named("html").tool_version == "1"
    with pytest.raises(strategies.UnknownStrategy):
        strategies.strategy_named("nope")
    assert str(strategies.STRATEGIES["pypdf"]).startswith("pypdf ")


def test_rendering_is_serialized_across_threads() -> None:
    """pdfium keeps process-global state: two threads rendering at once corrupt its heap and
    take the process down — which a threaded server does the moment two people open a
    document. Every call goes through one lock (`render.PDFIUM_LOCK`)."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415 - only this test

    pdf = text_pdf(3)

    def render_all(_: int) -> list[int]:
        return [page.number for page in render.render_pages(pdf, dpi=72)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(render_all, range(16)))

    assert results == [[1, 2, 3]] * 16
    assert not render.PDFIUM_LOCK.locked()


def test_a_page_image_is_a_jpeg_and_costs_what_it_should() -> None:
    """The serving format is measured, not assumed: on a scanned page PNG took ~67 ms and
    789 kB, JPEG ~6 ms and 254 kB, and the pdfium render itself is only ~32 ms of either."""
    pdf = text_pdf(2)

    full = render.render_image(pdf, 1)
    thumb = render.render_image(pdf, 1, long_edge=render.THUMB_PX)

    assert full.startswith(b"\xff\xd8\xff") and thumb.startswith(b"\xff\xd8\xff")
    assert len(thumb) < len(full)
    with pytest.raises(IndexError):
        render.render_image(pdf, 9)


def test_a_line_is_as_wide_as_its_text_not_as_its_widest_run() -> None:
    """A PDF reports the last positioning operator for every run of a line, so the runs all
    claim the same x. Measuring each one from its own x left a line as narrow as its longest
    run: on a real letter the right edge came out ~5% of the page short, now ~1%."""
    runs = [
        extraction.PdfChunk(text="Antonie", x=150.0, y=700.0, font_size=10.0),
        extraction.PdfChunk(text=" Hartmann", x=150.0, y=700.0, font_size=10.0),
    ]

    (line,) = extraction.pdf_lines(runs)

    assert line.text == "Antonie Hartmann"
    assert line.x0 == 150.0
    # The whole line, not the longer of the two runs (which would end at 150 + 9 chars).
    assert line.x1 == pytest.approx(150.0 + extraction.ADVANCE * 10.0 * len(line.text))
    assert line.x1 > 150.0 + extraction.ADVANCE * 10.0 * len(" Hartmann".strip())
