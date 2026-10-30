import asyncio
import logging

from . import downloader
from .config import Settings
from .jobs import DOWNLOADING, FAILED, JobStore

log = logging.getLogger(__name__)


class WorkerPool:
    """N asyncio consumers pulling job ids from a queue and running downloads in threads."""

    def __init__(self, store: JobStore, settings: Settings, download_fn=downloader.download):
        self.store = store
        self.settings = settings
        self._download = download_fn
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

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
                job = self.store.get(job_id)
                if job is None:
                    log.info("worker %d: job %s vanished (cleared?), skipping", n, job_id)
                    continue
                self.store.update(job_id, status=DOWNLOADING)
                await asyncio.to_thread(self._download, job_id, job.url, self.store, self.settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
                log.exception("worker %d: unexpected error on job %s", n, job_id)
                try:
                    self.store.update(job_id, status=FAILED, error=f"worker error: {exc}"[:200])
                except Exception:  # noqa: BLE001 - store itself may be broken; nothing more to do
                    log.exception("worker %d: could not mark job %s failed", n, job_id)
            finally:
                self.queue.task_done()
