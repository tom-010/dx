import json
from pathlib import Path

from django.test import Client

from apps.accounts.models import User
from config.api import api

SPEC_FILE = Path(__file__).resolve().parents[3].parent / "openschema.json"


def _operation_ids(schema: dict[str, object]) -> list[str]:
    paths = schema["paths"]
    assert isinstance(paths, dict)
    return [
        op["operationId"]
        for methods in paths.values()
        for method, op in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def test_operation_ids_are_unique() -> None:
    schema = json.loads(json.dumps(api.get_openapi_schema()))
    ids = _operation_ids(schema)

    assert len(ids) == len(set(ids)), f"duplicate operation ids: {ids}"
    assert ids == [id_ for id_ in ids if id_.islower()], "operation ids should be snake_case"


def test_openapi_json_is_served(client: Client, staff_user: User) -> None:
    client.force_login(staff_user)  # docs are staff-only, see test_security.py
    response = client.get("/api/openapi.json")

    assert response.status_code == 200
    assert "list_datasets" in _operation_ids(response.json())


def test_committed_spec_matches_code() -> None:
    """`openschema.json` at the repo root is the frontend contract (./scripts/sync_schema.sh)."""
    assert SPEC_FILE.is_file(), f"missing {SPEC_FILE}; run ./scripts/sync_schema.sh"
    committed = json.loads(SPEC_FILE.read_text())
    current = json.loads(json.dumps(api.get_openapi_schema()))

    assert committed == current, "openschema.json is out of date; run ./scripts/sync_schema.sh"
