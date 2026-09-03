"""The pipeline around the map step: render → read → `PageInput`s, page by page.

Every page is read on its own — nothing about page 7 depends on page 6 — so the pages go out
**in parallel**, `WORKERS` of them in flight, which is what makes a twelve-page scan cost about
what one page costs. pdfium is not thread-safe and is serialized by its own lock
(`render.PDFIUM_LOCK`), so each worker renders its page and then spends its time waiting on the
network, which is where the time actually goes.

One request per page and no second look: a page that fails is a failed page, and the run keeps
the rest. Pages are yielded in page order, and `on_page` runs on the calling thread, so writing
files and storing rows stays single-threaded and needs no locking of its own.

Shared by `manage.py ocr` (files under an output directory) and `GeminiOcrStrategy` (blobs).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
from pathlib import Path

import structlog

from apps.documents.ocr.assembly import PageInput
from apps.documents.ocr.gemini_client import PageFailed, PageReader
from apps.documents.ocr.render import (
    DPI,
    THUMB_PX,
    page_count,
    png_bytes,
    render_one,
    thumbnail_bytes,
)

log = structlog.get_logger(__name__)

PAGE_NAME = "{:04d}"
#: Pages in flight at once. They are network-bound; the ceiling is the API's rate limit and the
#: memory of that many rendered pages, not the CPU.
WORKERS = 8


def read_page(reader: PageReader, png: bytes, number: int, total: int) -> PageInput:
    """One page, read once. A page that cannot be read carries its error instead of HTML."""
    started = time.monotonic()
    try:
        html = reader.read(png, number, total)
    except PageFailed as exc:
        log.warning("ocr_page_failed", page=number, error=str(exc)[:300])
        return PageInput(number=number, error=str(exc)[:2000])
    log.info("ocr_page_read", page=number, of=total, chars=len(html), s=_since(started))
    return PageInput(number=number, html=html)


def _since(started: float) -> float:
    return round(time.monotonic() - started, 1)


def read_document(
    pdf: bytes | Path,
    reader: PageReader,
    *,
    dpi: int = DPI,
    pages: Iterable[int] | None = None,
    existing: dict[int, PageInput] | None = None,
    thumbnails: bool = False,
    on_page: Callable[[PageInput, bytes | None], None] | None = None,
    workers: int = WORKERS,
) -> Iterator[PageInput]:
    """Yield one `PageInput` per requested page, in order, `workers` of them read at once.

    A page that is already in `existing` is not sent again (resume) — it is re-rendered,
    because its size and thumbnail come from the file rather than from the extractor.
    `on_page` sees each result with its PNG, on this thread, in page order.
    """
    total = page_count(pdf)
    wanted = sorted(pages) if pages is not None else list(range(1, total + 1))
    done = existing or {}
    started = time.monotonic()
    log.info(
        "ocr_started", pages=len(wanted), of=total, workers=workers, dpi=dpi, resumed=len(done)
    )

    def one(number: int) -> tuple[PageInput, bytes]:
        rendered = render_one(pdf, number, dpi=dpi)
        png = png_bytes(rendered.image)  # what the model sees: lossless
        result = done.get(number) or read_page(reader, png, number, total)
        result.width, result.height = rendered.width, rendered.height
        if thumbnails:
            result.thumbnail = thumbnail_bytes(rendered.image, THUMB_PX)
        return result, png

    read = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # One batch in flight at a time: a batch's images are alive until it is consumed, and
        # a hundred-page scan should not hold a hundred of them to save a few seconds.
        for batch in batched(wanted, workers, strict=False):  # the last batch is short
            for result, png in pool.map(one, batch):
                if on_page is not None:
                    on_page(result, png)
                read += 1
                failed += int(result.failed)
                log.info("ocr_page_done", page=result.number, done=read, of=len(wanted))
                yield result
    log.info("ocr_finished", pages=read, failed=failed, s=_since(started))


# --- Files under an output directory (`manage.py ocr`) --------------------------------------------


def raw_dir(out: Path) -> Path:
    return out / "raw"


def pages_dir(out: Path) -> Path:
    return out / "pages"


def existing_raw(out: Path) -> dict[int, PageInput]:
    """The pages already under `out/raw/` — what `read_document(existing=…)` skips."""
    found: dict[int, PageInput] = {}
    for path in sorted(raw_dir(out).glob("*.json")) if raw_dir(out).is_dir() else []:
        record = json.loads(path.read_text())
        found[int(record["number"])] = PageInput.from_raw(record)
    return found


def write_raw(out: Path, page: PageInput, png: bytes | None) -> None:
    raw_dir(out).mkdir(parents=True, exist_ok=True)
    pages_dir(out).mkdir(parents=True, exist_ok=True)
    name = PAGE_NAME.format(page.number)
    (raw_dir(out) / f"{name}.json").write_text(
        json.dumps(page.to_raw(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if png is not None:
        (pages_dir(out) / f"{name}.png").write_bytes(png)
