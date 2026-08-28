from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import trips.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/", response=s.IdResult)
def create_trip(request, payload: s.TripIn):
    """Create a new Trips"""
    item = models.Trip.objects.create(
        **payload.dict(),
        owner=request.user.person
    )
    return s.IdResult(id=item.id)


@api.get("/", response=list[s.TripOut])
def list_trips(request) -> list[models.Trip]:
    return models.Trip.objects.filter(owner__user=request.user).all()


@api.get("/trips/{trip_id}", response=s.TripOut)
def get_trip(request, trip_id: str):
    """Get a specific Trips by ID"""
    item = get_object_or_404(models.Trip, id=trip_id, owner__user=request.user)
    return item


@api.put("/trips/{trip_id}", response=s.Success)
def update_trips(request, trip_id: str, payload: s.TripIn):
    """Update a Trips"""
    item = get_object_or_404(models.Trip, id=trip_id, owner__user=request.user)
    item.set_payload(payload)
    item.save()
    return s.Success(success=True)

@api.delete("/trips/{item_id}", response=s.Success)
def delete_trips(request, item_id: str):
    """Delete a Trips"""
    item = get_object_or_404(models.Trip, id=item_id, owner__user=request.user)
    item.delete()
    return s.Success(success=True)