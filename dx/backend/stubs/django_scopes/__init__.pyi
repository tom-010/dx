# Minimal stub for django-scopes (ships no type hints). Only what apps/core uses:
# the scope state (contextvar) and the error. The library's ScopedManager is not used —
# apps/core/models.py::OwnedManager applies the scope itself so `for_user()` keeps its type.
from contextlib import AbstractContextManager

version: str

class ScopeError(Exception): ...

def scope(**scope_kwargs: object) -> AbstractContextManager[None]: ...
def scopes_disabled() -> AbstractContextManager[None]: ...
def get_scope() -> dict[str, object]: ...

__all__ = ["ScopeError", "get_scope", "scope", "scopes_disabled", "version"]
