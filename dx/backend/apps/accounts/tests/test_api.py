from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client

from apps.accounts import services
from apps.accounts.models import ApiToken, RefreshToken, User
from config.env import env

LOGIN = {"username": "alice", "password": "correct horse battery"}

pytestmark = pytest.mark.django_db


def _bearer(token: str) -> Client:
    return Client(headers={"Authorization": f"Bearer {token}"})


def _login(client: Client) -> dict[str, str]:
    response = client.post("/api/auth/login", LOGIN, content_type="application/json")
    assert response.status_code == 200
    tokens: dict[str, str] = response.json()
    return tokens


def _refresh(client: Client, refresh_token: str) -> tuple[int, dict[str, str]]:
    response = client.post(
        "/api/auth/refresh", {"refresh_token": refresh_token}, content_type="application/json"
    )
    body: dict[str, str] = response.json()
    return response.status_code, body


def test_login_returns_a_working_token(client: Client, user: User) -> None:
    tokens = _login(client)

    assert set(tokens) == {"access_token", "refresh_token"}
    me = _bearer(tokens["access_token"]).get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {
        "id": str(user.pk),
        "username": "alice",
        "email": "alice@example.com",
        "first_name": "",
        "last_name": "",
        "is_staff": False,
    }


def test_login_rejects_wrong_password(client: Client, user: User) -> None:
    response = client.post(
        "/api/auth/login",
        {"username": "alice", "password": "wrong"},
        content_type="application/json",
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid username or password"}


def test_input_schemas_reject_unknown_fields(client: Client, user: User) -> None:
    """StrictSchema: a misspelled field is a 422, not a silently ignored key."""
    response = client.post(
        "/api/auth/login",
        {"username": "alice", "password": "correct horse battery", "remember_me": True},
        content_type="application/json",
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_api_requires_a_bearer_token(client: Client) -> None:
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/datasets").status_code == 401
    assert _bearer("not-a-token").get("/api/datasets").status_code == 401
    # Public endpoints stay reachable.
    assert client.get("/api/health").status_code == 200


def test_refresh_rotates_the_pair(client: Client, user: User) -> None:
    tokens = _login(client)

    status, renewed = _refresh(client, tokens["refresh_token"])

    assert status == 200
    assert renewed["access_token"] != tokens["access_token"]
    assert renewed["refresh_token"] != tokens["refresh_token"]
    assert _bearer(renewed["access_token"]).get("/api/auth/me").status_code == 200
    # Single-use: the old refresh token is revoked, the new one keeps working.
    assert _refresh(client, tokens["refresh_token"]) == (
        401,
        {"detail": "Invalid or expired refresh token"},
    )
    assert _refresh(client, renewed["refresh_token"])[0] == 200
    # Every login/refresh leaves one active session row behind.
    assert RefreshToken.objects.filter(user=user, is_active=True).count() == 1


def test_refresh_needs_no_access_token(client: Client, user: User) -> None:
    """Public endpoint: the frontend calls it precisely because the access token has expired."""
    tokens = _login(client)
    expired = services.issue_access_token(user, lifetime=timedelta(seconds=-1))

    status, _ = _refresh(_bearer(expired), tokens["refresh_token"])

    assert status == 200


def test_refresh_rejects_other_token_kinds(client: Client, user: User) -> None:
    tokens = _login(client)

    # An access token is not a refresh token, and a refresh token cannot authenticate requests.
    assert _refresh(client, tokens["access_token"])[0] == 401
    assert _refresh(client, "garbage")[0] == 401
    assert _bearer(tokens["refresh_token"]).get("/api/auth/me").status_code == 401


def test_refresh_rejects_expired_or_inactive_sessions(client: Client, user: User) -> None:
    tokens = _login(client)
    session = RefreshToken.objects.get(user=user)
    session.expires = datetime.now(UTC) - timedelta(seconds=1)
    session.save()
    assert _refresh(client, tokens["refresh_token"])[0] == 401

    tokens = _login(client)  # also purges the expired row
    assert RefreshToken.objects.filter(user=user).count() == 1
    user.is_active = False
    user.save()
    assert _refresh(client, tokens["refresh_token"])[0] == 401


def test_logout_revokes_the_refresh_token(client: Client, user: User) -> None:
    tokens = _login(client)

    response = client.post(
        "/api/auth/logout",
        {"refresh_token": tokens["refresh_token"]},
        content_type="application/json",
    )

    assert response.status_code == 204
    assert _refresh(client, tokens["refresh_token"])[0] == 401
    assert not RefreshToken.objects.get(user=user).is_active
    # Idempotent, and harmless for tokens that never existed.
    for token in (tokens["refresh_token"], "garbage"):
        again = client.post(
            "/api/auth/logout", {"refresh_token": token}, content_type="application/json"
        )
        assert again.status_code == 204


def test_fixed_token_from_env_acts_as_superuser(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    User.objects.create_superuser("root", "root@example.com", "pw")
    monkeypatch.setattr(env, "API_FIXED_TOKEN", "ci-secret")

    assert _bearer("ci-secret").get("/api/auth/me").json()["username"] == "root"
    assert _bearer("ci-secret-wrong").get("/api/auth/me").status_code == 401


def test_personal_api_tokens_lifecycle(auth_client: Client, user: User) -> None:
    created = auth_client.post(
        "/api/auth/api-tokens", {"name": "CI"}, content_type="application/json"
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "CI"
    assert body["token"].startswith("tk_")
    assert body["last_used"] is None

    # The token authenticates as its owner and records its use.
    me = _bearer(body["token"]).get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == str(user.pk)
    assert ApiToken.objects.get(pk=body["id"]).last_used is not None

    listed = auth_client.get("/api/auth/api-tokens")
    assert [t["id"] for t in listed.json()] == [body["id"]]

    assert auth_client.delete(f"/api/auth/api-tokens/{body['id']}").status_code == 204
    assert auth_client.delete(f"/api/auth/api-tokens/{body['id']}").status_code == 404
    assert _bearer(body["token"]).get("/api/auth/me").status_code == 401
    assert auth_client.get("/api/auth/api-tokens").json() == []


def test_register_is_closed_by_default(client: Client) -> None:
    response = client.post(
        "/api/auth/register",
        {"username": "carol", "password": "long enough"},
        content_type="application/json",
    )

    assert response.status_code == 403
    assert not User.objects.filter(username="carol").exists()


def test_register_when_open(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env, "REGISTRATION_OPEN", True)
    payload = {"username": "carol", "password": "long enough", "email": "carol@example.com"}

    response = client.post("/api/auth/register", payload, content_type="application/json")

    assert response.status_code == 201
    assert _bearer(response.json()["access_token"]).get("/api/auth/me").status_code == 200
    duplicate = client.post("/api/auth/register", payload, content_type="application/json")
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Username already taken"}


def test_expired_or_tampered_jwt_is_rejected(user: User) -> None:
    expired = services.issue_access_token(user, lifetime=timedelta(seconds=-1))
    tampered = services.issue_access_token(user) + "x"

    assert services.user_from_access_token(expired) is None
    assert services.user_from_access_token(tampered) is None
    assert services.user_from_access_token(services.issue_access_token(user)) == user
