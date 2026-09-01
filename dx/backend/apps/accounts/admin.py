"""Admin pages for the accounts app: Django's defaults, see apps/core/admin.py."""

from apps.accounts import models
from apps.core.admin import register_all

register_all(models)
