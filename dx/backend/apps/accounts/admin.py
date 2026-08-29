from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import ApiToken, RefreshToken, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin[User]):
    readonly_fields = ["id", "last_login", "date_joined"]


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin[ApiToken]):
    list_display = ["name", "user", "is_active", "created", "last_used"]
    list_filter = ["is_active"]
    search_fields = ["name", "user__username"]
    readonly_fields = ["id", "token", "created", "modified", "last_used"]


@admin.register(RefreshToken)
class RefreshTokenAdmin(admin.ModelAdmin[RefreshToken]):
    """Logins. Deactivating one ends that session at its next refresh (≤ the access lifetime)."""

    list_display = ["user", "is_active", "expires", "created"]
    list_filter = ["is_active"]
    search_fields = ["user__username"]
    readonly_fields = ["id", "user", "expires", "created", "modified"]
