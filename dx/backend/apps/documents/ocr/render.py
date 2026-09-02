"""Page rasterization with pdfium — one page in memory at a time.

Ported from `manage.py playground render`: 150 dpi reads well on screen and is what a vision
model gets anyway (they downscale past ~200); the long edge is capped at `MAX_PX` so an A0 plan
stays bounded; thumbnails are scaled down from the page image, never enlarged.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

DPI = 150
#: Long-edge cap in pixels: an A0 plan at 300 dpi would be 140 megapixels otherwise.
MAX_PX = 5000
#: Thumbnail long edges: a preview card (300 CSS px at 2x) and a table row (48 CSS px at 2x).
THUMB_PX = 600
ROW_PX = 96


@dataclass
class RenderedPage:
    number: int
    image: Image.Image
    #: The page size in points (1/72 in), for `Page.width`/`Page.height`.
    width: float
    height: float


def render_page(page: pdfium.PdfPage, scale: float) -> Image.Image:
    """One page as a PIL image; the pdfium bitmap is released before returning."""
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil()
    finally:
        bitmap.close()


def page_count(pdf: bytes | Path) -> int:
    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def page_sizes(pdf: bytes | Path) -> list[tuple[float, float]]:
    """(width, height) in points of every page, without rendering."""
    document = pdfium.PdfDocument(pdf)
    try:
        sizes = []
        for index in range(len(document)):
            page = document[index]
            try:
                sizes.append(page.get_size())
            finally:
                page.close()
        return sizes
    finally:
        document.close()


def render_pages(
    pdf: bytes | Path, *, dpi: int = DPI, pages: Iterable[int] | None = None
) -> Iterator[RenderedPage]:
    """Yield the requested pages (1-based; all when None), rendered at `dpi` (capped), one at
    a time — the caller must finish with a page before asking for the next."""
    wanted = set(pages) if pages is not None else None
    document = pdfium.PdfDocument(pdf)
    try:
        for index in range(len(document)):
            number = index + 1
            if wanted is not None and number not in wanted:
                continue
            page = document[index]
            try:
                width, height = page.get_size()
                scale = min(dpi / 72, MAX_PX / max(width, height, 1.0))
                image = render_page(page, scale)
            finally:
                page.close()
            yield RenderedPage(number=number, image=image, width=width, height=height)
    finally:
        document.close()


def png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def thumbnail_png(image: Image.Image, long_edge: int = THUMB_PX) -> bytes:
    """A PNG no larger than `long_edge` on its long side; aspect ratio kept, never enlarged."""
    copy = image.copy()
    copy.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    return png_bytes(copy)


def parse_page_range(text: str, total: int) -> list[int]:
    """`"1-5,8"` → `[1, 2, 3, 4, 5, 8]`, clipped to the document; empty = every page."""
    if not text.strip():
        return list(range(1, total + 1))
    numbers: set[int] = set()
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            start, end = piece.split("-", 1)
            numbers.update(range(int(start), int(end) + 1))
        else:
            numbers.add(int(piece))
    return sorted(n for n in numbers if 1 <= n <= total)
