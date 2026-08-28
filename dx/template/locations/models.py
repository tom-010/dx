from django.db import models
from config.models import BaseModel


class LocationPoint(BaseModel): # TODO: capture something like area, address, etc.
    name = models.TextField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    was_interpolated = models.BooleanField(default=False)
    # TODO: interpolation sources

    class Meta:
        verbose_name = "Location"
        verbose_name_plural = "Locations"
        ordering = ["-created"]

    def __str__(self):
        if self.name:
            return self.name
        if self.latitude is not None and self.longitude is not None:
            return f"Location ({self.latitude}, {self.longitude})"
        return str(self.id)


class TravelDistance(BaseModel):
    from_location = models.ForeignKey(
        LocationPoint, on_delete=models.CASCADE, related_name="distances_from"
    )
    to_location = models.ForeignKey(
        LocationPoint, on_delete=models.CASCADE, related_name="distances_to"
    )
    distance_m = models.FloatField()

    class Meta:
        verbose_name = "Travel Distance"
        verbose_name_plural = "Travel Distances"
        ordering = ["-created"]

    def __str__(self):
        return f"Distance from {self.from_location} to {self.to_location}: {self.distance_m} m"