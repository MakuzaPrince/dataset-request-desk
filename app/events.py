"""In-process pub/sub used to push request changes to operators over SSE.

Publishing happens from sync route handlers (which FastAPI runs in a thread pool),
so messages are handed to each subscriber's event loop thread-safely.
"""
import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self, queue_size: int = 100):
        self._queue_size = queue_size
        self._subscribers: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(self._queue_size)
        with self._lock:
            self._subscribers.add((queue, asyncio.get_running_loop()))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = {s for s in self._subscribers if s[0] is not queue}

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:  # loop already closed
                self.unsubscribe(queue)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # A stalled client must not block everyone else; it can refetch on reconnect.
            log.warning("dropping event for slow SSE subscriber")


broadcaster = Broadcaster()
