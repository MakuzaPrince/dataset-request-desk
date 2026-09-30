"""Server-Sent Events feed of request changes for operators (the chosen stretch item)."""
import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.deps import require_ops
from app.events import broadcaster
from app.models import User

router = APIRouter(prefix="/api/events", tags=["events"])

HEARTBEAT_SECONDS = 15


@router.get("")
async def stream(request: Request, _: User = Depends(require_ops)):
    queue = broadcaster.subscribe()

    async def events():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment line keeps proxies from closing the connection
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
