import pytest
from pydantic import ValidationError

from apps.core.schemas import StrictSchema


class Payload(StrictSchema):
    name: str


def test_strict_schema_rejects_unknown_fields() -> None:
    assert Payload(name="ok").name == "ok"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        Payload.model_validate({"name": "ok", "nmae": "typo"})
