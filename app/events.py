"""Server-Sent Events for the page: the job list on every change, the status every few seconds.

JobStore listeners run under the store lock (possibly in a worker thread), so `notify` only
schedules a wake-up on the event loop; the stream reads the store itself afterwards."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, Optional

log = logging.getLogger(__name__)

EVENT_COALESCE = 0.25   # seconds to gather a burst of changes into one `jobs` event
STATUS_INTERVAL = 5.0   # seconds between `status` events
KEEPALIVE = 15.0        # seconds without any frame before a comment line keeps the connection up


class Subscription:
    """One connected stream: `event` is set when the jobs changed since it was last cleared."""

    def __init__(self) -> None:
        self.event = asyncio.Event()


class Broadcaster:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: set[Subscription] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def notify(self) -> None:
        """Thread-safe; a no-op before bind (or once the loop is closed)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._wake)
        except RuntimeError:   # the loop closed in between
            pass

    def _wake(self) -> None:
        for sub in list(self._subs):
            sub.event.set()

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)


def frame(name: str, payload) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


KEEPALIVE_FRAME = b": keepalive\n\n"


async def stream(request, broadcaster: Broadcaster, jobs_fn: Callable[[], list],
                 status_fn: Callable[[], dict]) -> AsyncIterator[bytes]:
    """`jobs` and `status` on connect, then `jobs` after each (coalesced) change, `status`
    every STATUS_INTERVAL and a keepalive comment after KEEPALIVE of silence, until the
    client disconnects. status_fn runs in a thread (it scans the disk)."""
    sub = broadcaster.subscribe()
    try:
        loop = asyncio.get_running_loop()
        try:
            yield frame("jobs", jobs_fn())
        except Exception:
            log.exception("event stream: jobs_fn failed")
        try:
            yield frame("status", await asyncio.to_thread(status_fn))
        except Exception:
            log.exception("event stream: status_fn failed")
        now = loop.time()
        next_status = now + STATUS_INTERVAL
        last_frame = now
        while not await request.is_disconnected():
            now = loop.time()
            timeout = max(0.0, min(next_status, last_frame + KEEPALIVE) - now)
            try:
                await asyncio.wait_for(sub.event.wait(), timeout=timeout)
                woke = True
            except asyncio.TimeoutError:
                woke = False
            if woke:
                await asyncio.sleep(EVENT_COALESCE)
                sub.event.clear()
                try:
                    payload = jobs_fn()
                except Exception:
                    log.exception("event stream: jobs_fn failed")
                else:
                    yield frame("jobs", payload)
                    last_frame = loop.time()
            now = loop.time()
            if now >= next_status:
                next_status = now + STATUS_INTERVAL   # also after a failure: no retry storm
                try:
                    payload = await asyncio.to_thread(status_fn)
                except Exception:
                    log.exception("event stream: status_fn failed")
                else:
                    yield frame("status", payload)
                    last_frame = loop.time()
            elif not woke and now - last_frame >= KEEPALIVE:
                yield KEEPALIVE_FRAME
                last_frame = now
    finally:
        broadcaster.unsubscribe(sub)
