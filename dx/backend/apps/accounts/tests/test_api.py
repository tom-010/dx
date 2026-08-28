import pytest
from django.test import Client

from apps.accounts import services
from apps.accounts.models import ApiToken, User
from config.env import env

pytestmark = pytest.mark.django_db


def _bearer(token: str) -> Client:
    return Client(headers={"Authorization": f"Bearer {token}"})


def test_login_returns_a_working_token(client: Client, user: User) -> None:
    response = client.post(
        "/api/auth/login",
        {"username": "alice", "password": "correct horse battery"},
        content_type="application/json",
    )

    assert response.status_code == 200
    token = response.json()["access_token"]

    me = _bearer(token).get("/api/auth/me")
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


def test_refresh_issues_a_new_token(auth_client: Client) -> None:
    response = auth_client.post("/api/auth/refresh")

    assert response.status_code == 200
    assert _bearer(response.json()["access_token"]).get("/api/auth/me").status_code == 200


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


def test_expired_or_tampered_jwt_is_rejected(user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env, "ACCESS_TOKEN_LIFETIME_DAYS", -1)
    expired = services.issue_access_token(user)
    monkeypatch.setattr(env, "ACCESS_TOKEN_LIFETIME_DAYS", 7)
    tampered = services.issue_access_token(user) + "x"

    assert services.user_from_access_token(expired) is None
    assert services.user_from_access_token(tampered) is None
    assert services.user_from_access_token(services.issue_access_token(user)) == user
