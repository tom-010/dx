from typing import Optional
from ninja import Schema


class LocationsIn(Schema):
    """Input schema for creating/updating Locations"""
    title: str
    description: str = ""
    is_active: bool = True


class LocationsOut(LocationsIn):
    """Output schema for Locations with ID"""
    id: str


class LocationsPatch(Schema):
    """Schema for partial updates to Locations"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool