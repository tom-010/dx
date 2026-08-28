import pytest
from ninja.testing import TestClient

from core.api import api

# todos:
# class based to group
# django best practices
# distinguis between unit- and db-tests


class DbTest:
    pytestmark = pytest.mark.django_db


@pytest.mark.api
class ApiTest:
    pytestmark = pytest.mark.django_db
    client = TestClient(api)


def assert_contains(actual, expected):
    # TODO: test me
    for key, value in expected.items():
        # recursive is is dict
        if isinstance(value, dict):
            assert_contains(actual[key], value)

        # if ... only check if key exists
        if value == ...:
            assert key in actual
            continue

        assert actual[key] == value, f"{key} != {value}"


class TestTodos(ApiTest):
    def test_via_api(self):
        client = self.client
        client.post("/todos", json={"title": "test", "description": "test", "done": False}).json()
        todos = client.get("/todos").json()
        assert len(todos) == 1
        todo = todos[0]
        assert_contains(todo, {"id": ..., "title": "test", "description": "test", "done": False})
        todo = client.get(f'/todos/{todo["id"]}').json()
        assert_contains(todo, {"title": "test", "description": "test", "done": False})

        client.put(
            f'/todos/{todo["id"]}',
            json={"title": "test", "description": "test", "done": True},
        )
        todo = client.get(f'/todos/{todo["id"]}').json()
        assert todo["done"] is True

        client.put(f'/todos/{todo["id"]}/set_done', json={"done": False})
        todo = client.get(f'/todos/{todo["id"]}').json()
        assert todo["done"] is False

        client.delete(f'/todos/{todo["id"]}')

        todos = client.get("/todos").json()

        assert len(todos) == 0


class TestAdding(ApiTest):
    def test_adding(self):
        self.client.get("/add?a=1&b=2").json() == {"result": 3}
