"""Admin pages for the timeline app: Django's defaults, see apps/core/admin.py."""

from apps.core.admin import register_all
from apps.timeline import models

register_all(models)
