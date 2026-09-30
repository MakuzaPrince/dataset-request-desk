"""Role-based access, enforced by the API regardless of what the UI shows."""
from datetime import date, timedelta

import pytest

from app.models import RequestStatus
from tests.conftest import SEED_CSV, make_episode, make_request

FUTURE = (date.today() + timedelta(days=10)).isoformat()


def test_client_sees_only_their_own_requests(client, db, users, h):
    mine = make_request(db, users["client_a"])
    make_request(db, users["client_b"])
    r = client.get("/api/requests", headers=h["client_a"])
    assert [x["id"] for x in r.json()["items"]] == [mine.id]
    assert r.json()["total"] == 1


def test_operator_sees_all_requests(client, db, users, h):
    make_request(db, users["client_a"])
    make_request(db, users["client_b"])
    assert client.get("/api/requests", headers=h["ops"]).json()["total"] == 2


def test_client_gets_404_for_another_clients_request(client, db, users, h):
    other = make_request(db, users["client_b"], status=RequestStatus.delivered)
    assert client.get(f"/api/requests/{other.id}", headers=h["client_a"]).status_code == 404
    assert client.get(f"/api/requests/{other.id}/assignments", headers=h["client_a"]).status_code == 404
    r = client.post(f"/api/requests/{other.id}/transitions", json={"to_status": "accepted"}, headers=h["client_a"])
    assert r.status_code == 404
    db.refresh(other)
    assert other.status == RequestStatus.delivered


def test_client_can_view_assignments_on_own_request(client, db, users, h):
    req = make_request(db, users["client_a"])
    assert client.get(f"/api/requests/{req.id}/assignments", headers=h["client_a"]).status_code == 200


def test_only_clients_create_requests_and_owner_is_the_caller(client, users, h):
    body = {"task_name": "  Pick   CUP ", "episodes_requested": 5, "deadline": FUTURE, "client_id": users["client_b"].id}
    r = client.post("/api/requests", json=body, headers=h["client_a"])
    assert r.status_code == 201
    assert r.json()["client_id"] == users["client_a"].id  # client_id in the body is ignored
    assert r.json()["task_name"] == "pick cup"
    assert r.json()["status"] == "submitted"
    for role in ("ops", "admin"):
        assert client.post("/api/requests", json=body, headers=h[role]).status_code == 403


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("GET", "/api/episodes", {}),
        ("GET", "/api/episodes/task-names", {}),
        ("POST", "/api/requests/{rid}/assignments", {"json": {"episode_ids": ["X"]}}),
        ("DELETE", "/api/requests/{rid}/assignments/X", {}),
        ("GET", "/api/analytics", {}),
        ("GET", "/api/users", {}),
        ("GET", "/api/events", {}),
    ],
)
def test_client_is_forbidden_from_operator_endpoints(client, db, users, h, method, path, kwargs):
    req = make_request(db, users["client_a"], status=RequestStatus.in_progress)
    r = client.request(method, path.format(rid=req.id), headers=h["client_a"], **kwargs)
    assert r.status_code == 403


def test_client_cannot_import(client, h):
    with SEED_CSV.open("rb") as fh:
        r = client.post("/api/episodes/import", files={"file": ("e.csv", fh, "text/csv")}, headers=h["client_a"])
    assert r.status_code == 403


def test_operator_cannot_manage_users(client, users, h):
    assert client.get("/api/users", headers=h["ops"]).status_code == 403
    body = {"email": "x@test.io", "name": "X", "password": "password123", "role": "admin"}
    assert client.post("/api/users", json=body, headers=h["ops"]).status_code == 403
    r = client.patch(f"/api/users/{users['ops'].id}", json={"role": "admin"}, headers=h["ops"])
    assert r.status_code == 403


def test_admin_can_do_operator_work(client, db, users, h):
    req = make_request(db, users["client_a"], n=1)
    ep = make_episode(db)
    assert client.post(f"/api/requests/{req.id}/transitions", json={"to_status": "in_progress"}, headers=h["admin"]).status_code == 200
    assert client.post(f"/api/requests/{req.id}/assignments", json={"episode_ids": [ep.episode_id]}, headers=h["admin"]).status_code == 200
    assert client.get("/api/episodes", headers=h["admin"]).status_code == 200
    assert client.get("/api/analytics", headers=h["admin"]).status_code == 200


def test_admin_creates_user_who_can_log_in(client, h):
    body = {"email": "New@Test.io", "name": "New", "password": "longenough", "role": "client", "organisation": "Gamma"}
    r = client.post("/api/users", json=body, headers=h["admin"])
    assert r.status_code == 201
    assert r.json()["email"] == "new@test.io"
    assert client.post("/api/users", json=body, headers=h["admin"]).status_code == 409
    login = client.post("/api/auth/login", json={"email": "new@test.io", "password": "longenough"})
    assert login.status_code == 200


def test_role_change_applies_to_existing_tokens(client, users, h):
    assert client.get("/api/episodes", headers=h["client_a"]).status_code == 403
    client.patch(f"/api/users/{users['client_a'].id}", json={"role": "operator"}, headers=h["admin"])
    assert client.get("/api/episodes", headers=h["client_a"]).status_code == 200


def test_admin_cannot_lock_themselves_out(client, users, h):
    admin_id = users["admin"].id
    assert client.patch(f"/api/users/{admin_id}", json={"is_active": False}, headers=h["admin"]).status_code == 422
    assert client.patch(f"/api/users/{admin_id}", json={"role": "client"}, headers=h["admin"]).status_code == 422
