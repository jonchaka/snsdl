import asyncio
import json
import threading

import pytest

from app import events


class FakeRequest:
    """is_disconnected() answers False `alive` times, then True."""

    def __init__(self, alive: int = 1000):
        self.alive = alive
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.alive


def _parse(chunk: bytes):
    text = chunk.decode()
    if text.startswith(":"):
        return ("comment", text)
    name, data = text.split("\n")[:2]
    return (name.removeprefix("event: "), json.loads(data.removeprefix("data: ")))


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(events, "EVENT_COALESCE", 0.01)
    monkeypatch.setattr(events, "STATUS_INTERVAL", 60.0)
    monkeypatch.setattr(events, "KEEPALIVE", 60.0)


def test_constants():
    assert events.EVENT_COALESCE == 0.25 and events.STATUS_INTERVAL == 5.0 and events.KEEPALIVE == 15.0


def test_frame_is_one_line_json():
    assert events.frame("jobs", [{"a": 1, "b": "x y"}]) == b'event: jobs\ndata: [{"a":1,"b":"x y"}]\n\n'


def test_notify_before_bind_is_a_no_op():
    b = events.Broadcaster()
    b.notify()   # nothing bound: must not raise


def test_stream_sends_jobs_and_status_first_then_stops_on_disconnect(fast):
    b = events.Broadcaster()
    status_threads = []

    def status_fn():
        status_threads.append(threading.current_thread())
        return {"ok": True}

    async def run():
        b.bind(asyncio.get_running_loop())
        return [c async for c in events.stream(FakeRequest(alive=0), b, lambda: [{"id": "j1"}], status_fn)]

    chunks = asyncio.run(run())
    assert [_parse(c) for c in chunks] == [("jobs", [{"id": "j1"}]), ("status", {"ok": True})]
    assert status_threads and status_threads[0] is not threading.main_thread()
    assert b._subs == set()   # unsubscribed on the way out


def test_a_burst_of_notifies_gives_one_coalesced_jobs_event(fast, monkeypatch):
    monkeypatch.setattr(events, "EVENT_COALESCE", 0.05)
    b = events.Broadcaster()
    jobs = [[{"id": "a"}]]

    async def run():
        b.bind(asyncio.get_running_loop())
        req = FakeRequest()
        gen = events.stream(req, b, lambda: jobs[0], lambda: {})
        out = [await gen.__anext__(), await gen.__anext__()]
        jobs[0] = [{"id": "b"}]
        # notify from another thread, several times, as JobStore saves would
        t = threading.Thread(target=lambda: [b.notify() for _ in range(5)])
        t.start()
        t.join()
        out.append(await asyncio.wait_for(gen.__anext__(), timeout=5))
        req.alive = 0   # disconnect: the generator ends at the next check
        rest = [c async for c in gen]
        return out, rest

    out, rest = asyncio.run(run())
    assert _parse(out[2]) == ("jobs", [{"id": "b"}])
    assert rest == []   # the five notifies made a single event


def test_keepalive_after_silence(fast, monkeypatch):
    monkeypatch.setattr(events, "KEEPALIVE", 0.05)
    b = events.Broadcaster()

    async def run():
        b.bind(asyncio.get_running_loop())
        gen = events.stream(FakeRequest(), b, lambda: [], lambda: {})
        await gen.__anext__(), await gen.__anext__()
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=5)
        await gen.aclose()
        return chunk

    assert asyncio.run(run()) == b": keepalive\n\n"


def test_status_repeats_every_interval(fast, monkeypatch):
    monkeypatch.setattr(events, "STATUS_INTERVAL", 0.05)
    b = events.Broadcaster()
    n = iter(range(100))

    async def run():
        b.bind(asyncio.get_running_loop())
        gen = events.stream(FakeRequest(), b, lambda: [], lambda: {"n": next(n)})
        chunks = [await asyncio.wait_for(gen.__anext__(), timeout=5) for _ in range(4)]
        await gen.aclose()
        return chunks

    assert [_parse(c) for c in asyncio.run(run())][1:] == [("status", {"n": 0}), ("status", {"n": 1}),
                                                           ("status", {"n": 2})]


def test_notify_after_the_loop_closed_is_harmless():
    b = events.Broadcaster()
    loop = asyncio.new_event_loop()
    b.bind(loop)
    loop.close()
    b.notify()


def test_create_app_wires_the_broadcaster_to_the_store(settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(settings, run_background=False)
    assert isinstance(app.state.events, events.Broadcaster)
    with TestClient(app):
        assert app.state.events._loop is not None
        sub = app.state.events.subscribe()
        app.state.store.add("https://youtu.be/x")   # a save notifies the listener

        async def woke():
            await asyncio.wait_for(sub.event.wait(), timeout=5)
            return True

        # wait on the app's own loop (the TestClient portal), where the wake-up was scheduled
        fut = asyncio.run_coroutine_threadsafe(woke(), app.state.events._loop)
        assert fut.result(timeout=5) is True
        app.state.events.unsubscribe(sub)
