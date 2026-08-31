"""Tenant-safe cache keys.

Django's cache is shared by every process and every user; a key that does not name the user
serves one user's cached value to the next. Feature code builds keys with `tenant_cache_key()`
and never calls `cache.get/set` with a bare string (apps/core/tests/test_tenancy.py checks
tenant apps for raw `cache.` calls).
"""

from django.core.cache import cache
from django_scopes import get_scope


def tenant_cache_key(name: str) -> str:
    """`tenant:<user id>:<name>` for the active tenant scope. Raises outside one."""
    user_id = get_scope().get("user")
    if user_id is None:
        raise LookupError(f"tenant_cache_key({name!r}) needs an active tenant scope")
    return f"tenant:{user_id}:{name}"


def tenant_cache_get(name: str, default: object = None) -> object:
    return cache.get(tenant_cache_key(name), default)


def tenant_cache_set(name: str, value: object, timeout: int | None = None) -> None:
    cache.set(tenant_cache_key(name), value, timeout)
