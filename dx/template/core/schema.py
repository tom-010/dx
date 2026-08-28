from ninja import Schema
from ninja_schema import ModelSchema, model_validator

from core.models import User


class IntResult(Schema):
    result: int


class Profile(Schema):
    name: str
    firstName: str


class TodoIn(Schema):
    title: str
    description: str = ""
    done: bool = False


class TodoOut(TodoIn):
    id: str


class IdResult(Schema):
    id: str


class SetDone(Schema):
    done: bool


class Success(Schema):
    success: bool


class TodoStats(Schema):
    total: int
    completed: int
    pending: int
    completion_rate: float


class TodoHistory(Schema):
    id: str
    title: str
    description: str
    done: bool
    created: str
    modified: str


class UserSchema(ModelSchema):
    class Config:
        model = User
        exclude = ["password"]
        extra = "forbid"

    @model_validator("email")
    def validate_email(cls, email):
        if not email.endswith("@example.com"):
            raise ValueError("Only example.com emails are allowed")
        return email

