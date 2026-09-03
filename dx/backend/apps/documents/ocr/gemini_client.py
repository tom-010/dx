"""Stage 1 for a scan: the Gemini requests one page image takes.

One request per page: structured output through `response_schema`, **thinking at MINIMAL**
(the lowest this model family takes; a budget of 0 is a 400 on flash-lite), temperature 0,
automatic function calling off. Two more per document — one to date its tags,
one to name it. The SDK is imported inside the functions that need it: it is heavy, and
nothing else in the app should pay for it.

**A page is read once.** There is no review round and no repair call: what comes back is
untrusted input, validated and clamped by `page_html.parse_page`, and whatever is still wrong
with it is recorded in the snapshot's stats. A page that cannot be read at all is a failed
page, and the run becomes PARTIAL rather than losing the rest.

The prompt is part of the extractor's identity: `PROMPT_VERSION` and `prompt_sha256()` go into
the `Extractor` row's config, and any change to the prompt or the schema bumps
`GeminiOcrStrategy.tool_version` — that is what keeps two snapshots comparable.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    wait_chain,
    wait_fixed,
)

from apps.documents.ocr.page_html import (
    DATING_PROMPT,
    NAMING_PROMPT,
    PROMPT,
    dating_schema,
    naming_schema,
    prompt_for,
    response_schema,
    strip_fence,
)

if TYPE_CHECKING:
    from google.genai import types

MODEL = "gemini-3.5-flash-lite"
PROMPT_VERSION = 3
#: Longest any one request may take. A call that hangs would otherwise hang the whole run, and
#: it is also what bounds a shutdown: Python cannot kill a thread, so a worker asked to stop
#: leaves when the requests in flight have returned or timed out (`apps/core/worker_reload.py`).
REQUEST_TIMEOUT_S = 120
#: What a failed call waits before the next one: nine attempts, eight waits, 22 seconds of
#: sleeping in all. Many tries and short ones, because what fails here fails in bursts — a rate
#: limit, a model that is briefly busy — and none of that is helped by waiting a minute.
RETRY_WAITS = (1.0, 2.0, 2.0, 2.0, 3.0, 3.0, 4.0, 5.0)
#: …and it gives up here whatever the attempt count says. Nine attempts that each *time out*
#: would be eighteen minutes on one page, which is no longer retrying, it is hanging.
RETRY_BUDGET_S = 300
#: `DocumentContent.title` is 500 characters; a name is a line, not a paragraph.
TITLE_LIMIT = 200


log = structlog.get_logger(__name__)


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT.encode()).hexdigest()


def transient(exc: BaseException) -> bool:
    """Whether another try could plausibly go better.

    A rate limit, a server fault, or a call that never got an answer at all: yes. A 400 is the
    request itself being wrong — nine identical ones would be wrong nine times — and so is
    anything that is not the network failing.
    """
    import httpx  # noqa: PLC0415 - only where a request is made
    from google.genai import errors  # noqa: PLC0415

    if isinstance(exc, errors.APIError):
        code = exc.code or 0
        return code in (0, 429) or code >= 500
    return isinstance(exc, httpx.TransportError | TimeoutError | ConnectionError)


class PageFailed(Exception):
    """The page could not be read: refused, or the answer was not usable JSON."""


class PageReader(Protocol):
    """What the pipeline needs of a reader — `GeminiPageReader`, or a stub in tests."""

    def read(self, png: bytes, number: int, total: int) -> str: ...

    def date(self, html: str) -> dict[int, tuple[str, float]]: ...

    def name(self, html: str) -> str: ...


class GeminiPageReader:
    """Reads one page per request, and dates and names the assembled document.

    Every call goes through the same retry policy (`RETRY_WAITS`): a rate limit, a server
    fault or a connection that dropped is tried again, and anything else is not. What the
    retries cannot fix is handled per call — a page that will not read is a failed page, while
    a dating or a naming that will not run leaves the reading it already has.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = MODEL,
        timeout: float = REQUEST_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from google import genai  # noqa: PLC0415 - heavy; only where a request is made
        from google.genai import types  # noqa: PLC0415

        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout * 1000)),  # the SDK takes ms
        )
        self._model = model
        self._sleep = sleep

    def _retry(self, call: str) -> Retrying:
        """The policy, and a line in the log for every wait — a run that is being throttled
        should say so rather than just look slow."""

        def note(state: RetryCallState) -> None:
            failure = state.outcome.exception() if state.outcome is not None else None
            log.warning(
                "gemini_retry",
                call=call,
                attempt=state.attempt_number,
                waiting=round(state.next_action.sleep, 1) if state.next_action else 0.0,
                error=f"{type(failure).__name__}: {failure}"[:300],
            )

        return Retrying(
            stop=stop_after_attempt(len(RETRY_WAITS) + 1) | stop_after_delay(RETRY_BUDGET_S),
            wait=wait_chain(*(wait_fixed(seconds) for seconds in RETRY_WAITS)),
            retry=retry_if_exception(transient),
            before_sleep=note,
            sleep=self._sleep,
            reraise=True,
        )

    def _ask(
        self,
        call: str,
        parts: list[types.Part],
        schema: types.Schema,
        *,
        system: str | None = None,
    ) -> dict[str, Any]:
        """One request, retried while retrying is worth it, and its JSON answer as an object."""
        from google.genai import types  # noqa: PLC0415 - only where a request is made

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            # As low as this model family goes — `thinking_budget=0` is a 400 on flash-lite.
            # This is transcription, not reasoning: a budget spent thinking about a page is
            # latency and money the reading does not get.
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json",
            response_schema=schema,
        )

        def request() -> str:
            answer = self._client.models.generate_content(
                model=self._model,
                contents=types.Content(role="user", parts=parts),
                config=config,
            )
            return answer.text or "{}"

        payload = json.loads(self._retry(call)(request))
        if not isinstance(payload, dict):
            raise ValueError("the answer is not an object")
        return payload

    def read(self, png: bytes, number: int, total: int) -> str:
        """One page image as HTML. Retries a rate-limited or failing API; a page the model
        calls blank is a blank page, not an error."""
        from google.genai import types  # noqa: PLC0415

        parts = [
            types.Part.from_bytes(data=png, mime_type="image/png"),
            types.Part.from_text(text=prompt_for(number, total)),
        ]
        try:
            payload = self._ask("read", parts, response_schema(), system=PROMPT)
        except Exception as exc:  # noqa: BLE001 - the API or the network, after the last attempt
            raise PageFailed(f"{type(exc).__name__}: {exc}") from exc
        if payload.get("blank") is True:
            return ""  # the model says the page carries nothing; believe it
        html = payload.get("html")
        if not isinstance(html, str):
            raise PageFailed("the answer carries no html")
        return strip_fence(html)

    def date(self, html: str) -> dict[int, tuple[str, float]]:
        """When the information in each tag originates: `{nid: (EDTF, confidence)}`.

        One call for the whole document, on the assembled HTML — the model needs the sections
        and the datelines around a paragraph to place it, which a single page cannot give it.
        A tag it cannot date is simply absent; nothing here invents a date, and what comes back
        is an `INFERRED` estimate that any printed dateline still overrules
        (`apps/documents/dating.py`).
        """
        from google.genai import types  # noqa: PLC0415

        parts = [types.Part.from_text(text=DATING_PROMPT.format(html=html))]
        try:
            payload = self._ask("date", parts, dating_schema())
        except Exception as exc:  # noqa: BLE001 - best effort, but never silently
            log.warning("gemini_dating_failed", error=f"{type(exc).__name__}: {exc}")
            return {}
        entries = payload.get("dates")
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
        from google.genai import types  # noqa: PLC0415

        parts = [types.Part.from_text(text=NAMING_PROMPT.format(html=html))]
        try:
            payload = self._ask("name", parts, naming_schema())
        except Exception as exc:  # noqa: BLE001 - best effort, but never silently
            log.warning("gemini_naming_failed", error=f"{type(exc).__name__}: {exc}")
            return ""
        title = payload.get("title")
        return " ".join(title.split())[:TITLE_LIMIT] if isinstance(title, str) else ""
