from django.db import models
from config.models import BaseModel


class Iternary(BaseModel):
    """
    Iternary model for iternary app
    """
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Iternary"
        verbose_name_plural = "Iternarys"
        ordering = ["-created"]

    def __str__(self):
        return self.title