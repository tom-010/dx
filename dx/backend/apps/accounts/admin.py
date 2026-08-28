from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import ApiToken, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin[User]):
    readonly_fields = ["id", "last_login", "date_joined"]


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin[ApiToken]):
    list_display = ["name", "user", "is_active", "created", "last_used"]
    list_filter = ["is_active"]
    search_fields = ["name", "user__username"]
    readonly_fields = ["id", "token", "created", "modified", "last_used"]
