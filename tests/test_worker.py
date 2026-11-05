def _controlled_sleep(monkeypatch, long: float = 100.0):
    """Patch asyncio.sleep so any sleep of at least `long` seconds (a retry timer or site gap
    the test set up) waits for the returned event instead of the wall clock; shorter sleeps
    (the test's own polling) stay real. `entered` is set once such a sleep has started."""
    real_sleep = asyncio.sleep
    release, entered = asyncio.Event(), asyncio.Event()

    async def fake_sleep(delay, *args, **kwargs):
        if delay >= long:
            entered.set()
            await release.wait()
            return args[0] if args else None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(worker_mod.asyncio, "sleep", fake_sleep)
    return release, entered


def test_schedule_retry_enqueues_after_the_delay(settings, store, monkeypatch):
    ran = []

    async def scenario():
        release, entered = _controlled_sleep(monkeypatch)
        pool = WorkerPool(store, settings,
                          download_fn=lambda j, u, st, se: (ran.append(j), st.update(j, status=DONE)))
        await pool.start()
        job = store.add("https://example.com/a")
        store.update(job.id, retry_at="2026-11-05T10:00:30+00:00")
        pool.schedule_retry(job.id, 500)
        assert len(pool._timers) == 1
        await asyncio.wait_for(entered.wait(), timeout=5)   # the timer is sleeping
        assert ran == [] and pool.queue.qsize() == 0
        release.set()                                      # the delay is over
        await _until(lambda: store.get(job.id).status == DONE)
        await _until(lambda: not pool._timers)   # finished timers are forgotten
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert ran == [job.id]


import asyncio
import json
import collections
import threading

import pytest

from app import webhook
from app import worker as worker_mod
from app.jobs import DONE, DOWNLOADING, FAILED, PAUSED, QUEUED
from app.worker import JobQueue, WorkerPool, disk_low


@pytest.fixture(autouse=True)
def _no_site_gap(settings):
    """Most tests use several example.com jobs; the default 3 s per-site gap would only make
    them slow. Pacing tests set site_gap explicitly."""
    settings.site_gap = 0


def test_worker_processes_queued_jobs(settings, store):
    seen: list[tuple[str, str]] = []

    def fake_download(job_id, url, st, se):
        seen.append((job_id, url))
        assert st.get(job_id).status == DOWNLOADING
        st.update(job_id, status=DONE, progress=100, filename=f"{job_id}.mp4")

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        await pool.start()
        a = store.add("https://example.com/a")
        b = store.add("https://example.com/b")
        pool.enqueue(a.id)
        pool.enqueue(b.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a, b

    a, b = asyncio.run(scenario())
    assert sorted(seen) == sorted([(a.id, a.url), (b.id, b.url)])
    assert store.get(a.id).status == DONE
    assert store.get(b.id).status == DONE


def test_worker_runs_max_concurrent_downloads(settings, store):
    settings.max_concurrent = 2
    settings.max_per_site = 0   # both jobs are example.com; per-site pacing is tested below
    gate = threading.Barrier(2, timeout=5)

    def blocking_download(job_id, url, st, se):
        gate.wait()  # both workers must be inside at once or this times out
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=blocking_download)
        await pool.start()
        for i in range(2):
            pool.enqueue(store.add(f"https://example.com/{i}").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert all(j.status == DONE for j in store.list())


def test_worker_skips_job_missing_from_store(settings, store):
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append(job_id)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        await pool.start()
        pool.enqueue("doesnotexist")
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert calls == []


def test_worker_survives_download_fn_exception(settings, store):
    def exploding(job_id, url, st, se):
        st.update(job_id, speed=999, eta=9)   # as if the hook had already written a rate
        raise RuntimeError("boom")

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=exploding)
        await pool.start()
        job = store.add("https://example.com/a")
        pool.enqueue(job.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert all(not t.done() for t in pool._tasks)  # workers still alive
        await pool.stop()
        return job.id

    job_id = asyncio.run(scenario())
    job = store.get(job_id)
    assert job.status == FAILED
    assert job.error == "worker error: boom"
    assert job.speed is None and job.eta is None


def test_worker_skips_paused_job_and_runs_duplicate_id_once(settings, store):
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append(job_id)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        paused = store.add("https://example.com/p")
        store.update(paused.id, status=PAUSED)
        twice = store.add("https://example.com/t")
        pool.enqueue(paused.id)
        pool.enqueue(twice.id)
        pool.enqueue(twice.id)
        await pool.start()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return paused, twice

    paused, twice = asyncio.run(scenario())
    assert calls == [twice.id]
    assert store.get(paused.id).status == PAUSED
    assert store.get(twice.id).status == DONE


def test_gate_holds_new_jobs_until_resumed(settings, store):
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append(job_id)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        assert pool.paused is False
        pool.pause()
        assert pool.paused is True
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.2)
        assert calls == [] and store.get(a.id).status == "queued"
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a

    a = asyncio.run(scenario())
    assert calls == [a.id]
    assert store.get(a.id).status == DONE


def test_pause_lets_in_flight_download_finish(settings, store):
    release = threading.Event()
    started = threading.Event()

    def blocking_download(job_id, url, st, se):
        started.set()
        release.wait(5)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=blocking_download)
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        pool.pause()
        release.set()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert store.get(a.id).status == DONE
        b = store.add("https://example.com/b")
        pool.enqueue(b.id)
        await asyncio.sleep(0.2)
        assert store.get(b.id).status == "queued"
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return b

    b = asyncio.run(scenario())
    assert store.get(b.id).status == DONE


def test_job_removed_while_held_at_gate_is_skipped(settings, store):
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append(job_id)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        pool.pause()
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.1)
        store.remove(a.id)
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert calls == []


def test_stop_while_paused_cancels_workers_blocked_at_gate(settings, store):
    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)
        pool.pause()
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.1)
        await asyncio.wait_for(pool.stop(), timeout=5)   # must not hang on gate.wait()
        return a

    a = asyncio.run(scenario())
    assert store.get(a.id).status == "queued"   # untouched; restart recovery re-enqueues it


def test_hold_keeps_dequeued_jobs_from_starting_until_unhold(settings, store):
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append(job_id)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        assert pool.held is False
        pool.hold()
        assert pool.held is True and pool.paused is False   # independent of the user's pause
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.2)
        assert calls == []
        pool.unhold()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a

    a = asyncio.run(scenario())
    assert calls == [a.id]


def test_user_pause_does_not_mask_drain_hold(settings, store):
    """Order matters: gate (user pause) then drain (restart hold). Resuming the user pause while a
    restart hold is active must still keep jobs from starting."""
    calls = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: calls.append(a[0]))
        pool.pause(); pool.hold()
        await pool.start()
        pool.enqueue(store.add("https://example.com/a").id)
        pool.resume()
        await asyncio.sleep(0.2)
        assert calls == []
        pool.unhold()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert len(calls) == 1


# -- round 6: JobQueue -------------------------------------------------------------


def test_job_queue_is_fifo_and_put_front_jumps_ahead():
    async def scenario():
        q = JobQueue()
        assert q.empty() and q.qsize() == 0
        q.put_nowait("a"); q.put_nowait("b"); q.put_front("c")
        assert q.qsize() == 3 and not q.empty()
        return [await q.get() for _ in range(3)]

    assert asyncio.run(scenario()) == ["c", "a", "b"]


def test_job_queue_get_blocks_until_put_and_put_front_wakes_it():
    async def scenario():
        q = JobQueue()
        getter = asyncio.create_task(q.get())
        await asyncio.sleep(0.05)
        assert not getter.done()
        q.put_front("x")
        return await asyncio.wait_for(getter, timeout=1)

    assert asyncio.run(scenario()) == "x"


def test_job_queue_join_waits_for_task_done():
    async def scenario():
        q = JobQueue()
        await asyncio.wait_for(q.join(), timeout=1)   # nothing pending: returns at once
        q.put_nowait("a"); q.put_front("b")
        joiner = asyncio.create_task(q.join())
        await q.get(); q.task_done()
        await asyncio.sleep(0.02)
        assert not joiner.done()
        await q.get()
        await asyncio.sleep(0.02)
        assert not joiner.done()   # got but not task_done yet
        q.task_done()
        await asyncio.wait_for(joiner, timeout=1)
        with pytest.raises(ValueError):
            q.task_done()

    asyncio.run(scenario())


def test_job_queue_cancelled_getter_does_not_lose_items_or_counts():
    async def scenario():
        q = JobQueue()
        g1 = asyncio.create_task(q.get())
        g2 = asyncio.create_task(q.get())
        await asyncio.sleep(0.01)
        g1.cancel()                      # cancelled while blocked
        await asyncio.gather(g1, return_exceptions=True)
        g3 = asyncio.create_task(q.get())
        await asyncio.sleep(0.01)
        q.put_nowait("a")                # wakes g2 ...
        g2.cancel()                      # ... which is cancelled before it runs: g3 must get "a"
        await asyncio.gather(g2, return_exceptions=True)
        got = await asyncio.wait_for(g3, timeout=1)
        assert q.qsize() == 0
        q.task_done()
        await asyncio.wait_for(q.join(), timeout=1)
        return got

    assert asyncio.run(scenario()) == "a"


def test_enqueue_front_runs_before_earlier_queued_jobs(settings, store):
    settings.max_concurrent = 1
    order = []

    def fake_download(job_id, url, st, se):
        order.append(job_id)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        assert isinstance(pool.queue, JobQueue)
        pool.pause()
        await pool.start()
        a, b, c, d = (store.add(f"https://example.com/{n}") for n in "abcd")
        pool.enqueue(a.id)
        await asyncio.sleep(0.05)        # the single worker holds a at the gate
        pool.enqueue(b.id); pool.enqueue(c.id)
        pool.enqueue_front(d.id)
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return [a.id, d.id, b.id, c.id]

    expected = asyncio.run(scenario())
    assert order == expected


# -- round 6: priority cleared on start ----------------------------------------------

def test_worker_clears_priority_when_starting(settings, store):
    seen = []

    def fake_download(job_id, url, st, se):
        j = st.get(job_id)
        seen.append((j.status, j.priority))
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        await pool.start()
        job = store.add("https://example.com/a")
        store.update(job.id, priority=True)
        pool.enqueue_front(job.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert seen == [(DOWNLOADING, False)]
    assert store.get(job.id).priority is False


# -- round 6: free-space guard -------------------------------------------------------

_Usage = collections.namedtuple("_Usage", "total used free")


def test_disk_low_compares_free_space_with_minimum(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(worker_mod.shutil, "disk_usage",
                        lambda p: (seen.append(p), _Usage(100, 90, 10))[1])
    settings.min_free_space = 11
    assert disk_low(settings) is True
    settings.min_free_space = 10
    assert disk_low(settings) is False
    assert seen == [settings.downloads_dir, settings.downloads_dir]


def test_disk_low_disabled_by_zero_never_checks(settings, monkeypatch):
    def boom(p):
        raise AssertionError("disk_usage must not be called")
    monkeypatch.setattr(worker_mod.shutil, "disk_usage", boom)
    settings.min_free_space = 0
    assert disk_low(settings) is False


def test_disk_check_interval_default():
    assert worker_mod.DISK_CHECK_INTERVAL == 10


def _low_disk(monkeypatch, low: list[bool]):
    monkeypatch.setattr(worker_mod, "DISK_CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: low[0])


def test_worker_waits_while_disk_low_then_proceeds(settings, store, monkeypatch):
    low = [True]
    _low_disk(monkeypatch, low)
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append(job_id)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fake_download)
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.1)
        assert calls == [] and store.get(a.id).status == QUEUED
        low[0] = False
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a

    a = asyncio.run(scenario())
    assert calls == [a.id] and store.get(a.id).status == DONE


@pytest.mark.parametrize("change", ["remove", "pause"])
def test_job_removed_or_paused_during_disk_wait_is_skipped(settings, store, monkeypatch, change):
    low = [True]
    _low_disk(monkeypatch, low)
    calls = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: calls.append(a[0]))
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.05)
        if change == "remove":
            store.remove(a.id)
        else:
            store.update(a.id, status=PAUSED)
        low[0] = False
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert calls == []


def test_queue_pause_during_disk_wait_still_holds_the_job(settings, store, monkeypatch):
    low = [True]
    _low_disk(monkeypatch, low)
    calls = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: (calls.append(a[0]), a[2].update(a[0], status=DONE)))
        await pool.start()
        pool.enqueue(store.add("https://example.com/a").id)
        await asyncio.sleep(0.05)
        pool.pause()
        low[0] = False
        await asyncio.sleep(0.1)
        assert calls == []
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert len(calls) == 1


def test_stop_cancels_worker_waiting_for_disk(settings, store, monkeypatch):
    _low_disk(monkeypatch, [True])

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)
        await pool.start()
        a = store.add("https://example.com/a")
        pool.enqueue(a.id)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(pool.stop(), timeout=5)
        return a

    a = asyncio.run(scenario())
    assert store.get(a.id).status == QUEUED


# -- round 7: per-site pacing ----------------------------------------------------------

def _blocking_downloads(block: set[str]):
    """A download_fn that finishes immediately except for URLs in block, which wait for the
    returned per-URL event. Records (event, url) start/end order in `log`."""
    log: list[tuple[str, str]] = []
    events = {url: threading.Event() for url in block}

    def fn(job_id, url, st, se):
        log.append(("start", url))
        if url in events:
            events[url].wait(5)
        log.append(("end", url))
        st.update(job_id, status=DONE)

    return fn, events, log


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def test_same_site_jobs_run_one_at_a_time_while_other_sites_run(settings, store):
    settings.max_concurrent, settings.max_per_site = 2, 1
    a1, a2, b1 = "https://a.com/1", "https://www.a.com/2", "https://b.com/1"
    fn, events, log = _blocking_downloads({a1})

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        ja1, ja2, jb1 = (store.add(u) for u in (a1, a2, b1))
        for j in (ja1, ja2, jb1):
            pool.enqueue(j.id)
        await _until(lambda: store.get(jb1.id).status == DONE)
        # b.com ran in parallel; the second a.com job is parked, still queued
        assert store.get(ja1.id).status == DOWNLOADING
        assert store.get(ja2.id).status == QUEUED
        assert list(pool._deferred["a.com"]) == [ja2.id]
        assert pool._running == {"a.com": 1, "b.com": 0}
        events[a1].set()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return ja2

    ja2 = asyncio.run(scenario())
    assert store.get(ja2.id).status == DONE
    assert log.index(("end", a1)) < log.index(("start", a2))


def test_deferred_ids_return_to_queue_front_in_original_order(settings, store):
    settings.max_concurrent, settings.max_per_site = 2, 1
    fn, events, log = _blocking_downloads({"https://a.com/1"})

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        a1, a2, a3 = (store.add(f"https://a.com/{i}") for i in (1, 2, 3))
        for j in (a1, a2, a3):
            pool.enqueue(j.id)
        await _until(lambda: len(pool._deferred.get("a.com", ())) == 2)
        assert list(pool._deferred["a.com"]) == [a2.id, a3.id]
        pool.pause()
        x, y = store.add("https://x.com/1"), store.add("https://y.com/1")
        pool.enqueue(x.id)   # the free worker takes x and holds it at the gate
        await asyncio.sleep(0.05)
        pool.enqueue(y.id)
        events["https://a.com/1"].set()
        await _until(lambda: store.get(a1.id).status == DONE)
        # a2, a3 went back ahead of y; the worker freed by a1 has already taken a2 to the gate
        await _until(lambda: pool.queue.qsize() == 2)
        assert list(pool.queue._items) == [a3.id, y.id]
        assert "a.com" not in pool._deferred or not pool._deferred["a.com"]
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a2, a3

    a2, a3 = asyncio.run(scenario())
    starts = [u for ev, u in log if ev == "start" and "a.com" in u]
    assert starts == ["https://a.com/1", "https://a.com/2", "https://a.com/3"]


def test_max_per_site_zero_is_unlimited(settings, store):
    settings.max_concurrent, settings.max_per_site = 2, 0
    gate = threading.Barrier(2, timeout=5)

    def fn(job_id, url, st, se):
        gate.wait()   # both same-site jobs must be inside at once
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        for i in range(2):
            pool.enqueue(store.add(f"https://a.com/{i}").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert all(j.status == DONE for j in store.list())


def test_failed_or_cancelled_download_also_releases_deferred(settings, store):
    """A cancel makes the download return early (job failed/cancelled); an unexpected exception
    goes through the worker error path. Either way the parked same-site job must run."""
    settings.max_concurrent, settings.max_per_site = 2, 1
    release = threading.Event()
    ran = []

    def fn(job_id, url, st, se):
        ran.append(url)
        if url.endswith("/1"):
            release.wait(5)
            raise RuntimeError("cancelled mid-download")
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        a1, a2 = store.add("https://a.com/1"), store.add("https://a.com/2")
        pool.enqueue(a1.id); pool.enqueue(a2.id)
        await _until(lambda: len(pool._deferred.get("a.com", ())) == 1)
        release.set()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a1, a2

    a1, a2 = asyncio.run(scenario())
    assert store.get(a1.id).status == FAILED
    assert store.get(a2.id).status == DONE
    assert ran == ["https://a.com/1", "https://a.com/2"]


def test_site_gap_is_waited_with_jitter(settings, store, monkeypatch):
    settings.max_concurrent, settings.max_per_site, settings.site_gap = 1, 1, 0.2
    calls = []
    monkeypatch.setattr(worker_mod, "jitter", lambda lo, hi: (calls.append((lo, hi)), 1.5)[1])
    times = {}

    def fn(job_id, url, st, se):
        times[url] = worker_mod.time.monotonic()
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        pool.enqueue(store.add("https://a.com/1").id)
        pool.enqueue(store.add("https://a.com/2").id)
        pool.enqueue(store.add("https://b.com/1").id)   # different site: no gap
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert calls == [(0.5, 1.5)]   # only the second a.com job had a previous finish to wait on
    assert times["https://a.com/2"] - times["https://a.com/1"] >= 0.3 - 0.02
    assert times["https://b.com/1"] - times["https://a.com/2"] < 0.2


def test_site_gap_counts_from_the_last_finish(settings, store, monkeypatch):
    """Only the remaining part of the gap is waited: with the patched clock far past the
    previous finish, no wait happens even with a long gap."""
    settings.max_concurrent, settings.max_per_site, settings.site_gap = 1, 1, 30
    now = [1000.0]

    def fake_clock():
        now[0] += 100   # every reading is 100 s later than the last
        return now[0]

    monkeypatch.setattr(worker_mod, "clock", fake_clock)
    monkeypatch.setattr(worker_mod, "jitter", lambda lo, hi: 1.0)

    async def scenario():
        pool = WorkerPool(store, settings,
                          download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        pool.enqueue(store.add("https://a.com/1").id)
        pool.enqueue(store.add("https://a.com/2").id)
        await asyncio.wait_for(pool.queue.join(), timeout=2)   # a 30 s wait would time out
        await pool.stop()
        return pool

    pool = asyncio.run(scenario())
    assert set(pool._last_finish) == {"a.com"}


def test_job_removed_during_gap_is_skipped_and_releases_the_site(settings, store, monkeypatch):
    settings.max_concurrent, settings.max_per_site, settings.site_gap = 2, 1, 0.3
    monkeypatch.setattr(worker_mod, "jitter", lambda lo, hi: 1.0)
    ran = []

    def fn(job_id, url, st, se):
        ran.append(url)
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        a1 = store.add("https://a.com/1")
        pool.enqueue(a1.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        finished = pool._last_finish["a.com"]
        a2 = store.add("https://a.com/2")
        pool.enqueue(a2.id)
        await asyncio.sleep(0.05)       # a2 is waiting out the gap
        store.remove(a2.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert pool._running["a.com"] == 0
        assert pool._last_finish["a.com"] == finished   # nothing ran, so no new finish time
        await pool.stop()

    asyncio.run(scenario())
    assert ran == ["https://a.com/1"]


def test_worker_clock_and_jitter_defaults():
    import random
    import time
    assert worker_mod.clock is time.monotonic
    assert worker_mod.jitter is random.uniform


# -- round 7: start clears retry state; retry timers -----------------------------------

def test_start_clears_retry_at_and_error(settings, store):
    seen = []

    def fn(job_id, url, st, se):
        j = st.get(job_id)
        seen.append((j.status, j.priority, j.retry_at, j.error))
        st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        job = store.add("https://example.com/a")
        store.update(job.id, priority=True, retry_at="2026-11-05T10:00:30+00:00",
                     error="retrying: timed out", attempts=1)
        pool.enqueue(job.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert seen == [(DOWNLOADING, False, None, None)]
    assert store.get(job.id).attempts == 1   # only a manual retry resets the count


def test_schedule_retry_enqueues_after_the_delay(settings, store):
    ran = []

    async def scenario():
        pool = WorkerPool(store, settings,
                          download_fn=lambda j, u, st, se: (ran.append(j), st.update(j, status=DONE)))
        await pool.start()
        job = store.add("https://example.com/a")
        store.update(job.id, retry_at="2026-11-05T10:00:30+00:00")
        pool.schedule_retry(job.id, 0.1)
        assert len(pool._timers) == 1
        await asyncio.sleep(0.03)
        assert ran == [] and pool.queue.qsize() == 0
        await _until(lambda: store.get(job.id).status == DONE)
        await _until(lambda: not pool._timers)   # finished timers are forgotten
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert ran == [job.id]


@pytest.mark.parametrize("change", ["failed", "paused", "removed", "no_retry_at"])
def test_schedule_retry_skips_jobs_no_longer_waiting(settings, store, change):
    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)
        job = store.add("https://example.com/a")
        store.update(job.id, retry_at="2026-11-05T10:00:30+00:00")
        pool.schedule_retry(job.id, 0.02)
        if change == "failed":
            store.update(job.id, status=FAILED, error="cancelled")
        elif change == "paused":
            store.update(job.id, status=PAUSED)
        elif change == "removed":
            store.remove(job.id)
        else:
            store.update(job.id, retry_at=None)
        await _until(lambda: not pool._timers)
        return pool.queue.qsize()

    assert asyncio.run(scenario()) == 0


def test_stop_cancels_pending_retry_timers(settings, store):
    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)
        await pool.start()
        job = store.add("https://example.com/a")
        store.update(job.id, retry_at="2026-11-05T10:00:30+00:00")
        pool.schedule_retry(job.id, 30)
        (timer,) = pool._timers
        await asyncio.wait_for(pool.stop(), timeout=2)
        return pool, timer

    pool, timer = asyncio.run(scenario())
    assert timer.cancelled()
    assert not pool._timers and pool.queue.qsize() == 0


def test_worker_schedules_retry_when_download_requeues_the_job(settings, store, monkeypatch):
    monkeypatch.setattr(worker_mod, "RETRY_DELAY", 0.05)
    calls = []

    def fn(job_id, url, st, se):
        calls.append(job_id)
        if len(calls) == 1:   # what downloader does on a transient failure
            st.update(job_id, status=QUEUED, attempts=1, retry_at="2026-11-05T10:00:30+00:00",
                      error="retrying: timed out")
        else:
            st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        job = store.add("https://example.com/a")
        pool.enqueue(job.id)
        await _until(lambda: store.get(job.id).status == DONE)
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert calls == [job.id, job.id]
    j = store.get(job.id)
    assert j.retry_at is None and j.error is None and j.attempts == 1


def test_worker_does_not_schedule_retry_for_finished_jobs(settings, store):
    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        scheduled = []
        pool.schedule_retry = lambda job_id, delay: scheduled.append(job_id)
        await pool.start()
        pool.enqueue(store.add("https://example.com/a").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return scheduled

    assert asyncio.run(scenario()) == []


def test_queue_pause_during_site_gap_holds_the_job(settings, store, monkeypatch):
    settings.max_concurrent, settings.max_per_site, settings.site_gap = 1, 1, 1000
    monkeypatch.setattr(worker_mod, "jitter", lambda lo, hi: 1.0)
    ran = []

    async def scenario():
        release, entered = _controlled_sleep(monkeypatch)
        pool = WorkerPool(store, settings,
                          download_fn=lambda j, u, st, se: (ran.append(u), st.update(j, status=DONE)))
        await pool.start()
        pool.enqueue(store.add("https://a.com/1").id)
        await _until(lambda: ran == ["https://a.com/1"])
        pool.enqueue(store.add("https://a.com/2").id)
        await asyncio.wait_for(entered.wait(), timeout=5)   # a2 is inside the gap
        pool.pause()
        release.set()               # gap over, but paused
        await asyncio.sleep(0.1)
        assert ran == ["https://a.com/1"]
        pool.resume()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert ran == ["https://a.com/1", "https://a.com/2"]


def test_parked_priority_job_goes_to_the_front_of_its_sites_deferred_list(settings, store):
    settings.max_concurrent, settings.max_per_site = 2, 1
    fn, events, log = _blocking_downloads({"https://a.com/1"})

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        a1, a2, a3 = (store.add(f"https://a.com/{i}") for i in (1, 2, 3))
        for j in (a1, a2):
            pool.enqueue(j.id)
        await _until(lambda: len(pool._deferred.get("a.com", ())) == 1)
        store.update(a3.id, priority=True)   # "download next"
        pool.enqueue_front(a3.id)
        await _until(lambda: len(pool._deferred.get("a.com", ())) == 2)
        assert list(pool._deferred["a.com"]) == [a3.id, a2.id]
        events["https://a.com/1"].set()
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    starts = [u for ev, u in log if ev == "start"]
    assert starts == ["https://a.com/1", "https://a.com/3", "https://a.com/2"]


def test_retry_timer_logs_a_store_error_instead_of_dying_silently(settings, store, caplog):
    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)

        def broken(job_id):
            raise RuntimeError("store is broken")

        store.get = broken
        pool.schedule_retry("j1", 0)
        (timer,) = pool._timers
        await asyncio.wait_for(asyncio.gather(timer, return_exceptions=True), timeout=2)
        return timer

    with caplog.at_level("ERROR", logger="app.worker"):
        timer = asyncio.run(scenario())
    assert timer.exception() is None
    assert any("retry timer" in r.getMessage() and r.exc_info for r in caplog.records)


# -- download window (round 9) -------------------------------------------------------

from datetime import datetime as _dt, time as _time, timezone as _tz

from app import window as window_mod
from app.window import Window

_CLOSED = _dt(2026, 11, 6, 12, 0, tzinfo=_tz.utc)   # outside 01:00-07:00
_OPEN = _dt(2026, 11, 6, 3, 0, tzinfo=_tz.utc)


def _window(settings, monkeypatch, now):
    """Configure a 01:00-07:00 window and a patchable clock; returns a one-item list whose
    element is the current time (assign to [0] to move the clock)."""
    settings.download_window = Window(_time(1), _time(7))
    clock_now = [now]
    monkeypatch.setattr(worker_mod, "now_fn", lambda s: clock_now[0])
    return clock_now


def _done_download(ran):
    def fn(job_id, url, st, se):
        ran.append(job_id)
        st.update(job_id, status=DONE)
    return fn


def test_now_fn_default():
    assert worker_mod.now_fn is window_mod.now_in_tz
    assert worker_mod.WINDOW_CHECK_INTERVAL == 60


def test_no_window_means_never_closed(settings, store):
    pool = WorkerPool(store, settings)
    assert settings.download_window is None
    assert pool._window_closed() is False


def test_window_closed_uses_now_fn(settings, store, monkeypatch):
    clock_now = _window(settings, monkeypatch, _CLOSED)
    pool = WorkerPool(store, settings)
    assert pool._window_closed() is True
    clock_now[0] = _OPEN
    assert pool._window_closed() is False


def test_closed_window_parks_job_and_worker_takes_the_next(settings, store, monkeypatch):
    settings.max_concurrent = 1
    _window(settings, monkeypatch, _CLOSED)
    ran = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=_done_download(ran))
        await pool.start()
        a, b = store.add("https://a.com/1"), store.add("https://b.com/1")
        store.update(b.id, now=True)   # "download now" job runs regardless of the window
        pool.enqueue(a.id)
        pool.enqueue(b.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert list(pool._held) == [a.id]
        assert store.get(a.id).status == QUEUED
        await pool.stop()
        return a, b

    a, b = asyncio.run(scenario())
    assert ran == [b.id]
    assert store.get(b.id).now is False   # starting a job clears now


def test_release_starts_a_held_job(settings, store, monkeypatch):
    _window(settings, monkeypatch, _CLOSED)
    ran = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=_done_download(ran))
        await pool.start()
        a = store.add("https://a.com/1")
        pool.enqueue(a.id)
        await _until(lambda: list(pool._held) == [a.id])
        assert pool.release("unknown") is False
        assert pool.queue.qsize() == 0
        store.update(a.id, now=True)   # what /now does before release
        assert pool.release(a.id) is True
        assert list(pool._held) == []
        await _until(lambda: store.get(a.id).status == DONE)
        assert pool.release(a.id) is False   # no longer held
        await pool.stop()
        return a

    a = asyncio.run(scenario())
    assert ran == [a.id] and store.get(a.id).now is False


def test_release_puts_job_at_the_front(settings, store, monkeypatch):
    _window(settings, monkeypatch, _CLOSED)
    pool = WorkerPool(store, settings)
    h1, h2 = store.add("https://a.com/1").id, store.add("https://a.com/2").id

    async def scenario():
        pool._held.extend([h1, h2])
        pool.enqueue("x")
        assert pool.release(h2) is True
        assert list(pool.queue._items) == [h2, "x"]
        assert list(pool._held) == [h1]

    asyncio.run(scenario())


def test_release_all_keeps_order_at_the_front(settings, store):
    pool = WorkerPool(store, settings)
    h1, h2, h3 = (store.add(f"https://a.com/{i}").id for i in range(3))

    async def scenario():
        pool._held.extend([h1, h2, h3])
        pool.enqueue("x")
        pool.release_all()
        assert list(pool.queue._items) == [h1, h2, h3, "x"]
        assert not pool._held
        pool.release_all()   # nothing held: no-op
        assert pool.queue.qsize() == 4

    asyncio.run(scenario())


def test_release_and_release_all_drop_jobs_no_longer_waiting(settings, store):
    """A held job that was cancelled/paused/removed, or now waits out a retry delay, is
    dropped from the held list instead of being re-enqueued."""
    pool = WorkerPool(store, settings)
    jobs = [store.add(f"https://a.com/{i}").id for i in range(5)]
    cancelled, retrying, paused, ok, gone = jobs
    store.update(cancelled, status=FAILED, error="cancelled")
    store.update(retrying, retry_at="2026-11-06T12:00:00+00:00")
    store.update(paused, status=PAUSED)
    store.remove(gone)

    async def scenario():
        pool._held.extend([cancelled, ok])
        assert pool.release(cancelled) is False
        assert pool.queue.qsize() == 0 and list(pool._held) == [ok]
        pool._held.extend([retrying, paused, gone])
        pool.release_all()
        assert list(pool.queue._items) == [ok]
        assert not pool._held
        pool._held.append(retrying)
        assert pool.release(retrying) is False
        assert pool.queue.qsize() == 1 and not pool._held

    asyncio.run(scenario())


def test_priority_job_goes_to_the_front_of_held(settings, store, monkeypatch):
    settings.max_concurrent = 1
    _window(settings, monkeypatch, _CLOSED)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)
        await pool.start()
        a, b, c = (store.add(f"https://a.com/{i}") for i in range(3))
        store.update(b.id, priority=True)
        for j in (a, b, c):
            pool.enqueue(j.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert list(pool._held) == [b.id, a.id, c.id]
        await pool.stop()

    asyncio.run(scenario())


def test_window_closing_during_the_site_gap_parks_the_job(settings, store, monkeypatch):
    settings.max_concurrent, settings.max_per_site, settings.site_gap = 1, 1, 0.3
    monkeypatch.setattr(worker_mod, "jitter", lambda lo, hi: 1.0)
    clock_now = _window(settings, monkeypatch, _OPEN)
    ran = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=_done_download(ran))
        await pool.start()
        a1 = store.add("https://a.com/1")
        pool.enqueue(a1.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        finished = pool._last_finish["a.com"]
        a2 = store.add("https://a.com/2")
        pool.enqueue(a2.id)
        await asyncio.sleep(0.05)          # a2 was taken while open and now waits out the gap
        assert pool._running["a.com"] == 1
        clock_now[0] = _CLOSED
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert list(pool._held) == [a2.id]
        assert store.get(a2.id).status == QUEUED
        assert pool._running["a.com"] == 0                 # slot given back
        assert pool._last_finish["a.com"] == finished      # nothing started
        assert ran == [a1.id]
        clock_now[0] = _OPEN
        pool.release_all()
        await _until(lambda: store.get(a2.id).status == DONE)
        await pool.stop()
        return a1, a2

    a1, a2 = asyncio.run(scenario())
    assert ran == [a1.id, a2.id]


def test_held_job_is_not_parked_twice(settings, store, monkeypatch):
    _window(settings, monkeypatch, _CLOSED)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda *a: None)
        await pool.start()
        a = store.add("https://a.com/1")
        pool.enqueue(a.id)
        pool.enqueue_front(a.id)   # e.g. "download next" while the window is closed
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert list(pool._held) == [a.id]
        await pool.stop()

    asyncio.run(scenario())


def test_ticker_releases_held_jobs_when_the_window_opens(settings, store, monkeypatch):
    clock_now = _window(settings, monkeypatch, _CLOSED)
    monkeypatch.setattr(worker_mod, "WINDOW_CHECK_INTERVAL", 0.02)
    ran = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=_done_download(ran))
        await pool.start()
        a, b = store.add("https://a.com/1"), store.add("https://b.com/1")
        pool.enqueue(a.id)
        pool.enqueue(b.id)
        await _until(lambda: list(pool._held) == [a.id, b.id])
        await asyncio.sleep(0.1)   # several ticks while closed: nothing moves
        assert ran == [] and list(pool._held) == [a.id, b.id]
        clock_now[0] = _OPEN
        await _until(lambda: len(ran) == 2)
        assert not pool._held
        await pool.stop()
        return a, b

    a, b = asyncio.run(scenario())
    assert ran == [a.id, b.id]


def test_ticker_survives_an_error(settings, store, monkeypatch):
    monkeypatch.setattr(worker_mod, "WINDOW_CHECK_INTERVAL", 0.01)
    calls = []

    def boom(s):
        calls.append(1)
        raise RuntimeError("tz broke")

    settings.download_window = Window(_time(1), _time(7))
    monkeypatch.setattr(worker_mod, "now_fn", boom)

    async def scenario():
        pool = WorkerPool(store, settings)
        await pool.start()
        await _until(lambda: len(calls) >= 3)
        assert not pool._ticker.done()
        await pool.stop()

    asyncio.run(scenario())


def test_stop_cancels_the_ticker(settings, store):
    async def scenario():
        pool = WorkerPool(store, settings)
        await pool.start()
        ticker = pool._ticker
        assert ticker is not None and not ticker.done()
        await pool.stop()
        assert ticker.cancelled()
        assert pool._ticker is None

    asyncio.run(scenario())


def test_open_window_runs_jobs_normally(settings, store, monkeypatch):
    _window(settings, monkeypatch, _OPEN)
    ran = []

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=_done_download(ran))
        await pool.start()
        a = store.add("https://a.com/1")
        pool.enqueue(a.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return a

    a = asyncio.run(scenario())
    assert ran == [a.id]


# -- finish webhook -------------------------------------------------------------

def _hook(settings, monkeypatch, results=None):
    """Configure a webhook and capture send_fn calls; results is the list of return values
    (default: always True)."""
    settings.webhook_url = "https://hooks.example/x"
    sent = []
    results = list(results) if results is not None else None

    def fake_send(se, body, event):
        sent.append((event, json.loads(body)))
        return results.pop(0) if results else True

    monkeypatch.setattr(worker_mod, "send_fn", fake_send)
    return sent


@pytest.mark.parametrize("final", [DONE, FAILED])
def test_webhook_fires_when_a_job_ends(settings, store, monkeypatch, final):
    sent = _hook(settings, monkeypatch)

    async def scenario():
        pool = WorkerPool(store, settings,
                          download_fn=lambda j, u, st, se: st.update(j, status=final, filename="a.mp4"))
        await pool.start()
        job = store.add("https://example.com/a")
        pool.enqueue(job.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert [(e, d["job"]["id"]) for e, d in sent] == [(final, job.id)]
    assert sent[0][1]["file_url"] == "/files/a.mp4"


def test_webhook_waits_for_the_automatic_retry(settings, store, monkeypatch):
    sent = _hook(settings, monkeypatch)
    monkeypatch.setattr(worker_mod, "RETRY_DELAY", 0.05)
    calls = []

    def fn(job_id, url, st, se):
        calls.append(job_id)
        if len(calls) == 1:
            st.update(job_id, status=QUEUED, attempts=1, retry_at="2026-11-05T10:00:30+00:00")
        else:
            st.update(job_id, status=DONE)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=fn)
        await pool.start()
        job = store.add("https://example.com/a")
        pool.enqueue(job.id)
        await _until(lambda: len(calls) == 1 and store.get(job.id).status == QUEUED)
        await asyncio.sleep(0.01)
        assert sent == []                      # pending retry: no webhook yet
        await _until(lambda: store.get(job.id).status == DONE)
        await _until(lambda: len(sent) == 1)
        await pool.stop()

    asyncio.run(scenario())
    assert [e for e, _ in sent] == ["done"]


def test_webhook_retries_once_after_a_failed_send(settings, store, monkeypatch):
    sent = _hook(settings, monkeypatch, results=[False, False])

    monkeypatch.setattr(webhook, "WEBHOOK_RETRY", 777.0)
    real_sleep = asyncio.sleep

    async def scenario():
        release, entered = asyncio.Event(), asyncio.Event()

        async def fake_sleep(delay, *args, **kwargs):
            if delay == 777.0:                 # only the webhook retry timer, not the ticker
                entered.set()
                await release.wait()
                return None
            return await real_sleep(delay, *args, **kwargs)

        monkeypatch.setattr(worker_mod.asyncio, "sleep", fake_sleep)
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        job = store.add("https://example.com/a")
        pool.enqueue(job.id)
        await asyncio.wait_for(entered.wait(), timeout=5)   # the retry timer is sleeping
        assert len(sent) == 1 and len(pool._timers) == 1
        release.set()
        await _until(lambda: len(sent) == 2)
        await _until(lambda: not pool._timers)
        await asyncio.sleep(0.05)
        await pool.stop()

    asyncio.run(scenario())
    assert len(sent) == 2                       # one retry only, even though it failed too
    assert sent[0][1] == sent[1][1]             # same body


def test_webhook_no_retry_after_a_successful_send(settings, store, monkeypatch):
    sent = _hook(settings, monkeypatch, results=[True])

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        pool.enqueue(store.add("https://example.com/a").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert not pool._timers
        await pool.stop()

    asyncio.run(scenario())
    assert len(sent) == 1


def test_stop_cancels_a_pending_webhook_retry(settings, store, monkeypatch):
    sent = _hook(settings, monkeypatch, results=[False])
    monkeypatch.setattr(webhook, "WEBHOOK_RETRY", 30.0)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        pool.enqueue(store.add("https://example.com/a").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await _until(lambda: len(pool._timers) == 1)
        await pool.stop()
        assert not pool._timers

    asyncio.run(scenario())
    assert len(sent) == 1


def test_webhook_never_fires_when_unconfigured(settings, store, monkeypatch):
    settings.webhook_url = None
    sent = []
    monkeypatch.setattr(worker_mod, "send_fn", lambda *a: sent.append(a) or True)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        pool.enqueue(store.add("https://example.com/a").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()

    asyncio.run(scenario())
    assert sent == []


def test_webhook_send_error_does_not_fail_the_job(settings, store, monkeypatch):
    settings.webhook_url = "https://hooks.example/x"

    def boom(*a):
        raise RuntimeError("bad")

    monkeypatch.setattr(worker_mod, "send_fn", boom)

    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        job = store.add("https://example.com/a")
        pool.enqueue(job.id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        await pool.stop()
        return job

    job = asyncio.run(scenario())
    assert store.get(job.id).status == DONE


def test_enqueue_from_another_thread_wakes_a_worker(settings, store):
    """Subscription checks run in a thread and call pool.enqueue from there."""
    async def scenario():
        pool = WorkerPool(store, settings, download_fn=lambda j, u, st, se: st.update(j, status=DONE))
        await pool.start()
        await asyncio.sleep(0.01)              # workers are blocked in get()
        job = store.add("https://example.com/a")
        await asyncio.to_thread(pool.enqueue, job.id)
        await _until(lambda: store.get(job.id).status == DONE)
        await pool.stop()

    asyncio.run(scenario())
