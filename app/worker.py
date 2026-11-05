import asyncio
import logging
import random
import shutil
import time
from collections import deque

from . import downloader, sites
from .config import Settings
from .jobs import DOWNLOADING, FAILED, QUEUED, RETRY_DELAY, JobStore

log = logging.getLogger(__name__)

DISK_CHECK_INTERVAL = 10   # seconds between free-space checks while a job waits for disk

# Module-level so tests can patch the pacing clock and the gap jitter.
clock = time.monotonic
jitter = random.uniform


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
        # Per-site pacing, all in memory (a restart re-enqueues queued jobs normally).
        self._running: dict[str, int] = {}            # downloads holding a slot, per site
        self._deferred: dict[str, deque[str]] = {}    # ids parked because their site was full
        self._last_finish: dict[str, float] = {}      # clock() when the site's last download ended
        self._timers: set[asyncio.Task] = set()       # pending automatic-retry re-enqueues

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

    def schedule_retry(self, job_id: str, delay: float) -> None:
        """Re-enqueue job_id after delay seconds, if it is then still queued with retry_at set
        (a cancel, pause, removal or manual start in between makes the timer a no-op)."""
        task = asyncio.create_task(self._retry_later(job_id, delay), name=f"retry-{job_id}")
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _retry_later(self, job_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            job = self.store.get(job_id)
            if job is not None and job.status == QUEUED and job.retry_at:
                self.enqueue(job_id)
            else:
                log.info("retry timer: job %s is no longer waiting for a retry; skipping", job_id)
        except Exception:  # noqa: BLE001 - nobody awaits this task; do not fail silently
            log.exception("retry timer: could not re-enqueue job %s", job_id)

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
        # Pending retries are dropped; the jobs stay queued and restart recovery re-enqueues them.
        timers = list(self._timers)
        for t in timers:
            t.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        self._timers.clear()

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

    def _site_full(self, site: str) -> bool:
        limit = self.settings.max_per_site
        return bool(limit) and self._running.get(site, 0) >= limit

    async def _wait_site_gap(self, site: str) -> bool:
        """Sleep whatever is left of the pause since the site's last download ended; True if it
        slept. Jittered so a long queue for one site does not hit it at a machine-regular interval."""
        last = self._last_finish.get(site)
        if last is None or not self.settings.site_gap:
            return False
        delay = last + self.settings.site_gap * jitter(0.5, 1.5) - clock()
        if delay <= 0:
            return False
        await asyncio.sleep(delay)
        return True

    def _release_site(self, site: str, started: bool) -> None:
        """Give back the site slot and put the site's parked ids back at the queue front, in the
        order they were parked (put_front reverses, so feed them back to front)."""
        self._running[site] -= 1
        if started:   # a job skipped during its gap wait did not touch the site
            self._last_finish[site] = clock()
        parked = self._deferred.pop(site, None)
        if parked:
            for jid in reversed(parked):
                self.queue.put_front(jid)

    async def _run(self, n: int) -> None:
        while True:
            job_id = await self.queue.get()
            site = None        # set once this worker holds a slot for the job's site
            started = False
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
                job_site = sites.site_of_url(job.url)
                if self._site_full(job_site):
                    # Park it and move on, so a busy site does not tie up this worker while
                    # jobs for other sites wait. It comes back when that site's download ends.
                    log.info("worker %d: %s is at its limit, parking job %s", n, job_site, job_id)
                    parked = self._deferred.setdefault(job_site, deque())
                    # A "download next" job stays ahead of the site's other parked jobs.
                    (parked.appendleft if job.priority else parked.append)(job_id)
                    continue
                # Take the slot before the gap wait, so another worker cannot start a second
                # download of this site meanwhile.
                site = job_site
                self._running[site] = self._running.get(site, 0) + 1
                if await self._wait_site_gap(site):
                    # The queue may have been paused (or the disk filled) during the gap.
                    await self._wait_until_startable(n, job_id)
                job = self.store.get(job_id)
                if job is None or job.status != QUEUED:
                    log.info("worker %d: job %s changed during the site gap; skipping", n, job_id)
                    continue
                self.store.update(job_id, status=DOWNLOADING, priority=False, retry_at=None, error=None)
                started = True
                await asyncio.to_thread(self._download, job_id, job.url, self.store, self.settings)
                # A transient failure puts the job back to queued with retry_at; it is re-enqueued
                # after the delay rather than now, so the site gets a breather.
                job = self.store.get(job_id)
                if job is not None and job.status == QUEUED and job.retry_at:
                    self.schedule_retry(job_id, RETRY_DELAY)
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
                if site is not None:
                    self._release_site(site, started)
                self.queue.task_done()
