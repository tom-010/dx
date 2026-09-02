"""The built-in strategies and the parsers behind them (`apps/documents/strategies.py`,
`apps/documents/extraction.py`), including the PDF y-flip (brief §9.10)."""

import math
from collections.abc import Sequence

import pytest

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import extraction, snapshot, strategies
from apps.documents.extraction import ExtractionError
from apps.documents.models import ExtractionStatus, StructureSource
from apps.documents.tests.conftest import upload

pytestmark = pytest.mark.django_db


def close(actual: Sequence[float] | None, expected: Sequence[float]) -> bool:
    """Elementwise float comparison (mypy's strict equality rejects `pytest.approx` on tuples)."""
    return actual is not None and all(
        math.isclose(a, b, abs_tol=1e-6) for a, b in zip(actual, expected, strict=True)
    )


def minimal_pdf(text: str = "Hello", x: float = 72, y: float = 700, size: float = 12) -> bytes:
    """A one-page Letter PDF with one text run at (x, y) in PDF user space (origin bottom-left)."""
    stream = f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


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


def test_a_born_digital_pdf_places_its_text_with_no_confidence(user: User) -> None:
    tree = strategies.PdfStrategy().parse(minimal_pdf(), "application/pdf")
    assert [(p.number, p.width, p.height) for p in tree.pages] == [(1, 612.0, 792.0)]
    (node,) = tree.nodes
    assert (node.tag, node.text) == ("p", "Hello")
    (region,) = node.regions
    assert region.page == 1 and region.span == (0, 5)
    assert close(region.envelope, (72 / 612, 82.4 / 792, 102 / 612, 94.4 / 792))
    assert region.words is not None and region.words[0].conf is None
    assert tree.raw is not None and b'"Hello"' in tree.raw

    document = upload(user, "hello.pdf", minimal_pdf(), "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document)
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert content.conf_stats is None  # born-digital: never a fake 1.0
        assert snapshot.verify_snapshot(content) == []
        # Drawn over a render at the page's own size, the box sits where the text is.
        (polygon,) = content.node(1).polygons(612, 792)
        assert polygon.page.number == 1
        assert close(polygon.points[0], (72, 82.4))
        assert close(polygon.points[2], (102, 94.4))
        assert document.hit(1, 80 / 612, 88 / 792) == content.node(1)
        assert content.raw_output is not None


def test_an_unreadable_pdf_is_an_extraction_error(user: User) -> None:
    with pytest.raises(ExtractionError, match="not a readable PDF"):
        strategies.PdfStrategy().parse(b"%PDF-1.4 garbage", "application/pdf")
    document = upload(user, "bad.pdf", b"%PDF-1.4 garbage", "application/pdf")
    with acting_as(user):
        content = snapshot.extract_now(document)
        assert content.status == ExtractionStatus.FAILED
        assert content.error.startswith("ExtractionError: not a readable PDF")


# --- registry ---


def test_the_registry_maps_mime_types_to_strategies() -> None:
    plain = strategies.STRATEGIES["plain-text"]
    assert strategies.strategy_for_mime("text/plain; charset=utf-8") is plain
    assert strategies.strategy_for_mime("application/PDF") is strategies.STRATEGIES["pypdf"]
    assert strategies.strategy_for_mime("image/png") is None
    assert strategies.strategy_named("html").tool_version == "1"
    with pytest.raises(strategies.UnknownStrategy):
        strategies.strategy_named("nope")
    assert str(strategies.STRATEGIES["pypdf"]).startswith("pypdf ")
