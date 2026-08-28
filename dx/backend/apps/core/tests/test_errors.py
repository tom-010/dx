"""JSON error bodies for API clients (config/errors.py)."""

from django.http import HttpRequest
from django.test import Client
from ninja import NinjaAPI
from ninja.testing import TestClient
from pytest_django.fixtures import Settings

from config.errors import install_exception_handlers


def _api_that_raises() -> NinjaAPI:
    api = NinjaAPI(urls_namespace="test-errors")
    install_exception_handlers(api)

    @api.get("/boom")
    def boom(request: HttpRequest) -> None:
        raise RuntimeError("kaboom")

    return api


def test_debug_errors_return_json_with_traceback(settings: Settings) -> None:
    settings.DEBUG = True

    response = TestClient(_api_that_raises()).get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "RuntimeError: kaboom"
    assert any("kaboom" in line for line in body["traceback"])


def test_production_errors_are_left_to_django(settings: Settings) -> None:
    settings.DEBUG = False
    client = TestClient(_api_that_raises())

    try:
        client.get("/boom")
    except RuntimeError as exc:
        assert str(exc) == "kaboom"
    else:  # pragma: no cover
        raise AssertionError("exception should propagate to Django's handler500")


def test_unknown_api_path_is_a_json_404(client: Client) -> None:
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_json_404_for_clients_that_ask_for_json(client: Client) -> None:
    response = client.get("/admin/does-not-exist/", headers={"Accept": "application/json"})

    assert response.status_code in (404, 302)  # admin redirects anonymous users to login
    if response.status_code == 404:
        assert response.json() == {"detail": "Not Found"}
