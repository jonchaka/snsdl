import asyncio
import logging
import shutil
from collections import deque

from . import downloader
from .config import Settings
from .jobs import DOWNLOADING, FAILED, QUEUED, JobStore

log = logging.getLogger(__name__)

DISK_CHECK_INTERVAL = 10   # seconds between free-space checks while a job waits for disk


def disk_low(settings: Settings) -> bool:
    """True when the download disk has less than MIN_FREE_SPACE free (never when it is 0)."""
    if settings.min_free_space == 0:
        return False
    try:
        free = shutil.disk_usage(settings.downloads_dir).free
    except OSError:
        # Cannot tell; do not hold every download hostage to a failing statvfs.
        log.warning("could not read free space of %s", settings.downloads_dir, exc_info=True)
        return False
    return free < settings.min_free_space


class JobQueue:
    """FIFO of job ids that can also push to the front ("download next").

    Drop-in for the asyncio.Queue methods the pool uses. Items stay in the deque until a
    getter actually takes them; waiting getters are woken through futures, so put_nowait and
    put_front stay synchronous (an asyncio.Condition cannot be notified without awaiting its
    lock). join() waits until every item ever put has been matched by task_done().
    """

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._getters: deque[asyncio.Future] = deque()
        self._unfinished = 0
        self._finished = asyncio.Event()
        self._finished.set()

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def put_nowait(self, item: str) -> None:
        self._items.append(item)
        self._added()

    def put_front(self, item: str) -> None:
        self._items.appendleft(item)
        self._added()

    def _added(self) -> None:
        self._unfinished += 1
        self._finished.clear()
        self._wake_one()

    def _wake_one(self) -> None:
        while self._getters:
            fut = self._getters.popleft()
            if not fut.done():
                fut.set_result(None)
                return

    async def get(self) -> str:
        while not self._items:
            fut = asyncio.get_running_loop().create_future()
            self._getters.append(fut)
            try:
                await fut
            except BaseException:
                fut.cancel()
                try:
                    self._getters.remove(fut)
                except ValueError:
                    pass
                # Woken for an item but cancelled before taking it: hand the wake-up on so
                # the item is not stranded while another worker sleeps.
                if self._items:
                    self._wake_one()
                raise
        return self._items.popleft()

    def task_done(self) -> None:
        if self._unfinished <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished -= 1
        if self._unfinished == 0:
            self._finished.set()

    async def join(self) -> None:
        if self._unfinished:
            await self._finished.wait()


class WorkerPool:
    """N asyncio consumers pulling job ids from a queue and running downloads in threads."""

    def __init__(self, store: JobStore, settings: Settings, download_fn=downloader.download):
        self.store = store
        self.settings = settings
        self._download = download_fn
        self.queue = JobQueue()
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

    def enqueue_front(self, job_id: str) -> None:
        """Queue a job ahead of everything not yet taken by a worker ("download next")."""
        self.queue.put_front(job_id)

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

    async def _wait_until_startable(self, n: int, job_id: str) -> None:
        """Wait for the user pause (gate), the restart hold (drain) and enough free disk.
        The job stays queued meanwhile. Gate and drain are re-checked after every disk wait
        so pausing the queue while waiting for space still holds the job."""
        warned = False
        while True:
            await self.gate.wait()
            await self.drain.wait()
            # Module-level lookup at call time so tests can patch disk_low/DISK_CHECK_INTERVAL.
            if not disk_low(self.settings):
                return
            if not warned:
                log.warning("worker %d: low disk space, job %s waits for free space", n, job_id)
                warned = True
            await asyncio.sleep(DISK_CHECK_INTERVAL)

    async def _run(self, n: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                # Hold the id here while the queue is paused. Gating after get() (not before)
                # means a worker already blocked in get() cannot start a job enqueued mid-pause.
                await self._wait_until_startable(n, job_id)
                # Re-fetch after the waits: the job may have been removed or paused meanwhile.
                job = self.store.get(job_id)
                if job is None:
                    log.info("worker %d: job %s vanished (cleared?), skipping", n, job_id)
                    continue
                if job.status != QUEUED:
                    # paused, already running/finished via a duplicate queue entry, etc.
                    log.info("worker %d: job %s is %s, not queued; skipping", n, job_id, job.status)
                    continue
                self.store.update(job_id, status=DOWNLOADING, priority=False)
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
