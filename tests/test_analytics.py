from datetime import datetime, timedelta

from app.models import Quality, RequestStatus, StatusEvent
from tests.conftest import make_episode, make_request

S = RequestStatus


def get(client, h, start="2026-08-01", end="2026-08-31"):
    r = client.get(f"/api/analytics?start={start}&end={end}", headers=h["ops"])
    assert r.status_code == 200, r.text
    return r.json()


def deliver_after(db, req, actor, hours: float, extra_deliveries: int = 0):
    t = req.created_at + timedelta(hours=hours)
    db.add(StatusEvent(request_id=req.id, from_status=S.in_progress, to_status=S.delivered, actor_id=actor.id, created_at=t))
    # Later re-deliveries (after rework) must not affect "time to delivery".
    for i in range(extra_deliveries):
        db.add(StatusEvent(request_id=req.id, from_status=S.in_progress, to_status=S.delivered,
                           actor_id=actor.id, created_at=t + timedelta(days=10 + i)))
    req.status = S.delivered
    db.commit()


def test_episodes_per_day_per_robot(client, db, h):
    make_episode(db, robot="arm-01", recorded_at=datetime(2026, 8, 1, 0, 0))
    make_episode(db, robot="arm-01", recorded_at=datetime(2026, 8, 1, 23, 59, 59))
    make_episode(db, robot="arm-02", recorded_at=datetime(2026, 8, 1, 12, 0), quality=Quality.bad)
    make_episode(db, robot="arm-01", recorded_at=datetime(2026, 8, 31, 23, 0))
    make_episode(db, robot="arm-01", recorded_at=datetime(2026, 9, 1, 0, 0))   # outside range
    make_episode(db, robot="arm-01", recorded_at=datetime(2026, 7, 31, 23, 59))  # outside range
    assert get(client, h)["episodes_per_day_per_robot"] == [
        {"day": "2026-08-01", "robot_id": "arm-01", "episodes": 2},
        {"day": "2026-08-01", "robot_id": "arm-02", "episodes": 1},
        {"day": "2026-08-31", "robot_id": "arm-01", "episodes": 1},
    ]


def test_top_five_tasks_count_only_good_episodes(client, db, h):
    counts = {"a": 6, "b": 5, "c": 4, "d": 3, "e": 2, "f": 1}
    for task, n in counts.items():
        for _ in range(n):
            make_episode(db, task=task)
    for _ in range(10):
        make_episode(db, task="f", quality=Quality.usable)
        make_episode(db, task="f", quality=Quality.bad)
    make_episode(db, task="f", recorded_at=datetime(2025, 1, 1))  # outside range
    top = get(client, h)["top_tasks_by_good_episodes"]
    assert top == [{"task_name": t, "good_episodes": counts[t]} for t in "abcde"]


def test_fulfilment_counts_and_median_odd(client, db, users, h):
    created = datetime(2026, 8, 10, 9, 0)
    reqs = [make_request(db, users["client_a"], created_at=created) for _ in range(4)]
    deliver_after(db, reqs[0], users["ops"], hours=1)
    deliver_after(db, reqs[1], users["ops"], hours=3, extra_deliveries=2)
    deliver_after(db, reqs[2], users["ops"], hours=10)
    make_request(db, users["client_a"], created_at=datetime(2026, 9, 5))  # submitted outside range
    f = get(client, h)["request_fulfilment"]
    assert f["by_status"] == {"submitted": 1, "in_progress": 0, "delivered": 3, "accepted": 0, "rejected": 0}
    assert f["total"] == 4
    assert f["delivered_count"] == 3
    assert f["median_seconds_to_delivery"] == 3 * 3600


def test_fulfilment_median_even_and_empty(client, db, users, h):
    assert get(client, h)["request_fulfilment"]["median_seconds_to_delivery"] is None
    created = datetime(2026, 8, 10, 9, 0)
    for hours in (2, 4):
        deliver_after(db, make_request(db, users["client_a"], created_at=created), users["ops"], hours=hours)
    assert get(client, h)["request_fulfilment"]["median_seconds_to_delivery"] == 3 * 3600


def test_invalid_ranges(client, h):
    assert client.get("/api/analytics?start=2026-08-10&end=2026-08-01", headers=h["ops"]).status_code == 422
    assert client.get("/api/analytics?start=2020-01-01&end=2026-08-01", headers=h["ops"]).status_code == 422
    assert client.get("/api/analytics", headers=h["ops"]).status_code == 200
