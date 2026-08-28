from typing import Optional
from ninja import Schema


class PersonsIn(Schema):
    """Input schema for creating/updating Persons"""
    title: str
    description: str = ""
    is_active: bool = True


class PersonsOut(PersonsIn):
    """Output schema for Persons with ID"""
    id: str


class PersonsPatch(Schema):
    """Schema for partial updates to Persons"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool