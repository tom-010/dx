"""Admin pages for the gallery app: Django's defaults, see apps/core/admin.py."""

from apps.core.admin import register_all
from apps.gallery import models

register_all(models)
