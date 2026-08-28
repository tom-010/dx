import pytest
from django.db import models
from ninja import Schema

from config.models import BaseModel
from core.models import User


class DbTest:
    pytestmark = pytest.mark.django_db


def test_user_name():
    user = User.example()
    assert user.name == "John Doe"
    user.last_name = None
    assert user.name == "John"

    user = User.example()
    user.first_name = None
    assert user.name == "Doe"

    user.last_name = None
    assert user.name == ""


class Dummy(BaseModel):
    name = models.CharField(max_length=255)
    description = models.TextField()
    done = models.BooleanField(default=False)
    model_additional = models.IntegerField(default=0)


class DummySchema(Schema):
    name: str
    description: str = "default"  # `=` makes it optional
    done: bool = False
    additional: int = 123


def test_to_dict():
    dummy = Dummy(name="test", description="test", done=True, created="abc", modified="aasdf")
    d = dummy.to_dict()
    assert d["name"] == "test"
    assert d["description"] == "test"
    assert d["done"] is True
    assert d["created"]
    assert d["modified"]
    assert "id" in d


class TestSetPayload:
    def test_complete(self):
        schema = DummySchema(name="test", description="test", done=True, additional=42)
        dummy = Dummy(name="old", description="old", done=False, model_additional=123)
        dummy.set_payload(schema)
        assert dummy.name == "test"
        assert dummy.description == "test"
        assert dummy.done is True
        assert dummy.model_additional == 123  # not changed

    def test_incomplete(self):
        # with set_payload, the defaults are written to the model
        # if not set. If you don't want this, use set_payload_partial (see below)

        schema = DummySchema(
            name="test",
        )
        dummy = Dummy(name="old", description="old", done=False, model_additional=123)
        dummy.set_payload(schema)
        assert dummy.name == "test"
        assert dummy.description == "default"
        assert dummy.done is False

    def test_incomplete_2(self):
        # this time, only the things actually set are writen
        # not the defaults from the model

        schema = DummySchema(
            name="test",
        )
        dummy = Dummy(name="old", description="old", done=True, model_additional=123)
        dummy.set_payload_partial(schema)
        assert dummy.name == "test"
        assert dummy.description == "old"
        assert dummy.done is True
