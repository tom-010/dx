from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.http import HttpRequest

from apps.accounts.models import ApiToken, RefreshToken, User
from apps.core.admin import BaseModelAdmin


@admin.register(User)
class UserAdmin(DjangoUserAdmin[User]):
    readonly_fields = ["id", "last_login", "date_joined"]

    def has_delete_permission(self, request: HttpRequest, obj: User | None = None) -> bool:
        """Deleting a user cascades into their owned rows — and the admin runs outside any
        tenant context, so row-level security hides exactly those rows from the cascade: it
        would clear nothing, report success, and then fail at the foreign key on commit.
        `manage.py delete_tenant <username>` does it properly (rows and stored files)."""
        return False


@admin.register(ApiToken)
class ApiTokenAdmin(BaseModelAdmin[ApiToken]):
    """A shared table (no owner column, no tenant scope — authentication runs before any context
    exists), but still a `BaseModel`: the delete button would hit the `no_hard_delete` trigger,
    so it registers with the same base as everything else. Revoke with `is_active`, or
    soft-delete."""

    list_display = ["name", "user", "is_active", "created", "last_used"]
    list_filter = ["is_active", "deleted_at"]
    search_fields = ["name", "user__username"]
    readonly_fields = ["id", "token", "created", "modified", "version", "deleted_at", "last_used"]


@admin.register(RefreshToken)
class RefreshTokenAdmin(BaseModelAdmin[RefreshToken]):
    """Logins. Deactivating one ends that session at its next refresh (≤ the access lifetime).
    A shared table like `ApiToken` above — see there for why it is a `BaseModelAdmin`."""

    list_display = ["user", "is_active", "expires", "created"]
    list_filter = ["is_active", "deleted_at"]
    search_fields = ["user__username"]
    readonly_fields = ["id", "user", "expires", "created", "modified", "version", "deleted_at"]
