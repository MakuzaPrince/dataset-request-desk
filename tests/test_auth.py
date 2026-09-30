import logging

import pytest

from app.models import Role
from tests.conftest import PASSWORD, auth, make_user


def test_login_returns_token_that_authenticates(client, users):
    r = client.post("/api/auth/login", json={"email": "OPS@test.io ", "password": PASSWORD})
    assert r.status_code == 200
    token = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == "ops@test.io"
    assert me.json()["role"] == "operator"


@pytest.mark.parametrize("email,password", [("ops@test.io", "wrong"), ("nobody@test.io", PASSWORD)])
def test_bad_credentials_get_the_same_error(client, users, email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"


def test_passwords_are_hashed(users):
    assert users["ops"].password_hash != PASSWORD
    assert users["ops"].password_hash.startswith("$2")


def test_inactive_user_cannot_log_in(client, db):
    make_user(db, "gone@test.io", Role.operator, active=False)
    r = client.post("/api/auth/login", json={"email": "gone@test.io", "password": PASSWORD})
    assert r.status_code == 401


def test_deactivation_revokes_existing_token_immediately(client, db, users, h):
    headers = auth(users["ops"])
    assert client.get("/api/requests", headers=headers).status_code == 200
    r = client.patch(f"/api/users/{users['ops'].id}", json={"is_active": False}, headers=h["admin"])
    assert r.status_code == 200
    assert client.get("/api/requests", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/auth/me"),
        ("GET", "/api/requests"),
        ("POST", "/api/requests"),
        ("GET", "/api/requests/1"),
        ("POST", "/api/requests/1/transitions"),
        ("POST", "/api/requests/1/assignments"),
        ("GET", "/api/episodes"),
        ("POST", "/api/episodes/import"),
        ("GET", "/api/analytics"),
        ("GET", "/api/users"),
        ("GET", "/api/events"),
    ],
)
def test_every_api_endpoint_requires_authentication(client, method, path):
    assert client.request(method, path).status_code == 401
    bad = client.request(method, path, headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "up"}


def test_access_log_has_one_structured_line_per_request(client, h, users, caplog):
    with caplog.at_level(logging.INFO, logger="app.access"):
        client.get("/api/requests", headers=h["ops"])
        client.get("/health")
    records = [r for r in caplog.records if r.name == "app.access"]
    assert len(records) == 2
    authed, anon = records
    assert (authed.method, authed.path, authed.status, authed.user_id) == ("GET", "/api/requests", 200, users["ops"].id)
    assert anon.user_id is None and anon.status == 200
    assert isinstance(authed.duration_ms, float)
