"""Admin pages for the notes app (see apps/datasets/admin.py for the rules)."""

from django.contrib import admin

from apps.core.admin import OwnedModelAdmin
from apps.notes.models import Note


@admin.register(Note)
class NoteAdmin(OwnedModelAdmin[Note]):
    list_display = ["title", "version", "created", "deleted_at"]
    list_filter = ["deleted_at"]
    search_fields = ["title", "body"]
