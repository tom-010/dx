from django.db import models
from config.models import BaseModel
from persons.models import Person
from locations.models import LocationPoint



class Photo(BaseModel):
    """
    A photo, taken by a user
    """
    title = models.TextField()
    image = models.ImageField(upload_to="photos/")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="photos", null=True, blank=True)
    location = models.ForeignKey(LocationPoint, on_delete=models.CASCADE, related_name="photos", null=True, blank=True)

    class Meta:
        verbose_name = "Photo"
        verbose_name_plural = "Photos"
        ordering = ["-created"]

    def __str__(self):
        return self.title