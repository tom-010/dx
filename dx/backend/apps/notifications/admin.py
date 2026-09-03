"""Admin pages for the notifications app: Django's defaults, see apps/core/admin.py."""

from apps.core.admin import register_all
from apps.notifications import models

register_all(models)
