"""Admin pages for the datasets app: Django's defaults, see apps/core/admin.py."""

from apps.core.admin import register_all
from apps.datasets import models

register_all(models)
