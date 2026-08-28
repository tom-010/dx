from typing import Optional
from ninja import Schema


class PhotosIn(Schema):
    """Input schema for creating/updating Photos"""
    title: str
    description: str = ""
    is_active: bool = True


class PhotosOut(PhotosIn):
    """Output schema for Photos with ID"""
    id: str


class PhotosPatch(Schema):
    """Schema for partial updates to Photos"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool