from typing import Optional
from ninja import Schema


class TripIn(Schema):
    """Input schema for creating/updating Trips"""
    name: str
    synopsis: str = ""


class TripOut(TripIn):
    """Output schema for Trips with ID"""
    id: str


class TripsPatch(Schema):
    """Schema for partial updates to Trips"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool