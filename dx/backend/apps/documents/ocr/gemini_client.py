"""The map step: one Gemini request per page image, validated, with retries.

Ported from `manage.py playground gemini`: structured output through `response_schema`,
thinking level MINIMAL, automatic function calling off, streamed. The SDK is imported inside
the functions that need it — it is heavy, and nothing else in the app should pay for it.

The prompt is part of the extractor's identity: `PROMPT_VERSION` and `prompt_sha256()` go into
the `Extractor` row's config, and any change to the prompt or the schema bumps
`GeminiOcrStrategy.tool_version` — that is what keeps two snapshots comparable.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from apps.documents.ocr.page_schema import (
    NormalizedBlock,
    PageBlocks,
    response_schema,
    validation_message,
)

MODEL = "gemini-3.5-flash-lite"
PROMPT_VERSION = 1
PROMPT = """You transcribe one scanned page of a document into structured blocks.

Rules:
- Transcribe verbatim in the original language (mostly German). No translation, no summary, \
no corrections: keep the original spelling, punctuation and mistakes.
- Return the blocks in reading order. Several columns: column by column, top to bottom \
within a column.
- One block per paragraph, heading, list item, table, figure, caption, or piece of page \
furniture. Classify running headers, footers and page numbers as page_header, page_footer \
or page_number — do not leave them out.
- Within the page, join words that a line break hyphenates. Keep a hyphen at the very end of \
the last block only when the word continues on the next page.
- List items: one block per item, kind list_item; leave the bullet glyph or the numbering out \
of the text.
- Headings: kind heading, level 1 (the largest) to 6.
- Tables: kind table, text empty, table_html a complete <table>…</table> using only thead, \
tbody, tr, th and td; colspan and rowspan are allowed; cell text verbatim.
- Figures, photos, stamps, signatures and handwriting you cannot read: kind figure with empty \
text. A caption next to a figure is its own caption block.
- box_2d is [ymin, xmin, ymax, xmax]: integers from 0 to 1000 over the whole image, y first.
- continues_from_previous_page is true on the first block only, and only when it \
grammatically continues the last block of the previous page quoted in the message. \
Otherwise false.
- A blank page has no blocks.
"""
#: How much of the previous page's last block the next request sees.
TAIL_CHARS = 500
FIRST_PAGE = "first page"


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT.encode()).hexdigest()


def tail_context(blocks: Sequence[NormalizedBlock]) -> str:
    """The previous page's last block — kind and up to `TAIL_CHARS` of its text."""
    if not blocks:
        return "the previous page was blank"
    last = blocks[-1]
    text = (last.text or last.table_html or "")[-TAIL_CHARS:]
    return f"{last.kind.value}: {text}" if text else last.kind.value


def page_message(number: int, total: int, tail: str) -> str:
    return f"Page {number} of {total}.\nLast block of the previous page — {tail}"


class PageFailed(Exception):
    """The page could not be read: refused, or not valid JSON after the repair retry."""


class PageReader(Protocol):
    """What the pipeline needs of a reader — `GeminiPageReader`, or a stub in tests."""

    def read(self, png: bytes, number: int, total: int, tail: str) -> dict[str, Any]: ...


class GeminiPageReader:
    """Reads one page per request. 429/5xx: exponential backoff; schema-invalid JSON: one
    repair retry with the validator's message appended; then the page fails."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = MODEL,
        retries: int = 3,
        backoff: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from google import genai  # noqa: PLC0415 - heavy; only where a request is made

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def read(self, png: bytes, number: int, total: int, tail: str) -> dict[str, Any]:
        """The validated raw response (`{"blocks": [...]}`) for one page image."""
        from google.genai import errors, types  # noqa: PLC0415

        message = page_message(number, total, tail)
        repair: str | None = None
        attempt = 0
        while True:
            text_part = message
            if repair is not None:
                text_part += (
                    f"\n\nYour previous answer was rejected: {repair}. "
                    "Return valid JSON that matches the schema exactly."
                )
            contents = types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=png, mime_type="image/png"),
                    types.Part.from_text(text=text_part),
                ],
            )
            config = types.GenerateContentConfig(
                system_instruction=PROMPT,
                temperature=0,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                response_mime_type="application/json",
                response_schema=response_schema(),
            )
            try:
                chunks = self._client.models.generate_content_stream(
                    model=self._model, contents=contents, config=config
                )
                text = "".join(chunk.text or "" for chunk in chunks)
            except errors.APIError as exc:
                code = exc.code or 0
                if (code == 429 or code >= 500) and attempt < self._retries:
                    self._sleep(self._backoff**attempt)
                    attempt += 1
                    continue
                raise PageFailed(f"{code}: {exc}") from exc
            try:
                raw = json.loads(text)
                PageBlocks.model_validate(raw)
                return dict(raw)
            except ValidationError as exc:
                problem = validation_message(exc)
            except ValueError as exc:
                problem = f"not JSON: {exc}"
            if repair is not None:
                raise PageFailed(f"invalid after the repair retry: {problem}")
            repair = problem
