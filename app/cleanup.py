import logging
import shutil
from pathlib import Path
from typing import Iterable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import thumbs
from .config import Settings
from .jobs import DONE, DOWNLOADING, PAUSED, QUEUED, Job, JobStore

log = logging.getLogger(__name__)

JOB_ID = "nightly-cleanup"


def empty_dir(path: Path, protect: frozenset[str] = frozenset(),
              protect_prefixes: tuple[str, ...] = ()) -> int:
    """Delete every entry inside `path` (not `path` itself) except names in `protect` and names
    starting with one of `protect_prefixes`. Returns entries removed."""
    if not path.exists():
        return 0
    removed = 0
    for entry in path.iterdir():
        if entry.name in protect or (protect_prefixes and entry.name.startswith(protect_prefixes)):
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("cleanup: could not remove %s: %s", entry, exc)
            continue
        removed += 1
    return removed


ACTIVE = (QUEUED, PAUSED, DOWNLOADING)


def is_kept(job: Job) -> bool:
    return job.keep and job.status == DONE


def is_active(job: Job) -> bool:
    return job.status in ACTIVE


def sweep_thumbs(store: JobStore, settings: Settings) -> int:
    """Delete thumbnail files no job references (older than an hour: a fresh one may belong to a
    probe whose job is about to be added)."""
    return thumbs.sweep_orphans(settings.thumbs_dir, store.thumbs_in_use())


def delete_unused_thumbs(store: JobStore, settings: Settings, removed: Iterable[Job]) -> None:
    """Delete the thumbnails of jobs just removed from `store`, except keys a remaining job
    still references (call after the removal)."""
    keys = {job.thumb for job in removed if job.thumb}
    for key in keys - store.thumbs_in_use():
        thumbs.delete(settings.thumbs_dir, key)


def run_cleanup(store: JobStore, settings: Settings, keep_active: bool = False) -> int:
    """Delete every download except kept ones, empty tmp, drop every job except kept ones
    (and their thumbnails), then sweep orphan thumbnails.

    keep_active=True (the nightly run) also keeps queued, paused and downloading jobs with
    their files (downloads and their partials in tmp): a subscription entry still waiting at
    cleanup time is already in the subscription's `seen` and would otherwise never come back.
    The manual cleanup (keep_active=False) clears those too."""
    before = store.list()
    keep = (lambda job: is_kept(job) or is_active(job)) if keep_active else is_kept
    kept = store.clear(keep=keep)
    kept_ids = {job.id for job in kept}
    delete_unused_thumbs(store, settings, [job for job in before if job.id not in kept_ids])
    # A kept job's video and its separate subtitle files (sidecars) stay.
    protect = frozenset(name for job in kept for name in (job.filename, *job.sidecars) if name)
    # An active job's tmp files: outputs/partials "<id>-...", its use-once cookies and the
    # merged cookies copy of a running download (see cookies.CookieStore).
    active_ids = [job.id for job in kept if is_active(job)]
    tmp_protect = frozenset(name for jid in active_ids
                            for name in (f"cookies-once-{jid}.txt", f"cookies-{jid}.txt"))
    tmp_prefixes = tuple(f"{jid}-" for jid in active_ids)
    removed = (empty_dir(settings.downloads_dir, protect)
               + empty_dir(settings.tmp_dir, tmp_protect, tmp_prefixes))
    sweep_thumbs(store, settings)
    log.info("cleanup: removed %d entries, kept %d, cleared job list", removed, len(kept))
    return removed


UPDATE_JOB_ID = "yt_dlp_update"
SUBSCRIPTIONS_JOB_ID = "subscriptions"


def build_scheduler(store: JobStore, settings: Settings, update_job=None,
                    subscription_job=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    trigger = CronTrigger.from_crontab(settings.cleanup_cron, timezone=settings.tz)
    scheduler.add_job(run_cleanup, trigger, args=[store, settings],
                      kwargs={"keep_active": True}, id=JOB_ID, replace_existing=True,
                      misfire_grace_time=3600)
    if update_job is not None and settings.update_cron:
        scheduler.add_job(update_job, CronTrigger.from_crontab(settings.update_cron, timezone=settings.tz),
                          id=UPDATE_JOB_ID, replace_existing=True, misfire_grace_time=3600)
    if subscription_job is not None and settings.subscription_cron:
        scheduler.add_job(subscription_job,
                          CronTrigger.from_crontab(settings.subscription_cron, timezone=settings.tz),
                          id=SUBSCRIPTIONS_JOB_ID, replace_existing=True, misfire_grace_time=3600)
    return scheduler
