"""The QA loop: one HTML page per PDF page with the regions drawn over the page image —
assembly bugs are visible in seconds (`manage.py ocr assemble` writes `out/preview/`)."""

from __future__ import annotations

from html import escape
from pathlib import Path

from apps.documents.ocr.run import PAGE_NAME
from apps.documents.snapshot import Built

_STYLE = """
body{margin:1rem;font:14px system-ui,sans-serif;background:#222;color:#ddd}
.page{position:relative;display:inline-block;max-width:100%}
.page img{display:block;max-width:100%;height:auto}
.r{position:absolute;box-sizing:border-box;border:1px solid rgba(255,80,80,.8);
   background:rgba(255,80,80,.15)}
.r:hover{background:rgba(80,160,255,.35);border-color:#4af}
nav a{color:#9cf;margin-right:1rem}
"""


def _link(number: int, label: str) -> str:
    return f'<a href="{PAGE_NAME.format(number)}.html">{label}</a>'


def write_previews(out: Path, built: Built) -> list[Path]:
    """`out/preview/NNNN.html` for every page of the build, referencing `../pages/NNNN.png`."""
    folder = out / "preview"
    folder.mkdir(parents=True, exist_ok=True)
    numbers = [page.number for page in built.pages]
    written = []
    for index, number in enumerate(numbers):
        boxes = []
        for node in built.nodes:
            for region in node.regions:
                if region.page != number:
                    continue
                if region.x0 is None or region.y0 is None:
                    continue  # the page is known, the place on it is not: nothing to draw
                x1 = region.x1 if region.x1 is not None else region.x0
                y1 = region.y1 if region.y1 is not None else region.y0
                text = ""
                if region.text_start is not None and region.text_end is not None:
                    text = built.text[region.text_start : region.text_end]
                title = escape(f"<{node.tag}> #{node.nid}\n{text[:400]}", quote=True)
                boxes.append(
                    f'<div class="r" title="{title}" style="left:{region.x0 * 100:.2f}%;'
                    f"top:{region.y0 * 100:.2f}%;width:{(x1 - region.x0) * 100:.2f}%;"
                    f'height:{(y1 - region.y0) * 100:.2f}%"></div>'
                )
        previous = _link(numbers[index - 1], "previous") if index else ""
        following = _link(numbers[index + 1], "next") if index + 1 < len(numbers) else ""
        name = PAGE_NAME.format(number)
        page_html = (
            f"<!doctype html><meta charset=utf-8><title>page {number}</title>"
            f"<style>{_STYLE}</style>"
            f"<nav>{previous} page {number} of {len(numbers)} {following}</nav>"
            f'<div class="page"><img src="../pages/{name}.png" alt="page {number}">'
            + "".join(boxes)
            + "</div>"
        )
        path = folder / f"{name}.html"
        path.write_text(page_html)
        written.append(path)
    return written
