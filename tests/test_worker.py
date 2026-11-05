import asyncio
import collections
import threading

import pytest

from app import worker as worker_mod
from app.jobs import DONE, DOWNLOADING, FAILED, PAUSED, QUEUED
from app.worker import JobQueue, WorkerPool, disk_low


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


def test_hold_keeps_dequeued_jobs_from_starting_until_release(settings, store):
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
        pool.release()
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
        pool.release()
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
