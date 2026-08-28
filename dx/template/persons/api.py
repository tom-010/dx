from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import persons.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/persons", response=s.IdResult)
def create_persons(request, payload: s.PersonsIn):
    """Create a new Persons"""
    item = models.Persons.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/persons", response=list[s.PersonsOut])
def list_persons(request):
    """List all Personss"""
    return models.Persons.objects.all()


@api.get("/persons/{item_id}", response=s.PersonsOut)
def get_persons(request, item_id: str):
    """Get a specific Persons by ID"""
    item = get_object_or_404(models.Persons, id=item_id)
    return item


@api.put("/persons/{item_id}", response=s.Success)
def update_persons(request, item_id: str, payload: s.PersonsIn):
    """Update a Persons"""
    item = get_object_or_404(models.Persons, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/persons/{item_id}", response=s.Success)
def patch_persons(request, item_id: str, payload: s.PersonsPatch):
    """Partially update a Persons"""
    item = get_object_or_404(models.Persons, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/persons/{item_id}", response=s.Success)
def delete_persons(request, item_id: str):
    """Delete a Persons"""
    item = get_object_or_404(models.Persons, id=item_id)
    item.delete()
    return {"success": True}