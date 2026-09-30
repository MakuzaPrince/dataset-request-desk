"""Live updates: the broadcaster delivers events published from worker threads."""
import asyncio
import threading
from datetime import date, timedelta

from app.events import Broadcaster, broadcaster
from app.routers.stream import stream as stream_endpoint


def test_broadcaster_delivers_across_threads():
    async def scenario():
        b = Broadcaster()
        q = b.subscribe()
        threading.Thread(target=b.publish, args=({"type": "x", "request_id": 1},)).start()
        event = await asyncio.wait_for(q.get(), timeout=2)
        b.unsubscribe(q)
        assert b.subscriber_count == 0
        return event

    assert asyncio.run(scenario()) == {"type": "x", "request_id": 1}


def test_full_queue_drops_instead_of_blocking():
    async def scenario():
        b = Broadcaster(queue_size=1)
        q = b.subscribe()
        b.publish({"n": 1})
        b.publish({"n": 2})
        await asyncio.sleep(0.05)
        return q.qsize(), q.get_nowait()

    assert asyncio.run(scenario()) == (1, {"n": 1})


def test_request_changes_are_published(client, h, monkeypatch):
    seen = []
    monkeypatch.setattr(broadcaster, "publish", seen.append)
    body = {"task_name": "pick cup", "episodes_requested": 1, "deadline": (date.today() + timedelta(days=5)).isoformat()}
    rid = client.post("/api/requests", json=body, headers=h["client_a"]).json()["id"]
    client.post(f"/api/requests/{rid}/transitions", json={"to_status": "in_progress"}, headers=h["ops"])
    assert seen == [
        {"type": "request_created", "request_id": rid, "status": "submitted"},
        {"type": "request_status_changed", "request_id": rid, "status": "in_progress"},
    ]



def test_stream_releases_db_session_and_unsubscribes_on_disconnect():
    """An SSE connection can stay open for hours, so it must not pin a pooled DB connection."""

    class FakeSession:
        closed = False

        def close(self):
            self.closed = True

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    async def scenario():
        db = FakeSession()
        response = await stream_endpoint(DisconnectedRequest(), object(), db)
        assert db.closed
        assert broadcaster.subscriber_count == 1
        chunks = [chunk async for chunk in response.body_iterator]
        return chunks

    assert asyncio.run(scenario()) == ["retry: 3000\n\n"]
    assert broadcaster.subscriber_count == 0
