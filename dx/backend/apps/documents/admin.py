"""Admin pages for the documents app: Django's defaults (see apps/core/admin.py), except that
the snapshot tables and the blobs are read-only — the extraction pipeline is their only writer,
and a completed snapshot is never updated."""

from django.conf import settings
from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from apps.core.admin import register_all
from apps.documents import models

register_all(models)


class ReadOnlyAdmin(admin.ModelAdmin[Model]):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


if settings.ADMIN_ENABLED:
    for model in (models.Blob, models.DocumentContent, models.Page, models.Node, models.PageRegion):
        admin.site.unregister(model)
        admin.site.register(model, ReadOnlyAdmin)
