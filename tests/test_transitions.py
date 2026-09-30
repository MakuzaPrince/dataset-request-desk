import pytest

from app.models import Quality, RequestStatus, StatusEvent
from tests.conftest import assign_directly, make_episode, make_request

S = RequestStatus


def move(client, rid, to, headers, note=None):
    return client.post(f"/api/requests/{rid}/transitions", json={"to_status": to, "note": note}, headers=headers)


def test_full_lifecycle_with_rework_is_audited(client, db, users, h):
    req = make_request(db, users["client_a"], n=2)
    eps = [make_episode(db), make_episode(db, quality=Quality.usable)]

    assert move(client, req.id, "in_progress", h["ops"]).status_code == 200
    assign_directly(db, req, eps, users["ops"])
    assert move(client, req.id, "delivered", h["ops"]).status_code == 200
    assert move(client, req.id, "rejected", h["client_a"], note="blurry").status_code == 200
    assert move(client, req.id, "in_progress", h["ops"]).status_code == 200
    assert move(client, req.id, "delivered", h["admin"]).status_code == 200
    r = move(client, req.id, "accepted", h["client_a"])
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"

    events = r.json()["events"]
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        (None, "submitted"),
        ("submitted", "in_progress"),
        ("in_progress", "delivered"),
        ("delivered", "rejected"),
        ("rejected", "in_progress"),
        ("in_progress", "delivered"),
        ("delivered", "accepted"),
    ]
    actors = [e["actor_id"] for e in events]
    assert actors == [users[k].id for k in ("client_a", "ops", "ops", "client_a", "ops", "admin", "client_a")]
    assert events[3]["note"] == "blurry"
    assert all(e["created_at"] for e in events)


@pytest.mark.parametrize(
    "start,to",
    [
        (S.submitted, "delivered"),
        (S.submitted, "accepted"),
        (S.submitted, "rejected"),
        (S.in_progress, "submitted"),
        (S.in_progress, "accepted"),
        (S.delivered, "in_progress"),
        (S.delivered, "submitted"),
        (S.accepted, "in_progress"),
        (S.accepted, "rejected"),
        (S.rejected, "delivered"),
        (S.rejected, "accepted"),
        (S.in_progress, "in_progress"),
    ],
)
def test_invalid_transitions_are_rejected(client, db, users, h, start, to):
    req = make_request(db, users["client_a"], status=start, n=1)
    headers = h["client_a"] if to in ("accepted", "rejected") else h["ops"]
    r = move(client, req.id, to, headers, note="x")
    assert r.status_code == 409
    db.refresh(req)
    assert req.status == start
    assert db.query(StatusEvent).filter_by(request_id=req.id).count() == 1  # only the creation event


@pytest.mark.parametrize(
    "start,to,actor",
    [
        (S.submitted, "in_progress", "client_a"),
        (S.in_progress, "delivered", "client_a"),
        (S.rejected, "in_progress", "client_a"),
        (S.delivered, "accepted", "ops"),
        (S.delivered, "rejected", "ops"),
        (S.delivered, "accepted", "admin"),
    ],
)
def test_steps_can_only_be_taken_by_their_owning_role(client, db, users, h, start, to, actor):
    req = make_request(db, users["client_a"], status=start, n=1)
    assert move(client, req.id, to, h[actor], note="x").status_code == 403
    db.refresh(req)
    assert req.status == start


def test_cannot_deliver_until_enough_episodes_assigned(client, db, users, h):
    req = make_request(db, users["client_a"], status=S.in_progress, n=3)
    assign_directly(db, req, [make_episode(db), make_episode(db)], users["ops"])
    r = move(client, req.id, "delivered", h["ops"])
    assert r.status_code == 409
    assert r.json()["assigned_count"] == 2
    assign_directly(db, req, [make_episode(db)], users["ops"])
    assert move(client, req.id, "delivered", h["ops"]).status_code == 200


def test_rejection_requires_a_reason(client, db, users, h):
    req = make_request(db, users["client_a"], status=S.delivered)
    assert move(client, req.id, "rejected", h["client_a"]).status_code == 422
    assert move(client, req.id, "rejected", h["client_a"], note="   ").status_code == 422
    db.refresh(req)
    assert req.status == S.delivered


def test_allowed_transitions_reflect_caller(client, db, users, h):
    req = make_request(db, users["client_a"], status=S.delivered)
    assert client.get(f"/api/requests/{req.id}", headers=h["client_a"]).json()["allowed_transitions"] == ["accepted", "rejected"]
    assert client.get(f"/api/requests/{req.id}", headers=h["ops"]).json()["allowed_transitions"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"task_name": "", "episodes_requested": 1, "deadline": "2099-01-01"},
        {"task_name": "x", "episodes_requested": 0, "deadline": "2099-01-01"},
        {"task_name": "x", "episodes_requested": -3, "deadline": "2099-01-01"},
        {"task_name": "x", "episodes_requested": 1, "deadline": "2000-01-01"},
        {"task_name": "x", "episodes_requested": 1, "deadline": "not-a-date"},
        {"task_name": "x", "episodes_requested": 1},
    ],
)
def test_request_input_is_validated(client, h, body):
    assert client.post("/api/requests", json=body, headers=h["client_a"]).status_code == 422
