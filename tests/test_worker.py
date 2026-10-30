import asyncio
import threading

from app.jobs import DONE, DOWNLOADING
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
        pool.enqueue(store.add("https://example.com/a").id)
        await asyncio.wait_for(pool.queue.join(), timeout=5)
        assert all(not t.done() for t in pool._tasks)  # workers still alive
        await pool.stop()

    asyncio.run(scenario())
