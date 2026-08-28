from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import {{ cookiecutter.app_name }}.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/{{ cookiecutter.app_name }}", response=s.IdResult)
def create_{{ cookiecutter.app_name }}(request, payload: s.{{ cookiecutter.model_name }}In):
    """Create a new {{ cookiecutter.model_name }}"""
    item = models.{{ cookiecutter.model_name }}.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/{{ cookiecutter.app_name }}", response=list[s.{{ cookiecutter.model_name }}Out])
def list_{{ cookiecutter.app_name }}(request):
    """List all {{ cookiecutter.model_name }}s"""
    return models.{{ cookiecutter.model_name }}.objects.all()


@api.get("/{{ cookiecutter.app_name }}/{item_id}", response=s.{{ cookiecutter.model_name }}Out)
def get_{{ cookiecutter.app_name }}(request, item_id: str):
    """Get a specific {{ cookiecutter.model_name }} by ID"""
    item = get_object_or_404(models.{{ cookiecutter.model_name }}, id=item_id)
    return item


@api.put("/{{ cookiecutter.app_name }}/{item_id}", response=s.Success)
def update_{{ cookiecutter.app_name }}(request, item_id: str, payload: s.{{ cookiecutter.model_name }}In):
    """Update a {{ cookiecutter.model_name }}"""
    item = get_object_or_404(models.{{ cookiecutter.model_name }}, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/{{ cookiecutter.app_name }}/{item_id}", response=s.Success)
def patch_{{ cookiecutter.app_name }}(request, item_id: str, payload: s.{{ cookiecutter.model_name }}Patch):
    """Partially update a {{ cookiecutter.model_name }}"""
    item = get_object_or_404(models.{{ cookiecutter.model_name }}, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/{{ cookiecutter.app_name }}/{item_id}", response=s.Success)
def delete_{{ cookiecutter.app_name }}(request, item_id: str):
    """Delete a {{ cookiecutter.model_name }}"""
    item = get_object_or_404(models.{{ cookiecutter.model_name }}, id=item_id)
    item.delete()
    return {"success": True}