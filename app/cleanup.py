import logging
import shutil
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .jobs import JobStore

log = logging.getLogger(__name__)

JOB_ID = "nightly-cleanup"


def empty_dir(path: Path) -> int:
    """Delete every entry inside `path` (not `path` itself). Returns entries removed."""
    if not path.exists():
        return 0
    removed = 0
    for entry in path.iterdir():
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


def run_cleanup(store: JobStore, settings: Settings) -> int:
    removed = empty_dir(settings.downloads_dir) + empty_dir(settings.tmp_dir)
    store.clear()
    log.info("cleanup: removed %d entries and cleared job list", removed)
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
