from django.db import models
from config.models import BaseModel


class {{ cookiecutter.model_name }}(BaseModel):
    """
    {{ cookiecutter.model_name }} model for {{ cookiecutter.app_name }} app
    """
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "{{ cookiecutter.verbose_name }}"
        verbose_name_plural = "{{ cookiecutter.verbose_name }}s"
        ordering = ["-created"]

    def __str__(self):
        return self.title