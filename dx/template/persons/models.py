from __future__ import annotations
from django.db import models
from config.models import BaseModel
from core.models import User


class Group(BaseModel):
    name = models.CharField(max_length=255)


    def __str__(self):
        return self.name


class PersonInGroup(BaseModel):
    person = models.ForeignKey('persons.Person', on_delete=models.CASCADE)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('person', 'group')
        verbose_name = "Person in Group"
        verbose_name_plural = "Persons in Groups"
        ordering = ["-group__created", "-person__created"]

    def __str__(self):
        return f"{self.person.name} in {self.group.name}"


class Person(BaseModel):
    name = models.CharField(max_length=255)
    nicknames = models.TextField(blank=True, default="")
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    class Meta:
        verbose_name = "Person"
        verbose_name_plural = "Persons"
        ordering = ["-created"]

    def example() -> Person:
        return Person(
            name="John Doe",
            nicknames="Johnny, JD"
        )

    @staticmethod
    def example(self, user=None) -> Person:
        return Person(
            name="Example Person",
        )

    def __str__(self):
        return self.name




class PersonOnATrip(BaseModel):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="trips")
    trip = models.ForeignKey('trips.Trip', on_delete=models.CASCADE, related_name="persons")
    bio = models.TextField(null=True, blank=True)
    fitness_level = models.TextField(null=True, blank=True)
    expectations = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = "Person on a Trip"
        verbose_name_plural = "Persons on Trips"
        ordering = ["-created"]

    def __str__(self):
        return f"{self.person.name} on {self.trip.name}"