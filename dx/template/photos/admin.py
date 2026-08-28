from core.helpers.admin_register_all import register_all

from . import models as models_module

# Auto-register all models from this app
register_all(models_module)