"""Page rasterization with pdfium — one page in memory at a time, one thread at a time.

Ported from `manage.py playground render`: 150 dpi reads well on screen and is what a vision
model gets anyway (they downscale past ~200); the long edge is capped at `MAX_PX` so an A0 plan
stays bounded; thumbnails are scaled down from the page image, never enlarged.

**pdfium is not thread-safe.** The library keeps process-global state, so two threads rendering
at once corrupt its heap and take the process down with it — measured: eight threads rendering
one file crash within seconds, every time, while the same volume in one thread never does.
That is what a threaded server does the moment someone opens a document with a page strip.

Every call therefore goes through `PDFIUM_LOCK`, and the functions here are the only place
that touches the library.

What that costs is small, and it was measured rather than assumed. Of the ~115 ms a page used
to take, only ~32 ms is pdfium; the rest was PNG encoding. Encoding is now JPEG (5.9 ms and a
third of the bytes for a scanned page), so a page is ~38 ms and the serialized part is 32 ms of
it. Rendering the same page in a process pool was tried and measured *slower* than the lock —
the several megabytes of PDF in and image out cost more than the parallelism won — so the pool
was removed rather than kept for the look of it. If page images ever become hot, store them
instead of rendering them: they never change for a (document, page, size).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

#: Serializes every call into pdfium *within* a process (see the module docstring). Held for
#: one page's render and never across a network call or a database query.
PDFIUM_LOCK = threading.Lock()

#: What a page is rasterized at for the model. 200 rather than 150 because handwriting is
#: what costs the most resolution: a doctor's marginal note is small, thin and already twice
#: photocopied, and the reading is only as good as the pixels it is made from.
DPI = 200
#: Long-edge cap in pixels: an A0 plan at 300 dpi would be 140 megapixels otherwise.
MAX_PX = 5000
#: Thumbnail long edges: a preview card (300 CSS px at 2x) and a table row (48 CSS px at 2x).
THUMB_PX = 600
ROW_PX = 96
#: What to rasterize each of those at. An A4 page is 11.7 inches tall, so these land just above
#: the long edge above and the downscale only takes off the remainder — rendering a row picture
#: at reading dpi and then shrinking it by twelve is the same picture for a hundred times the
#: pixels. A page larger than A4 comes out below the cap; nothing is ever enlarged.
THUMB_DPI = 56
ROW_DPI = 12
#: What a page image is served as, and how good it looks.
IMAGE_MIME = "image/jpeg"
JPEG_QUALITY = 80


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
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(pdf)
        try:
            return len(document)
        finally:
            document.close()


def page_sizes(pdf: bytes | Path) -> list[tuple[float, float]]:
    """(width, height) in points of every page, without rendering."""
    with PDFIUM_LOCK:
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
    # The lock is taken per page rather than for the whole loop: the caller does slow things
    # between pages (a Gemini request, a PNG to disk) and must not hold pdfium meanwhile.
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(pdf)
        count = len(document)
    try:
        for index in range(count):
            number = index + 1
            if wanted is not None and number not in wanted:
                continue
            with PDFIUM_LOCK:
                page = document[index]
                try:
                    width, height = page.get_size()
                    scale = min(dpi / 72, MAX_PX / max(width, height, 1.0))
                    image = render_page(page, scale)
                finally:
                    page.close()
            yield RenderedPage(number=number, image=image, width=width, height=height)
    finally:
        with PDFIUM_LOCK:
            document.close()


def png_bytes(image: Image.Image) -> bytes:
    """Lossless, for what an extractor reads: a model should see the page, not the artefacts."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(image: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    """For what a person looks at: a scanned page is a photograph, and JPEG encodes one in a
    tenth of the time and a third of the bytes."""
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def thumbnail_bytes(image: Image.Image, long_edge: int = THUMB_PX) -> bytes:
    """A JPEG no larger than `long_edge` on its long side; ratio kept, never enlarged."""
    copy = image.copy()
    copy.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    return jpeg_bytes(copy)


def render_one(pdf: bytes | Path, number: int, *, dpi: int = DPI) -> RenderedPage:
    """One page, with the document opened and closed around it — what a worker thread renders.

    `render_pages` is the sequential walk; this is the one page a thread wants without holding
    a document handle other threads would have to wait on. Raises `IndexError` for a page the
    document does not have.
    """
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(pdf)
        try:
            if not 1 <= number <= len(document):
                raise IndexError(f"page {number} is not in a {len(document)}-page document")
            page = document[number - 1]
            try:
                width, height = page.get_size()
                scale = min(dpi / 72, MAX_PX / max(width, height, 1.0))
                image = render_page(page, scale)
            finally:
                page.close()
        finally:
            document.close()
    return RenderedPage(number=number, image=image, width=width, height=height)


def render_image(pdf: bytes, number: int, *, dpi: int = DPI, long_edge: int | None = None) -> bytes:
    """One page as a JPEG — what the workspace draws its region overlay on."""
    image = render_one(pdf, number, dpi=dpi).image
    return jpeg_bytes(image) if long_edge is None else thumbnail_bytes(image, long_edge)


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
