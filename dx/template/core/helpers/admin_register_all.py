from django.contrib import admin
from django.db.models import Model


def register_all(module):  # pragma: no cover
    # TODO: Add documentation
    # register everything that inherits from Model
    for m in dir(module):
        try:
            if m.startswith("__"):
                continue
            m = getattr(module, m)
            if isinstance(m, type) and issubclass(m, Model):
                if m._meta.abstract:
                    continue
                admin.site.register(m)
        except Exception as e:
            print(e)
