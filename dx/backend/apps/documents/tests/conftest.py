"""Fixtures for the documents tests: a temporary media root, an uploaded document, and the
fake extraction strategy the snapshot tests are built on (`FakeStrategy`)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import strategies
from apps.documents.api import store_documents
from apps.documents.extraction import (
    ExtractedNode,
    ExtractedPage,
    ExtractedRegion,
    ExtractedWord,
    Extraction,
)
from apps.documents.models import Document, StructureSource

HEADING = "Annual report 😀"
STRADDLING = "First paragraph spanning two pages"
TRAILING = 'Trailing & <special> chars "quoted"'
#: A diamond: a square rotated 45°, whose envelope is much bigger than its shape.
DIAMOND = [(0.5, 0.5), (0.7, 0.7), (0.5, 0.9), (0.3, 0.7)]


def fake_extraction() -> Extraction:
    """The fixture tree: a section with a heading, a paragraph straddling both pages, a list
    whose items overlap in envelope but not in shape, a table, a figure; then a loose paragraph
    with characters that need escaping. Word confidences on the heading and the paragraph."""
    heading = ExtractedNode(
        tag="h1",
        text=HEADING,
        level=1,
        source=StructureSource.DETECTED,
        regions=[
            ExtractedRegion(
                page=1,
                envelope=(0.1, 0.05, 0.9, 0.1),
                span=(0, len(HEADING)),
                words=[
                    ExtractedWord(0.1, 0.05, 0.4, 0.1, 0, 6, 0.99),
                    ExtractedWord(0.42, 0.05, 0.7, 0.1, 7, 13, 0.95),
                    ExtractedWord(0.72, 0.05, 0.9, 0.1, 14, 15, 0.5),
                ],
                detect_conf=0.97,
            )
        ],
    )
    split = STRADDLING.index("two")
    straddling = ExtractedNode(
        tag="p",
        text=STRADDLING,
        regions=[
            ExtractedRegion(
                page=1,
                envelope=(0.1, 0.2, 0.9, 0.5),
                span=(0, split),
                words=[
                    ExtractedWord(0.1, 0.2, 0.3, 0.3, 0, 5, 0.9),
                    ExtractedWord(0.32, 0.2, 0.6, 0.3, 6, 15, 0.8),
                    ExtractedWord(0.62, 0.2, 0.9, 0.3, 16, 24, 0.7),
                ],
            ),
            ExtractedRegion(
                page=2,
                envelope=(0.1, 0.05, 0.9, 0.2),
                span=(split, len(STRADDLING)),
                words=[
                    ExtractedWord(0.1, 0.05, 0.3, 0.2, split, split + 3, 0.6),
                    ExtractedWord(0.32, 0.05, 0.6, 0.2, split + 4, len(STRADDLING), 0.4),
                ],
            ),
        ],
    )
    items = ExtractedNode(
        tag="ul",
        regions=[ExtractedRegion(page=2, envelope=(0.1, 0.3, 0.9, 0.95))],
        children=[
            ExtractedNode(
                tag="li",
                text="Alpha",
                regions=[ExtractedRegion(page=2, ring=DIAMOND, span=(0, 5))],
            ),
            ExtractedNode(
                tag="li",
                text="Beta",
                # Overlaps the diamond's envelope (its corner) but not its shape.
                regions=[ExtractedRegion(page=2, envelope=(0.3, 0.5, 0.42, 0.62), span=(0, 4))],
            ),
        ],
    )
    table = ExtractedNode(
        tag="table",
        rows=[["Item", "Amount"], ["Rent", "1200"], ["Food & drink", "300"]],
        header=True,
        regions=[ExtractedRegion(page=2, envelope=(0.1, 0.96, 0.5, 0.99))],
    )
    figure = ExtractedNode(
        tag="figure",
        regions=[ExtractedRegion(page=2, envelope=(0.6, 0.96, 0.9, 0.99), detect_conf=0.8)],
        children=[ExtractedNode(tag="figcaption", text="Figure 1")],
    )
    section = ExtractedNode(tag="section", children=[heading, straddling, items, table, figure])
    trailing = ExtractedNode(tag="p", text=TRAILING)
    return Extraction(
        nodes=[section, trailing],
        pages=[
            ExtractedPage(number=1, width=612.0, height=792.0),
            ExtractedPage(number=2, width=612.0, height=792.0),
        ],
        raw=b'{"fake": true}',
        meta={"title": "Annual report"},
    )


class FakeStrategy(strategies.TreeStrategy):
    """Ignores the bytes and returns the fixture tree."""

    name = "fake"
    tool_version = "1"

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        return fake_extraction()


def text_pdf(
    pages: int = 1, text: str = "Hello", x: float = 72, y: float = 700, size: float = 12
) -> bytes:
    """A Letter-sized PDF with one text run per page at (x, y) in PDF user space (origin
    bottom-left) — born-digital, so `pypdf` reads it and pdfium renders it."""
    # The text is the same on every page: a caller that asserts on geometry (or on the text
    # of "the" page) gets one predictable run wherever it looks.
    streams = [f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET".encode()] * pages
    kids = " ".join(f"{3 + index} 0 R" for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
    ]
    objects += [
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {3 + pages + index} 0 R "
            f"/Resources << /Font << /F1 {3 + 2 * pages} 0 R >> >> >>"
        ).encode()
        for index in range(pages)
    ]
    objects += [
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        for stream in streams
    ]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
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


def synthetic_pdf(pages: int = 2) -> bytes:
    """A small scan-like PDF: pages with a printed title and a paragraph, drawn with Pillow —
    a real PDF for the strategies and the page-image endpoint, and nobody's medical record."""
    from io import BytesIO  # noqa: PLC0415 - only this helper needs them

    from PIL import Image, ImageDraw  # noqa: PLC0415

    images = []
    for number in range(1, pages + 1):
        image = Image.new("RGB", (595, 842), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 60), f"Seite {number}: Befund", fill="black")
        draw.text((60, 120), "Die Patientin ist beschwerdefrei.", fill="black")
        images.append(image)
    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def fake() -> Iterator[FakeStrategy]:
    """The fake strategy, registered for `application/x-fake` for the test's duration."""
    strategy = FakeStrategy()
    strategies.register(strategy, "application/x-fake")
    yield strategy
    del strategies.STRATEGIES[strategy.name]
    del strategies.MIME_STRATEGIES["application/x-fake"]


def upload(
    user: User,
    name: str = "report.fake",
    content: bytes = b"fake bytes",
    content_type: str = "application/x-fake",
) -> Document:
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile(name, content, content_type=content_type)]
        )
    return document


@pytest.fixture
def document(user: User, fake: FakeStrategy) -> Document:
    return upload(user)
