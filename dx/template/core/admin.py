from core.helpers.admin_register_all import register_all

from . import models as models_module

register_all(models_module)

# do it manually via
# admin.site.register(models.Todo)
