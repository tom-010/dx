# Mini-stub for django-click (no py.typed upstream; NOTES.md §5: own stubs instead of
# ignore_missing_imports). djclick re-exports click and replaces `command`/`group` with
# registrators that also expose the function to Django's command loader — same signatures.
from collections.abc import Callable
from typing import Any

from click import *  # noqa: F403
from click import command as command
from click import group as group

def pass_verbosity[**P, R](f: Callable[P, R]) -> Callable[P, R]: ...

__version__: str
__all__: list[Any]
