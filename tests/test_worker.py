import asyncio
import threading

from app.jobs import DONE, DOWNLOADING, FAILED, PAUSED
from app.worker import WorkerPool


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
