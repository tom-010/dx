from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import iternary.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/iternary", response=s.IdResult)
def create_iternary(request, payload: s.IternaryIn):
    """Create a new Iternary"""
    item = models.Iternary.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/iternary", response=list[s.IternaryOut])
def list_iternary(request):
    """List all Iternarys"""
    return models.Iternary.objects.all()


@api.get("/iternary/{item_id}", response=s.IternaryOut)
def get_iternary(request, item_id: str):
    """Get a specific Iternary by ID"""
    item = get_object_or_404(models.Iternary, id=item_id)
    return item


@api.put("/iternary/{item_id}", response=s.Success)
def update_iternary(request, item_id: str, payload: s.IternaryIn):
    """Update a Iternary"""
    item = get_object_or_404(models.Iternary, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/iternary/{item_id}", response=s.Success)
def patch_iternary(request, item_id: str, payload: s.IternaryPatch):
    """Partially update a Iternary"""
    item = get_object_or_404(models.Iternary, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/iternary/{item_id}", response=s.Success)
def delete_iternary(request, item_id: str):
    """Delete a Iternary"""
    item = get_object_or_404(models.Iternary, id=item_id)
    item.delete()
    return {"success": True}