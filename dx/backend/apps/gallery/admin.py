from django.contrib import admin

from apps.gallery.models import MediaItem


@admin.register(MediaItem)
class MediaItemAdmin(admin.ModelAdmin[MediaItem]):
    list_display = ["name", "kind", "content_type", "size", "owner", "created"]
    list_filter = ["kind"]
    list_select_related = ["owner"]
    search_fields = ["name"]
