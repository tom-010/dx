from django.contrib import admin

from apps.datasets.models import Dataset


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin[Dataset]):
    list_display = ["name", "owner", "row_count", "created"]
    list_filter = ["owner"]
    search_fields = ["name"]
    readonly_fields = ["id", "created", "modified"]
