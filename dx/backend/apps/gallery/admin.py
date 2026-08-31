"""Admin pages for the gallery module (see apps/datasets/admin.py for the rules)."""

from django.contrib import admin

from apps.core.admin import OwnedModelAdmin
from apps.gallery.models import MediaItem


@admin.register(MediaItem)
class MediaItemAdmin(OwnedModelAdmin[MediaItem]):
    list_display = ["name", "kind", "content_type", "size", "version", "created", "deleted_at"]
    list_filter = ["deleted_at", "kind"]
    search_fields = ["name"]
    readonly_fields = [*OwnedModelAdmin.readonly_fields, "file", "size", "content_type", "kind"]
