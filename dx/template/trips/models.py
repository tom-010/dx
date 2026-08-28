from __future__ import annotations
from django.db import models
from config.models import BaseModel
from persons.models import Person


class Trip(BaseModel):
    """
    Captures Metadata about a trip 
    """
    name = models.TextField()
    synopsis = models.TextField(blank=True, default="")
    owner = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="owned_trips")

    class Meta:
        verbose_name = "Trip"
        verbose_name_plural = "Trips"
        ordering = ["-created"]

    @staticmethod
    def example() -> Trip:
        return Trip(
            name="USA West Coast",
            synopsis="California, Utah, Arizona road trip",
            owner=Person.example()
        )

    def __str__(self):
        return self.title


