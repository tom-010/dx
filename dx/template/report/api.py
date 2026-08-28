from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import report.schema as s
from . import models

api = Router()

logger = getLogger(__name__)


@api.post("/report", response=s.IdResult)
def create_report(request, payload: s.ReportIn):
    """Create a new Report"""
    item = models.Report.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/report", response=list[s.ReportOut])
def list_report(request):
    """List all Reports"""
    return models.Report.objects.all()


@api.get("/report/{item_id}", response=s.ReportOut)
def get_report(request, item_id: str):
    """Get a specific Report by ID"""
    item = get_object_or_404(models.Report, id=item_id)
    return item


@api.put("/report/{item_id}", response=s.Success)
def update_report(request, item_id: str, payload: s.ReportIn):
    """Update a Report"""
    item = get_object_or_404(models.Report, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/report/{item_id}", response=s.Success)
def patch_report(request, item_id: str, payload: s.ReportPatch):
    """Partially update a Report"""
    item = get_object_or_404(models.Report, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/report/{item_id}", response=s.Success)
def delete_report(request, item_id: str):
    """Delete a Report"""
    item = get_object_or_404(models.Report, id=item_id)
    item.delete()
    return {"success": True}