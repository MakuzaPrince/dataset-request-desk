import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Assignment, Quality, RequestStatus
from tests.conftest import make_episode, make_request

S = RequestStatus


def assign(client, rid, ids, headers):
    return client.post(f"/api/requests/{rid}/assignments", json={"episode_ids": ids}, headers=headers)


@pytest.fixture
def open_request(db, users):
    return make_request(db, users["client_a"], status=S.in_progress, n=3)


def test_assigns_good_and_usable_episodes(client, db, h, open_request):
    good, usable = make_episode(db), make_episode(db, quality=Quality.usable)
    r = assign(client, open_request.id, [good.episode_id, usable.episode_id.lower()], h["ops"])
    assert r.status_code == 200
    assert r.json()["assigned"] == [good.episode_id, usable.episode_id]
    assert r.json()["assigned_count"] == 2


def test_bad_episodes_cannot_be_assigned_and_batch_is_atomic(client, db, h, open_request):
    good, bad = make_episode(db), make_episode(db, quality=Quality.bad)
    r = assign(client, open_request.id, [good.episode_id, bad.episode_id], h["ops"])
    assert r.status_code == 422
    assert r.json()["episode_ids"] == [bad.episode_id]
    assert db.query(Assignment).count() == 0  # the good one was not assigned either


def test_episode_can_only_belong_to_one_request(client, db, users, h, open_request):
    other = make_request(db, users["client_b"], status=S.in_progress)
    ep = make_episode(db)
    assert assign(client, open_request.id, [ep.episode_id], h["ops"]).status_code == 200
    r = assign(client, other.id, [ep.episode_id], h["ops"])
    assert r.status_code == 409
    assert r.json()["episode_ids"] == [ep.episode_id]


def test_reassigning_to_the_same_request_is_a_no_op(client, db, h, open_request):
    ep = make_episode(db)
    assign(client, open_request.id, [ep.episode_id], h["ops"])
    r = assign(client, open_request.id, [ep.episode_id, ep.episode_id], h["ops"])
    assert r.status_code == 200
    assert r.json() == {"request_id": open_request.id, "assigned": [], "already_assigned": [ep.episode_id], "assigned_count": 1}


def test_unknown_episode_is_404(client, db, h, open_request):
    r = assign(client, open_request.id, ["NOPE-1"], h["ops"])
    assert r.status_code == 404
    assert r.json()["episode_ids"] == ["NOPE-1"]


@pytest.mark.parametrize("status", [S.submitted, S.delivered, S.accepted, S.rejected])
def test_assignments_are_frozen_outside_in_progress(client, db, users, h, status):
    req = make_request(db, users["client_a"], status=status)
    ep = make_episode(db)
    assert assign(client, req.id, [ep.episode_id], h["ops"]).status_code == 409
    assert client.delete(f"/api/requests/{req.id}/assignments/{ep.episode_id}", headers=h["ops"]).status_code == 409


def test_unassign_frees_the_episode_for_another_request(client, db, users, h, open_request):
    other = make_request(db, users["client_b"], status=S.in_progress)
    ep = make_episode(db)
    assign(client, open_request.id, [ep.episode_id], h["ops"])
    r = client.delete(f"/api/requests/{open_request.id}/assignments/{ep.episode_id}", headers=h["ops"])
    assert r.status_code == 204
    assert client.delete(f"/api/requests/{open_request.id}/assignments/{ep.episode_id}", headers=h["ops"]).status_code == 404
    assert assign(client, other.id, [ep.episode_id], h["ops"]).status_code == 200


def test_database_enforces_one_request_per_episode(db, users):
    """The API check can race; the unique constraint is the guarantee."""
    a = make_request(db, users["client_a"], status=S.in_progress)
    b = make_request(db, users["client_b"], status=S.in_progress)
    ep = make_episode(db)
    db.add(Assignment(request_id=a.id, episode_id=ep.id, assigned_by_id=users["ops"].id))
    db.commit()
    db.add(Assignment(request_id=b.id, episode_id=ep.id, assigned_by_id=users["ops"].id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_episode_list_filters(client, db, users, h, open_request):
    make_episode(db, task="pick cup", quality=Quality.good)
    make_episode(db, task="pick cup", quality=Quality.bad)
    taken = make_episode(db, task="pick cup", quality=Quality.good)
    make_episode(db, task="fold towel", quality=Quality.good)
    assign(client, open_request.id, [taken.episode_id], h["ops"])

    def ids(q):
        return {e["episode_id"] for e in client.get(f"/api/episodes?{q}", headers=h["ops"]).json()["items"]}

    assert len(ids("task_name=Pick%20Cup")) == 3
    assert len(ids("task_name=pick%20cup&quality=good")) == 2
    assert ids("task_name=pick%20cup&quality=good&unassigned=true") != ids("task_name=pick%20cup&quality=good")
    assert taken.episode_id not in ids("unassigned=true")
    listed = client.get("/api/episodes?quality=good&task_name=pick%20cup", headers=h["ops"]).json()["items"]
    assert {e["assigned_request_id"] for e in listed} == {None, open_request.id}
    assert client.get("/api/episodes?quality=excellent", headers=h["ops"]).status_code == 422
