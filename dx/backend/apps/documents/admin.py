"""Admin pages for the documents module (see apps/datasets/admin.py for the rules)."""

from django.contrib import admin

from apps.core.admin import OwnedModelAdmin
from apps.documents.models import Document


@admin.register(Document)
class DocumentAdmin(OwnedModelAdmin[Document]):
    list_display = ["name", "content_type", "size", "version", "created", "deleted_at"]
    list_filter = ["deleted_at", "content_type"]
    search_fields = ["name"]
    # The bytes live in the object store and the row only holds the key; uploading through the
    # admin would bypass the batch validation in api.py.
    readonly_fields = [*OwnedModelAdmin.readonly_fields, "file", "size", "content_type"]
