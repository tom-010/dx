from typing import Optional
from ninja import Schema


class ReportIn(Schema):
    """Input schema for creating/updating Report"""
    title: str
    description: str = ""
    is_active: bool = True


class ReportOut(ReportIn):
    """Output schema for Report with ID"""
    id: str


class ReportPatch(Schema):
    """Schema for partial updates to Report"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool