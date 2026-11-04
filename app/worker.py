import asyncio
import logging

from . import downloader
from .config import Settings
from .jobs import DOWNLOADING, FAILED, QUEUED, JobStore

log = logging.getLogger(__name__)


class WorkerPool:
    """N asyncio consumers pulling job ids from a queue and running downloads in threads."""

    def __init__(self, store: JobStore, settings: Settings, download_fn=downloader.download):
        self.store = store
        self.settings = settings
        self._download = download_fn
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self.gate = asyncio.Event()   # set = workers may start jobs; cleared = global pause
        self.gate.set()
        self.drain = asyncio.Event()  # cleared while draining for a self-restart (in-memory only)
        self.drain.set()

    @property
    def held(self) -> bool:
        return not self.drain.is_set()

    def hold(self) -> None:
        """Stop starting new jobs without touching the user's persisted pause."""
        self.drain.clear()

    def release(self) -> None:
        self.drain.set()

    @property
    def paused(self) -> bool:
        return not self.gate.is_set()

    def pause(self) -> None:
        """Stop starting new jobs. Downloads already running finish."""
        self.gate.clear()

    def resume(self) -> None:
        self.gate.set()

    def enqueue(self, job_id: str) -> None:
        self.queue.put_nowait(job_id)

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(i), name=f"worker-{i}")
            for i in range(self.settings.max_concurrent)
        ]

    async def stop(self) -> None:
        # Cancelling a task does not stop its in-flight download thread; process exit joins
        # it, and restart recovery marks the job "interrupted by restart".
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run(self, n: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                # Hold the id here while the queue is paused. Gating after get() (not before)
                # means a worker already blocked in get() cannot start a job enqueued mid-pause.
                await self.gate.wait()
                await self.drain.wait()
                job = self.store.get(job_id)
                if job is None:
                    log.info("worker %d: job %s vanished (cleared?), skipping", n, job_id)
                    continue
                if job.status != QUEUED:
                    # paused, already running/finished via a duplicate queue entry, etc.
                    log.info("worker %d: job %s is %s, not queued; skipping", n, job_id, job.status)
                    continue
                self.store.update(job_id, status=DOWNLOADING)
                await asyncio.to_thread(self._download, job_id, job.url, self.store, self.settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
                log.exception("worker %d: unexpected error on job %s", n, job_id)
                try:
                    self.store.update(job_id, status=FAILED, error=f"worker error: {exc}"[:200],
                                      speed=None, eta=None)
                except Exception:  # noqa: BLE001 - store itself may be broken; nothing more to do
                    log.exception("worker %d: could not mark job %s failed", n, job_id)
            finally:
                self.queue.task_done()
