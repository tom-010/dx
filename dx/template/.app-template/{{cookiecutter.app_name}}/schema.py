from typing import Optional
from ninja import Schema


class {{ cookiecutter.model_name }}In(Schema):
    """Input schema for creating/updating {{ cookiecutter.model_name }}"""
    title: str
    description: str = ""
    is_active: bool = True


class {{ cookiecutter.model_name }}Out({{ cookiecutter.model_name }}In):
    """Output schema for {{ cookiecutter.model_name }} with ID"""
    id: str


class {{ cookiecutter.model_name }}Patch(Schema):
    """Schema for partial updates to {{ cookiecutter.model_name }}"""
    title: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IdResult(Schema):
    """Response schema containing just an ID"""
    id: str


class Success(Schema):
    """Generic success response"""
    success: bool