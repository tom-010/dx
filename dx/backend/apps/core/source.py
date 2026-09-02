"""The source code behind every recorded stack frame, stored once per distinct function body.

A stack frame names a file and a line. That is enough to *find* the code — if you have the
checkout, at the right commit, and the tree was clean when the write happened. A data reviewer
reading the explorer has none of that, and in development the tree is dirty every time. So each
frame also carries the sha of the function it was executing in, and this table holds that text:

    frame.sha        -> SourceSnippet.sha   (unique)
    frame.first_line    the line the function starts on, so the snippet can be numbered and the
                        executing line (`frame.line`) found inside it

**Content-addressed, like a git blob.** The key is the sha-256 of the function's text, so the
same function is stored exactly once no matter how many thousands of writes go through it — and
a *changed* function gets a new row, because its text changed. Nothing here depends on git: the
text comes from the running interpreter (`function_source`), which is the only source of truth
for what actually executed.

**Recording must cost nothing on the hot path.** Every `save()` records a stack; the same call
site fires thousands of times with the same frames; the source changes in one run out of many
thousands. So `store()` asks three places in order and stops at the first that knows:

1. a process-local set of shas known to be committed — zero queries, the answer 99.9% of the
   time after warm-up;
2. the shared cache (Redis in development and production, `CACHES`), so a fresh worker learns
   what its siblings already stored instead of asking Postgres;
3. Postgres, one `INSERT … ON CONFLICT DO NOTHING` for whatever is left.

Neither cache is authoritative: they are marked **after the transaction commits**
(`transaction.on_commit`), so a write that rolls back cannot leave the process believing a row
exists that never did. The one way to get a dangling sha is resetting the database under a
running process; the explorer says "source not stored" for those rather than failing.

Shared, not owned: code is not tenant data, and the same function writes for every tenant.
"""

from __future__ import annotations

import hashlib
import linecache
from types import CodeType

import pgtrigger
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import Func
from django.db.models.functions import Now

#: Shas this process has seen committed. Consulted before anything else; filled on commit only.
_known: set[str] = set()

_CACHE_PREFIX = "source:"


class SourceSnippet(models.Model):
    """One function's source text, keyed by its content. Immutable once written."""

    id = models.UUIDField(primary_key=True, db_default=Func(function="uuidv7"), editable=False)
    #: sha-256 of `text`, hex. The identity a stack frame references (`StackFrame.sha`).
    sha = models.CharField(max_length=64, unique=True, editable=False)
    text = models.TextField(editable=False)
    created = models.DateTimeField(db_default=Now(), editable=False)

    objects = models.Manager()

    class Meta:
        ordering = ["-created"]
        triggers = [
            # Content-addressed rows have no reason to change and every recorded frame may
            # depend on them; the same two guards `Lineage` has.
            pgtrigger.Protect(name="no_hard_delete", operation=pgtrigger.Delete),
            pgtrigger.Protect(name="append_only", operation=pgtrigger.Update),
        ]

    def __str__(self) -> str:
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return f"{self.sha[:12]} · {first[:60]}"

    @staticmethod
    def example() -> SourceSnippet:
        """A snippet of real code — this very function, read the way a recording reads it."""
        _first, _last, text = function_source(SourceSnippet.example.__code__)
        return SourceSnippet(sha=digest(text), text=text)


def function_source(code: CodeType) -> tuple[int, int, str]:
    """The whole function a code object belongs to: `(first line, last line, text)`.

    `co_firstlineno` is where the `def` (or its first decorator) starts; `co_lines()` maps the
    bytecode to line numbers, and its largest is the last line the function actually has code
    on. No tokenising, no parsing — a few microseconds — and it is exact for what was compiled,
    which is the point: this is the code that ran, not the code in some file somewhere.

    Empty text for a frame that has no file behind it (`<stdin>`, a frozen module).
    """
    first = code.co_firstlineno
    last = max((line for _, _, line in code.co_lines() if line), default=first)
    lines = linecache.getlines(code.co_filename)
    return first, last, "".join(lines[first - 1 : last])


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def store(snippets: dict[str, str]) -> None:
    """Make sure every `sha -> text` exists in the table, as cheaply as the situation allows.

    Call it inside the transaction that writes the frames referencing these shas: the rows then
    commit or roll back together, and the process only starts trusting them once they are in.
    See the module docstring for the three tiers and why the caches are marked on commit.
    """
    missing = {sha: text for sha, text in snippets.items() if sha and text and sha not in _known}
    if not missing:
        return

    unknown_to_cache = missing
    try:
        seen = cache.get_many([_CACHE_PREFIX + sha for sha in missing])
        unknown_to_cache = {
            sha: text for sha, text in missing.items() if _CACHE_PREFIX + sha not in seen
        }
    except Exception:  # noqa: S110 - the cache is an optimisation; an unreachable one is a miss
        pass

    if unknown_to_cache:
        SourceSnippet.objects.bulk_create(
            [SourceSnippet(sha=sha, text=text) for sha, text in unknown_to_cache.items()],
            ignore_conflicts=True,  # ON CONFLICT DO NOTHING: a sibling worker may have won the race
        )

    def remember() -> None:
        _known.update(missing)
        try:
            cache.set_many({_CACHE_PREFIX + sha: 1 for sha in missing}, timeout=None)
        except Exception:  # noqa: S110 - see above
            pass

    transaction.on_commit(remember)


def forget() -> None:
    """Drop the process-local memory. For tests, whose transactions never commit — a callback
    forced to run there would otherwise leave later tests trusting rows that were rolled back."""
    _known.clear()
