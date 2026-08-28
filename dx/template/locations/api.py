from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import locations.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/locations", response=s.IdResult)
def create_locations(request, payload: s.LocationsIn):
    """Create a new Locations"""
    item = models.Locations.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/locations", response=list[s.LocationsOut])
def list_locations(request):
    """List all Locationss"""
    return models.Locations.objects.all()


@api.get("/locations/{item_id}", response=s.LocationsOut)
def get_locations(request, item_id: str):
    """Get a specific Locations by ID"""
    item = get_object_or_404(models.Locations, id=item_id)
    return item


@api.put("/locations/{item_id}", response=s.Success)
def update_locations(request, item_id: str, payload: s.LocationsIn):
    """Update a Locations"""
    item = get_object_or_404(models.Locations, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/locations/{item_id}", response=s.Success)
def patch_locations(request, item_id: str, payload: s.LocationsPatch):
    """Partially update a Locations"""
    item = get_object_or_404(models.Locations, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/locations/{item_id}", response=s.Success)
def delete_locations(request, item_id: str):
    """Delete a Locations"""
    item = get_object_or_404(models.Locations, id=item_id)
    item.delete()
    return {"success": True}