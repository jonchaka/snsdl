import asyncio
import logging
import random
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from . import downloader, sites, webhook, window
from .config import Settings
from .jobs import DONE, DOWNLOADING, FAILED, QUEUED, RETRY_DELAY, JobStore

log = logging.getLogger(__name__)

DISK_CHECK_INTERVAL = 10   # seconds between free-space checks while a job waits for disk
WINDOW_CHECK_INTERVAL = 60   # seconds between download-window checks while jobs are held

# Module-level so tests can patch the pacing clock, the gap jitter and the window clock.
clock = time.monotonic
jitter = random.uniform
now_fn = window.now_in_tz
send_fn = webhook.send   # blocking finish-webhook POST; patched in tests


def _retry_pending(retry_at: Optional[str]) -> bool:
    """True while an automatic-retry time (ISO UTC) is still in the future. retry_at is only
    cleared when the job starts, so a past (or unparsable) value means the delay is over."""
    if not retry_at:
        return False
    try:
        when = datetime.fromisoformat(retry_at)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when > datetime.now(timezone.utc)


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

    def __init__(self, store: JobStore, settings: Settings, download_fn=downloader.download,
                 record_fn=None, history=None, subs=None, pending=None):
        self.store = store
        self.settings = settings
        self._download = download_fn
        self._record = record_fn      # live recordings; wired by the live task (unused yet)
        self.history = history        # history.HistoryStore: done jobs are recorded there
        self.subs = subs              # subscriptions.SubscriptionStore (auto-keep)
        self.pending = pending        # webhook.PendingStore: finish webhooks that never got through
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
        self._timers: set[asyncio.Task] = set()       # retry re-enqueues, webhook sends and resends
        # Download window, in memory too (a restart re-enqueues; closed -> parked again).
        self._held: deque[str] = deque()              # ids parked while the window is closed
        self._ticker: Optional[asyncio.Task] = None   # releases _held once the window opens
        self._loop: Optional[asyncio.AbstractEventLoop] = None   # set by start(); for enqueue from threads

    @property
    def held(self) -> bool:
        """True while the restart drain hold is on (not about the download window's _held)."""
        return not self.drain.is_set()

    def hold(self) -> None:
        """Stop starting new jobs without touching the user's persisted pause."""
        self.drain.clear()

    def unhold(self) -> None:
        """Lift the restart drain hold."""
        self.drain.set()

    # -- download window ------------------------------------------------------

    def _window_closed(self) -> bool:
        w = self.settings.download_window
        # Module-level now_fn looked up at call time so tests can patch it.
        return w is not None and not w.contains(now_fn(self.settings))

    def _releasable(self, job_id: str) -> bool:
        """A held id goes back to the queue only while its job is still queued and not still
        waiting out an automatic-retry delay (the retry timer re-enqueues those; anything else,
        e.g. cancelled, paused or removed meanwhile, would just be skipped by the worker). A
        retry_at in the past does not block: the timer already fired (or a restart re-enqueued
        the job) and the window held it; retry_at is only cleared once the job starts."""
        job = self.store.get(job_id)
        return job is not None and job.status == QUEUED and not _retry_pending(job.retry_at)

    def _hold_job(self, n: int, job_id: str, priority: bool) -> None:
        """Park a queued job while the download window is closed (once, even if the id was
        queued twice). A "download next" job goes ahead of the other held ones, also when it
        was already held (pressing "Download next" on a held row moves it to the front)."""
        if job_id in self._held:
            if priority and self._held[0] != job_id:
                self._held.remove(job_id)
                self._held.appendleft(job_id)
            return
        log.info("worker %d: download window closed, holding job %s", n, job_id)
        (self._held.appendleft if priority else self._held.append)(job_id)

    def is_held(self, job_id: str) -> bool:
        """True while job_id is parked by the closed download window."""
        return job_id in self._held

    def release(self, job_id: str) -> bool:
        """"Download now": if job_id is parked by the download window, take it out of the held
        list and, if its job is still queued without a pending retry, put it at the queue front
        and return True. False if it was not held (it is then still in the queue, or not queued
        at all) or no longer releasable (dropped); nothing is enqueued then."""
        try:
            self._held.remove(job_id)
        except ValueError:
            return False
        if not self._releasable(job_id):
            log.info("release: held job %s is no longer queued (or waits for a retry); dropping", job_id)
            return False
        self.queue.put_front(job_id)
        return True

    def release_all(self) -> None:
        """Put every held id whose job is still releasable back at the queue front, in the order
        they were held; the others are dropped."""
        held, self._held = list(self._held), deque()
        for jid in reversed(held):   # put_front reverses, so feed them back to front
            if self._releasable(jid):
                self.queue.put_front(jid)
            else:
                log.info("release: held job %s is no longer queued (or waits for a retry); dropping", jid)

    async def _window_ticker(self) -> None:
        while True:
            # Module-level lookup at call time so tests can patch WINDOW_CHECK_INTERVAL.
            await asyncio.sleep(WINDOW_CHECK_INTERVAL)
            try:
                if not self._window_closed() and self._held:
                    log.info("download window open: releasing %d held job(s)", len(self._held))
                    self.release_all()
            except Exception:  # noqa: BLE001 - nobody awaits this task; keep ticking
                log.exception("download window check failed")

    @property
    def paused(self) -> bool:
        return not self.gate.is_set()

    def pause(self) -> None:
        """Stop starting new jobs. Downloads already running finish."""
        self.gate.clear()

    def resume(self) -> None:
        self.gate.set()

    def enqueue(self, job_id: str) -> None:
        """Queue job_id. Safe to call from another thread (subscription checks run in one):
        the put is then handed to the pool's event loop, since JobQueue is not thread-safe."""
        loop = self._loop
        # A stopped (or closed) loop would never run a call_soon_threadsafe callback: put
        # directly then, as before start().
        if loop is not None and not loop.is_closed() and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self.queue.put_nowait, job_id)
                return
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

    # -- finish path ----------------------------------------------------------

    async def _finish(self, ids: list[str]) -> None:
        """After a download returned: the finish webhook per ended job, then the done ones
        go to the history and to auto-keep. Never raises."""
        for job_id in ids:
            self._notify(job_id)
        try:
            done = [j for j in (self.store.get(i) for i in ids) if j is not None and j.status == DONE]
            if self.history is not None and done:
                self.history.record(done)
            if done:
                await self._auto_keep(done)
        except Exception:  # noqa: BLE001 - history/keep must never break the worker
            log.exception("finishing jobs %s failed", ", ".join(ids))

    async def _auto_keep(self, done_jobs: list) -> None:
        """Keep the files of done jobs whose subscription asks for it (filled in by the files task)."""

    # -- finish webhook -------------------------------------------------------

    def _notify(self, job_id: str) -> None:
        """Send the finish webhook if one is configured and the job ended (done or failed; a
        queued job waiting for its automatic retry has not ended). The POST runs in a tracked
        task so a slow endpoint never holds the worker; a failed send gets one more attempt
        after webhook.WEBHOOK_RETRY in the same task. Never raises."""
        if not self.settings.webhook_url:
            return
        try:
            job = self.store.get(job_id)
            if job is None or job.status not in (DONE, FAILED):
                return
            event = job.status
            body = webhook.payload(event, job, self.settings)
            task = asyncio.create_task(self._deliver(job_id, body, event), name=f"webhook-{job_id}")
        except Exception:  # noqa: BLE001 - a webhook must never break the worker
            log.exception("webhook for job %s failed", job_id)
            return
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _deliver(self, job_id: str, body: bytes, event: str) -> None:
        try:
            # Module-level send_fn looked up at call time so tests can patch it.
            if await asyncio.to_thread(send_fn, self.settings, body, event):
                return
            await asyncio.sleep(webhook.WEBHOOK_RETRY)
            # last attempt; it logs
            if not await asyncio.to_thread(send_fn, self.settings, body, event) and self.pending is not None:
                # Kept for one more attempt after the next start.
                self.pending.add(job_id, event, body)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nobody awaits this task
            log.exception("webhook for job %s failed", job_id)

    async def _resend_pending(self) -> None:
        """Once after start: wait webhook.WEBHOOK_RETRY, send every finish webhook a previous
        run could not deliver (one attempt each), then forget them all. Never raises."""
        try:
            await asyncio.sleep(webhook.WEBHOOK_RETRY)
            entries = await asyncio.to_thread(self.pending.load)
            for entry in entries:
                try:
                    ok = await asyncio.to_thread(send_fn, self.settings, entry["body"].encode("utf-8"),
                                                 entry["event"])
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad entry must not stop the others
                    log.exception("webhook resend for job %s failed", entry.get("job_id"))
                    continue
                if not ok:
                    log.warning("webhook resend for job %s (%s) failed again; dropped",
                                entry.get("job_id"), entry.get("event"))
            await asyncio.to_thread(self.pending.clear)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nobody awaits this task
            log.exception("webhook resend failed")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._tasks = [
            asyncio.create_task(self._run(i), name=f"worker-{i}")
            for i in range(self.settings.max_concurrent)
        ]
        self._ticker = asyncio.create_task(self._window_ticker(), name="window-ticker")
        if self.pending is not None and self.settings.webhook_url:
            task = asyncio.create_task(self._resend_pending(), name="webhook-resend")
            self._timers.add(task)
            task.add_done_callback(self._timers.discard)

    async def stop(self) -> None:
        # Cancelling a task does not stop its in-flight download thread; process exit joins
        # it, and restart recovery marks the job "interrupted by restart".
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
            self._ticker = None
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
                if self._window_closed() and not job.now:
                    # Outside the download window: park it (still queued) and move on; the
                    # ticker or "download now" (release) puts it back.
                    self._hold_job(n, job_id, job.priority)
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
                if self._window_closed() and not job.now:
                    # The waits after the first check (gap, pause, disk, drain) can be long: the
                    # window may have closed meanwhile. The finally gives the site slot back.
                    self._hold_job(n, job_id, job.priority)
                    continue
                self.store.update(job_id, status=DOWNLOADING, priority=False, now=False,
                                  retry_at=None, error=None)
                started = True
                ended = await asyncio.to_thread(self._download, job_id, job.url, self.store, self.settings)
                # A transient failure puts the job back to queued with retry_at; it is re-enqueued
                # after the delay rather than now, so the site gets a breather. A split job is
                # gone by now (its parts replaced it).
                job = self.store.get(job_id)
                if job is not None and job.status == QUEUED and job.retry_at:
                    self.schedule_retry(job_id, RETRY_DELAY)
                # Anything but a list (a download_fn without that return value, e.g. test
                # fakes) means just this job.
                await self._finish((ended if isinstance(ended, list) else None) or [job_id])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
                log.exception("worker %d: unexpected error on job %s", n, job_id)
                try:
                    self.store.update(job_id, status=FAILED, error=f"worker error: {exc}"[:200],
                                      speed=None, eta=None)
                except Exception:  # noqa: BLE001 - store itself may be broken; nothing more to do
                    log.exception("worker %d: could not mark job %s failed", n, job_id)
                self._notify(job_id)   # only fires if the job did end up FAILED
            finally:
                if site is not None:
                    self._release_site(site, started)
                self.queue.task_done()
