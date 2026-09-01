"""The command index behind `manage.py tui`: every management command, grouped and searchable.

Django's own `manage.py help` prints the names grouped by the app that ships them. This adds
each command's one-line help — which means importing all ~50 command modules, about 0.1s — and
a fuzzy search over both, so half a remembered name is enough to find one.

Pure data on purpose: `apps/core/management/commands/tui.py` does the prompting and printing,
this module is what the tests exercise.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

from django.core.management import execute_from_command_line, get_commands, load_command_class

#: Where a command comes from, in the order the list shows them: ours first.
GROUPS = ("project", "django", "third party")


@dataclass(frozen=True)
class CommandInfo:
    """One `manage.py <name>`, as the index shows it."""

    name: str
    #: The app that ships it, as `get_commands()` reports it ("apps.core", "django.core").
    app: str
    #: First line of the command's help, or the import error that hid it.
    help: str
    #: False when the module failed to import — a broken command must still be listed, or the
    #: one tool that could show you the error is the one that swallows it.
    loaded: bool = True

    @property
    def group(self) -> str:
        return group_of(self.app)


def group_of(app: str) -> str:
    if app.startswith("apps."):
        return "project"
    if app.startswith("django."):
        return "django"
    return "third party"


def first_line(text: object) -> str:
    """The first non-empty line of a command's `help` (Django's may be a lazy translation)."""
    for line in str(text or "").splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def describe(name: str, app: str) -> CommandInfo:
    """Load one command far enough to read its help; never raise."""
    try:
        klass = load_command_class(app, name)
    except Exception as error:
        return CommandInfo(name, app, f"{type(error).__name__}: {error}", loaded=False)
    return CommandInfo(name, app, first_line(getattr(klass, "help", "")))


def command_index() -> list[CommandInfo]:
    """Every available command: this project's first, then Django's, then everything else."""
    found = [describe(name, app) for name, app in get_commands().items()]
    return sorted(found, key=lambda command: (GROUPS.index(command.group), command.name))


def full_help(name: str, width: int = 78) -> str:
    """The command's own `--help`, captured, wrapped at `width`.

    Captured rather than reassembled: click and argparse each know their own options, this
    module does not, and a usage line that has drifted from the real one is worse than none.
    Both of them exit the process when they are done printing, which is what the `SystemExit`
    is — not a failure.

    `COLUMNS` is how both of them decide where to wrap (`shutil.get_terminal_size`), so a pane
    narrower than the terminal has to say so or every line is wrapped twice.
    """
    printed = io.StringIO()
    previous = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = str(max(40, width))
    try:
        with redirect_stdout(printed), redirect_stderr(printed):
            execute_from_command_line(["manage.py", name, "--help"])
    except SystemExit:
        pass
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    finally:
        os.environ["COLUMNS"] = previous if previous is not None else ""
        if previous is None:
            del os.environ["COLUMNS"]
    return printed.getvalue().strip()


def fuzzy_score(query: str, text: str) -> int | None:
    """How well `text` matches `query`; `None` when it does not match at all.

    A very small fzf: every character of the query has to appear in `text`, in order. Runs of
    adjacent characters, matches at the start of a word and a prefix hit all score higher, which
    is what puts `pull_tenant` above `delete_tenant` for "pu" and `hello_world` above
    `shell_as` for "hw". An empty query matches everything, equally.
    """
    wanted, haystack = query.strip().lower(), text.lower()
    if not wanted:
        return 0

    score, position, previous = 0, 0, -2
    for char in wanted:
        found = haystack.find(char, position)
        if found < 0:
            return None
        if found == previous + 1:
            score += 8  # a contiguous run reads as "the word I typed"
        if found == 0 or haystack[found - 1] in "_-. ":
            score += 4  # start of a word: the letters people abbreviate with
        score -= found - position  # ...minus how much had to be skipped to get here
        position, previous = found + 1, found

    if haystack.startswith(wanted):
        score += 40
    elif wanted in haystack:
        score += 20
    return score


def terms_score(query: str, text: str) -> int | None:
    """`fuzzy_score` for a whole query: **every** whitespace-separated term has to match.

    fzf's rule, and what makes typing more words narrow the list instead of breaking it: "del
    ten" finds `delete_tenant`, and the terms need not be in that order. The score is their
    sum, so a query that matches twice over ranks above one that only just matches.
    """
    total = 0
    for term in query.split():
        score = fuzzy_score(term, text)
        if score is None:
            return None
        total += score
    return total


#: A description hit never outranks a name that matches: the name is what people type.
HELP_PENALTY = 30


def help_score(query: str, help_text: str) -> int | None:
    """`terms_score` over a command's description — as fuzzy as the name, ranked below it.

    Deliberately not gated on how dense the match is: a subsequence scattered across a sentence
    scores badly (every skipped character costs), so the noise sorts to the bottom on its own
    rather than being filtered out along with "polic" → "…the RLS policies…".
    """
    score = terms_score(query, help_text)
    return None if score is None else score - HELP_PENALTY


def search(
    commands: Iterable[CommandInfo],
    query: str,
    *,
    descriptions: bool = False,
    recent: Sequence[str] = (),
) -> list[CommandInfo]:
    """The commands matching `query`, best first; an empty query keeps the index order.

    Fuzzy throughout, in the sense fzf means: each of the query's terms has to appear in the
    text as a subsequence, nothing more, and the ranking does the rest — "hw" finds
    `hello_world`, "del ten" finds `delete_tenant`. `descriptions` searches what each command
    is *for* as well, the same way, so "polic" finds `rls_sync` ("…the RLS policies…"). It is
    off by default because the name is what you usually half-remember, and a sentence matches
    far more loosely than a name does.

    `recent` (command names, newest first — `apps/core/usage.py`) breaks ties: between two
    equally good matches the one you ran last comes first, which over a few days is almost
    always the one you meant.
    """
    indexed = list(enumerate(commands))
    if not query.strip():
        return [command for _, command in indexed]

    used = {name: rank for rank, name in enumerate(recent)}
    ranked = []
    for position, command in indexed:
        by_name = terms_score(query, command.name)
        by_help = help_score(query, command.help) if descriptions else None
        scores = [score for score in (by_name, by_help) if score is not None]
        if scores:
            # Then the most recently used, then the index order (this project's first).
            ranked.append((-max(scores), used.get(command.name, len(used)), position, command))
    return [command for *_, command in sorted(ranked)]


def group_by_app(commands: Iterable[CommandInfo]) -> list[tuple[str, list[CommandInfo]]]:
    """The commands under the app that ships them ("apps.core", "django.core", …).

    Groups keep the order their first member arrived in, which is the caller's order: the index
    order (this project first) with no query, best-match order with one — so the top group holds
    the best match either way.
    """
    grouped: dict[str, list[CommandInfo]] = {}
    for command in commands:
        grouped.setdefault(command.app, []).append(command)
    return list(grouped.items())
