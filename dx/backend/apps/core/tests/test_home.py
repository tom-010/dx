"""The development front door at `/` (`config/home.py`).

Worth testing about a page of links is that the links are there and point at pages that exist —
a dead link on the first page of a fresh checkout is the whole thing it exists to fix — and that
it is not readable without the session it advertises, nor unable to end it.
"""

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from config.home import FRONTEND_URL


@pytest.fixture
def home_client(user: User) -> Client:
    """The page authenticates by session, like the admin — not by bearer token."""
    client = Client()
    client.force_login(user)
    return client


def test_anonymous_is_sent_to_the_login_page(client: Client) -> None:
    response = client.get("/")

    assert response.status_code == 302
    assert "/admin/login/" in response.headers["Location"]


def test_the_home_page_links_to_everything_this_process_serves(home_client: Client) -> None:
    response = home_client.get("/")

    assert response.status_code == 200
    body = response.content.decode()
    for href in (FRONTEND_URL, "/api/docs", "/explorer/", "/admin/"):
        assert href in body, href
    # And the local ones are mounted: each answers (a login redirect for all three), not 404.
    for href in ("/api/docs", "/explorer/", "/admin/"):
        assert home_client.get(href).status_code != 404, href


def test_it_says_who_is_signed_in(home_client: Client, user: User) -> None:
    assert user.get_username() in home_client.get("/").content.decode()


def test_the_logout_button_ends_the_session(home_client: Client) -> None:
    response = home_client.post("/logout/")

    assert response.status_code == 302
    assert home_client.get("/").status_code == 302  # logged out: back to the login page


def test_logging_out_is_not_something_a_link_can_do(home_client: Client) -> None:
    assert home_client.get("/logout/").status_code == 405
    assert home_client.get("/").status_code == 200  # still signed in


def test_the_admin_pages_are_left_out_when_the_admin_is_off(
    home_client: Client, settings: Settings
) -> None:
    """`ADMIN_ENABLED` can be off while DEBUG is on, and then both of its pages 404."""
    settings.ADMIN_ENABLED = False

    body = home_client.get("/").content.decode()

    assert "/admin/" not in body
    assert "/api/docs" not in body
