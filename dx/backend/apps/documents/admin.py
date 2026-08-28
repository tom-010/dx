from django.contrib import admin

from apps.documents.models import Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin[Document]):
    list_display = ["name", "owner", "content_type", "size", "created"]
    list_filter = ["owner"]
    search_fields = ["name"]
    readonly_fields = ["id", "created", "modified"]
