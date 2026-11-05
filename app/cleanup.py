import logging
import shutil
from pathlib import Path
from typing import Iterable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import thumbs
from .config import Settings
from .jobs import DONE, Job, JobStore

log = logging.getLogger(__name__)

JOB_ID = "nightly-cleanup"


def empty_dir(path: Path, protect: frozenset[str] = frozenset()) -> int:
    """Delete every entry inside `path` (not `path` itself) except names in `protect`.
    Returns entries removed."""
    if not path.exists():
        return 0
    removed = 0
    for entry in path.iterdir():
        if entry.name in protect:
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


def is_kept(job: Job) -> bool:
    return job.keep and job.status == DONE


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


def run_cleanup(store: JobStore, settings: Settings) -> int:
    """Delete every download except kept ones, empty tmp, drop every job except kept ones
    (and their thumbnails), then sweep orphan thumbnails."""
    before = store.list()
    kept = store.clear(keep=is_kept)
    kept_ids = {job.id for job in kept}
    delete_unused_thumbs(store, settings, [job for job in before if job.id not in kept_ids])
    protect = frozenset(job.filename for job in kept if job.filename)
    removed = empty_dir(settings.downloads_dir, protect) + empty_dir(settings.tmp_dir)
    sweep_thumbs(store, settings)
    log.info("cleanup: removed %d entries, kept %d, cleared job list", removed, len(kept))
    return removed


UPDATE_JOB_ID = "yt_dlp_update"


def build_scheduler(store: JobStore, settings: Settings, update_job=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    trigger = CronTrigger.from_crontab(settings.cleanup_cron, timezone=settings.tz)
    scheduler.add_job(run_cleanup, trigger, args=[store, settings], id=JOB_ID, replace_existing=True,
                      misfire_grace_time=3600)
    if update_job is not None and settings.update_cron:
        scheduler.add_job(update_job, CronTrigger.from_crontab(settings.update_cron, timezone=settings.tz),
                          id=UPDATE_JOB_ID, replace_existing=True, misfire_grace_time=3600)
    return scheduler
