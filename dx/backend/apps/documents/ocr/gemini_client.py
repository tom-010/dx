"""Stage 1 for a scan: one Gemini request per page image, and the repair call behind it.

Ported from `manage.py playground gemini`: structured output through `response_schema`,
thinking level MINIMAL, automatic function calling off, streamed. The SDK is imported inside
the functions that need it — it is heavy, and nothing else in the app should pay for it.

Two models: the page reader (vision, cheap, one call per page) and the repairer, which only
ever sees HTML that did not parse and is asked to fix the markup and nothing else. A page that
is still broken after that is failed, and the run becomes PARTIAL rather than losing the rest.

The prompt is part of the extractor's identity: `PROMPT_VERSION` and `prompt_sha256()` go into
the `Extractor` row's config, and any change to the prompt or the schema bumps
`GeminiOcrStrategy.tool_version` — that is what keeps two snapshots comparable.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from apps.documents.extraction import Block
from apps.documents.ocr.page_html import (
    DATING_PROMPT,
    NAMING_PROMPT,
    PROMPT,
    REPAIR_PROMPT,
    dating_schema,
    naming_schema,
    prompt_for,
    response_schema,
    strip_fence,
)

MODEL = "gemini-3.5-flash-lite"
#: What repairs an answer that did not parse — a cheap model doing a mechanical job.
REPAIR_MODEL = "gemini-3.5-flash"
PROMPT_VERSION = 2
#: How much of the previous page's last block the next request sees.
TAIL_CHARS = 500
FIRST_PAGE = "first page"
#: `DocumentContent.title` is 500 characters; a name is a line, not a paragraph.
TITLE_LIMIT = 200


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT.encode()).hexdigest()


def tail_context(blocks: Sequence[Block]) -> str:
    """The previous page's last block — its tag and up to `TAIL_CHARS` of its text. This is
    the only thing the model is told about the rest of the document."""
    if not blocks:
        return "the previous page was blank"
    last = blocks[-1]
    text = (last.text or last.table_html or "")[-TAIL_CHARS:]
    return f"{last.tag}: {text}" if text else last.tag


class PageFailed(Exception):
    """The page could not be read: refused, or not valid JSON after the repair retry."""


class PageReader(Protocol):
    """What the pipeline needs of a reader — `GeminiPageReader`, or a stub in tests."""

    def read(self, png: bytes, number: int, total: int, tail: str) -> str: ...

    def repair(self, html: str, problems: Sequence[str]) -> str: ...

    def date(self, html: str) -> dict[int, tuple[str, float]]: ...

    def name(self, html: str) -> str: ...


class GeminiPageReader:
    """Reads one page per request, and repairs markup on demand. 429/5xx: exponential
    backoff."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = MODEL,
        repair_model: str = REPAIR_MODEL,
        retries: int = 3,
        backoff: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from google import genai  # noqa: PLC0415 - heavy; only where a request is made

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._repair_model = repair_model
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def read(self, png: bytes, number: int, total: int, tail: str) -> str:
        """One page image as HTML. Retries a rate-limited or failing API; a page whose answer
        is empty is a blank page, not an error."""
        from google.genai import errors, types  # noqa: PLC0415

        message = prompt_for(number, total, tail)
        attempt = 0
        while True:
            contents = types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=png, mime_type="image/png"),
                    types.Part.from_text(text=message),
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
                payload = json.loads(text)
            except ValueError as exc:
                raise PageFailed(f"the answer is not JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise PageFailed("the answer is not an object")
            if payload.get("blank") is True:
                return ""  # the model says the page carries nothing; believe it
            html = payload.get("html")
            if not isinstance(html, str):
                raise PageFailed("the answer carries no html")
            return strip_fence(html)

    def repair(self, html: str, problems: Sequence[str]) -> str:
        """Broken markup in, valid markup out — the content untouched. Failure returns the
        original: a repair that cannot be made is not a reason to lose the page."""
        from google.genai import errors, types  # noqa: PLC0415

        prompt = REPAIR_PROMPT.format(problems="\n".join(f"- {p}" for p in problems), html=html)
        try:
            answer = self._client.models.generate_content(
                model=self._repair_model,
                contents=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
                config=types.GenerateContentConfig(
                    temperature=0,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.MINIMAL
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    response_mime_type="application/json",
                    response_schema=response_schema(),
                ),
            )
            payload = json.loads(answer.text or "{}")
        except errors.APIError, ValueError:
            return html
        repaired = payload.get("html") if isinstance(payload, dict) else None
        return strip_fence(repaired) if isinstance(repaired, str) and repaired.strip() else html

    def date(self, html: str) -> dict[int, tuple[str, float]]:
        """When the information in each tag originates: `{nid: (EDTF, confidence)}`.

        One call for the whole document, on the assembled HTML — the model needs the sections
        and the datelines around a paragraph to place it, which a single page cannot give it.
        A tag it cannot date is simply absent; nothing here invents a date, and what comes back
        is an `INFERRED` estimate that any printed dateline still overrules
        (`apps/documents/dating.py`).
        """
        from google.genai import errors, types  # noqa: PLC0415

        try:
            answer = self._client.models.generate_content(
                model=self._model,
                contents=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=DATING_PROMPT.format(html=html))],
                ),
                config=types.GenerateContentConfig(
                    temperature=0,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.MINIMAL
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    response_mime_type="application/json",
                    response_schema=dating_schema(),
                ),
            )
            payload = json.loads(answer.text or "{}")
        except errors.APIError, ValueError:
            return {}
        entries = payload.get("dates") if isinstance(payload, dict) else None
        found: dict[int, tuple[str, float]] = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            nid, edtf, conf = entry.get("nid"), entry.get("edtf"), entry.get("confidence")
            if isinstance(nid, int) and isinstance(edtf, str) and edtf.strip():
                found[nid] = (edtf.strip(), float(conf) if isinstance(conf, int | float) else 0.5)
        return found

    def name(self, html: str) -> str:
        """What to call this document, read off the document itself.

        The last step of the pipeline and the cheapest: one line for a whole file. A file name
        says what a scanner called it; this says what it is.
        """
        from google.genai import errors, types  # noqa: PLC0415

        try:
            answer = self._client.models.generate_content(
                model=self._model,
                contents=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=NAMING_PROMPT.format(html=html))],
                ),
                config=types.GenerateContentConfig(
                    temperature=0,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.MINIMAL
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    response_mime_type="application/json",
                    response_schema=naming_schema(),
                ),
            )
            payload = json.loads(answer.text or "{}")
        except errors.APIError, ValueError:
            return ""
        title = payload.get("title") if isinstance(payload, dict) else None
        return " ".join(title.split())[:TITLE_LIMIT] if isinstance(title, str) else ""
