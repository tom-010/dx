from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import photos.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/photos", response=s.IdResult)
def create_photos(request, payload: s.PhotosIn):
    """Create a new Photos"""
    item = models.Photos.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/photos", response=list[s.PhotosOut])
def list_photos(request):
    """List all Photoss"""
    return models.Photos.objects.all()


@api.get("/photos/{item_id}", response=s.PhotosOut)
def get_photos(request, item_id: str):
    """Get a specific Photos by ID"""
    item = get_object_or_404(models.Photos, id=item_id)
    return item


@api.put("/photos/{item_id}", response=s.Success)
def update_photos(request, item_id: str, payload: s.PhotosIn):
    """Update a Photos"""
    item = get_object_or_404(models.Photos, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/photos/{item_id}", response=s.Success)
def patch_photos(request, item_id: str, payload: s.PhotosPatch):
    """Partially update a Photos"""
    item = get_object_or_404(models.Photos, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/photos/{item_id}", response=s.Success)
def delete_photos(request, item_id: str):
    """Delete a Photos"""
    item = get_object_or_404(models.Photos, id=item_id)
    item.delete()
    return {"success": True}