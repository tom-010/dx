"""Admin pages for the documents app: Django's defaults, see apps/core/admin.py."""

from apps.core.admin import register_all
from apps.documents import models

register_all(models)
