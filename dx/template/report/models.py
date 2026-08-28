from django.db import models
from config.models import BaseModel
from photos.models import Photo


class TravelDay(BaseModel):

    day_number = models.PositiveIntegerField(default=0)
    date = models.DateField()
    headline = models.TextField()

    class Meta:
        verbose_name = "Travel Day"
        verbose_name_plural = "Travel Days"
        ordering = ["-created"]

    def __str__(self):
        return self.headline


class TravelDayPart(BaseModel):
    headline = models.TextField()
    ordering = models.PositiveIntegerField(default=0)
    day = models.ForeignKey(TravelDay, on_delete=models.CASCADE, related_name="parts")

    class Meta:
        verbose_name = "Travel Day Part"
        verbose_name_plural = "Travel Day Parts"
        ordering = ["ordering", "-created"]

    def __str__(self):
        return self.headline


class Paragraph(BaseModel):
    content = models.TextField()
    ordering = models.PositiveIntegerField(default=0)
    day_part = models.ForeignKey(TravelDayPart, on_delete=models.CASCADE, related_name="paragraphs")

    class Meta:
        verbose_name = "Paragraph"
        verbose_name_plural = "Paragraphs"
        ordering = ["ordering", "-created"]

    def __str__(self):
        return f"Paragraph {self.ordering} of {self.day_part.headline}"
    

class PhotoForParagraph(BaseModel):
    paragraph = models.ForeignKey(Paragraph, on_delete=models.CASCADE, related_name="photos")
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE)
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Photo for Paragraph"
        verbose_name_plural = "Photos for Paragraphs"
        ordering = ["ordering", "-created"]

    def __str__(self):
        return f"Photo {self.photo.id} for {self.paragraph}"


class PhotoForTravelDay(BaseModel):
    travel_day = models.ForeignKey(TravelDay, on_delete=models.CASCADE, related_name="photos")
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE)
    ordering = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Photo for Travel Day"
        verbose_name_plural = "Photos for Travel Days"
        ordering = ["ordering", "-created"]

    def __str__(self):
        return f"Photo {self.photo.id} for {self.travel_day}"