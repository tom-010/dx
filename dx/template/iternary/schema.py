from typing import Optional
from ninja import Schema


class IternaryIn(Schema):
    """Input schema for creating/updating Iternary"""
    title: str
    description: str = ""
    is_active: bool = True


class IternaryOut(IternaryIn):
    """Output schema for Iternary with ID"""
    id: str


class IternaryPatch(Schema):
    """Schema for partial updates to Iternary"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool