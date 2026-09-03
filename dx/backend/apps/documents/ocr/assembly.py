"""The OCR strategy's stage 1: one page's answer from the model, as a page of the pipeline.

`PageInput` is what a run stores per page — the model's own HTML (or its failure), plus the
page's size — and `raw_payload` is exactly the bytes `manage.py ocr` writes and a snapshot
keeps in `raw_output`, so a production run replays bit for bit. What is specific to Gemini
lives here and in `page_html.py`: the box format and the furniture names. From
`page_contents()` on, a scan and a born-digital PDF are the same thing to the pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.documents.ocr.page_html import as_page_html
from apps.documents.pipeline import PageHtml


@dataclass
class PageInput:
    """One page of a run: `{"number", "width", "height", "html"}`, or `{"number", "failed",
    "error"}` — what `out/raw/NNNN.json` and the `raw_output` blob hold, plus (on the strategy
    path) a thumbnail PNG."""

    number: int
    width: float | None = None
    height: float | None = None
    html: str | None = None
    error: str | None = None
    thumbnail: bytes | None = None

    @property
    def failed(self) -> bool:
        return self.html is None

    @classmethod
    def from_raw(cls, record: dict[str, Any]) -> PageInput:
        number = int(record["number"])
        if record.get("failed"):
            return cls(number=number, error=str(record.get("error") or "failed"))
        return cls(
            number=number,
            width=float(record["width"]) if record.get("width") is not None else None,
            height=float(record["height"]) if record.get("height") is not None else None,
            html=str(record.get("html") or ""),
        )

    def to_raw(self) -> dict[str, Any]:
        if self.html is None:
            return {"number": self.number, "failed": True, "error": self.error or "failed"}
        record: dict[str, Any] = {
            "number": self.number,
            "width": self.width,
            "height": self.height,
            "html": self.html,
        }
        return record


def raw_payload(pages: Sequence[PageInput]) -> bytes:
    """`{"pages": [...]}` — the bytes both the command and the strategy keep; canonical JSON,
    so the same pages give the same bytes."""
    payload = {"pages": [page.to_raw() for page in sorted(pages, key=lambda p: p.number)]}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=None).encode()


def pages_from_raw(raw: bytes) -> list[PageInput]:
    payload = json.loads(raw)
    records = payload["pages"] if isinstance(payload, dict) else payload
    return [PageInput.from_raw(record) for record in records]


def page_contents(pages: Sequence[PageInput]) -> tuple[list[PageHtml], list[str]]:
    """Stage 1: the model's pages as canonical page HTML, with what was wrong with them.

    A page whose HTML does not parse cleanly keeps whatever the parser could make of it; the
    problems travel to the snapshot's stats rather than back to a model.
    """
    contents: list[PageHtml] = []
    problems: list[str] = []
    for page in sorted(pages, key=lambda p: p.number):
        if page.html is None:
            contents.append(PageHtml(number=page.number, failed=True))
            continue
        html, page_problems = as_page_html(page.html, page.number)
        problems += [f"page {page.number}: {problem}" for problem in page_problems]
        contents.append(
            PageHtml(
                number=page.number,
                html=html,
                width=page.width,
                height=page.height,
                thumbnail=page.thumbnail,
            )
        )
    return contents, problems
