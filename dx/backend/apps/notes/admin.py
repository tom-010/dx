"""Admin pages for the notes app: Django's defaults, see apps/core/admin.py."""

from apps.core.admin import register_all
from apps.notes import models

register_all(models)
