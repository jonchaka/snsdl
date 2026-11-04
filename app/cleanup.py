import logging
import shutil
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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


def run_cleanup(store: JobStore, settings: Settings) -> int:
    """Delete every download except kept ones, empty tmp, drop every job except kept ones."""
    kept = store.clear(keep=is_kept)
    protect = frozenset(job.filename for job in kept if job.filename)
    removed = empty_dir(settings.downloads_dir, protect) + empty_dir(settings.tmp_dir)
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
