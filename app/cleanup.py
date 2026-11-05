import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import files, thumbs
from .config import Settings
from .jobs import DONE, DOWNLOADING, FAILED, PAUSED, QUEUED, Job, JobStore

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


def _thumbs_in_use(store: JobStore, history=None) -> set[str]:
    keys = store.thumbs_in_use()
    if history is not None:
        keys |= history.thumbs_in_use()
    return keys


def sweep_thumbs(store: JobStore, settings: Settings, history=None) -> int:
    """Delete thumbnail files no job (and no history entry) references (older than an hour: a
    fresh one may belong to a probe whose job is about to be added)."""
    return thumbs.sweep_orphans(settings.thumbs_dir, _thumbs_in_use(store, history))


def delete_unused_thumbs(store: JobStore, settings: Settings, removed: Iterable[Job],
                         history=None) -> None:
    """Delete the thumbnails of jobs just removed from `store`, except keys a remaining job or a
    history entry still references (call after the removal)."""
    keys = {job.thumb for job in removed if job.thumb}
    for key in keys - _thumbs_in_use(store, history):
        thumbs.delete(settings.thumbs_dir, key)


def _active_protection(jobs: Iterable[Job]) -> tuple[frozenset[str], tuple[str, ...]]:
    """(tmp names, name prefixes) that belong to active jobs and must survive a cleanup: their
    outputs/partials "<id>-...", their use-once cookies and the merged cookies copy of a running
    download (see cookies.CookieStore). The "<id>-" prefix also guards downloads/: a running
    download may move its finished file (and its sidecars) there after the store snapshot,
    before its job records the filename."""
    active_ids = [job.id for job in jobs if is_active(job)]
    tmp_protect = frozenset(name for jid in active_ids
                            for name in (f"cookies-once-{jid}.txt", f"cookies-{jid}.txt"))
    return tmp_protect, tuple(f"{jid}-" for jid in active_ids)


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
    tmp_protect, active_prefixes = _active_protection(kept)
    removed = (empty_dir(settings.downloads_dir, protect, active_prefixes)
               + empty_dir(settings.tmp_dir, tmp_protect, active_prefixes))
    sweep_thumbs(store, settings)
    log.info("cleanup: removed %d entries, kept %d, cleared job list", removed, len(kept))
    return removed


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _expired(job: Job, cutoff: datetime) -> bool:
    """A done or failed, not kept job that finished (or, lacking finished_at, was created)
    before `cutoff`. A job whose time cannot be read is left alone."""
    if job.status not in (DONE, FAILED) or is_kept(job):
        return False
    when = _parse_time(job.finished_at) or _parse_time(job.created_at)
    return when is not None and when < cutoff


def _delete_old_orphans(directory: Path, referenced: frozenset[str], prefixes: tuple[str, ...],
                        cutoff: float) -> int:
    """Delete files in `directory` whose name is not referenced, does not start with one of
    `prefixes`, and whose mtime is before `cutoff` (epoch seconds). Returns files removed."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if entry.name in referenced or (prefixes and entry.name.startswith(prefixes)):
            continue
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except OSError as exc:
            log.warning("retention: could not remove %s: %s", entry.name, exc)
            continue
        removed += 1
    return removed


def run_retention(store: JobStore, settings: Settings, history=None,
                  now: Optional[datetime] = None) -> int:
    """The scheduled cleanup when RETENTION_DAYS is set: remove done and failed jobs (not kept)
    older than the cutoff with their files, sidecars and unused thumbnails; delete files in
    downloads/ that no job references and that are older than the cutoff; empty tmp/ except
    active jobs' files; sweep orphan thumbnails. Kept and active jobs are untouched; history is
    never changed (its thumbnails count as in use).

    Returns the number of removed entries: jobs removed plus orphan files and tmp entries
    deleted. With RETENTION_DAYS unset nothing is removed (0): a cutoff of "now" would wipe
    every finished download."""
    if not settings.retention_days:
        log.warning("retention: RETENTION_DAYS is not set; nothing removed")
        return 0
    now = datetime.now(timezone.utc) if now is None else now
    cutoff = now - timedelta(days=settings.retention_days)
    expired = store.remove_where(lambda job: _expired(job, cutoff))
    for job in expired:
        files.delete_files(settings, job)
    delete_unused_thumbs(store, settings, expired, history)
    remaining = store.list()
    referenced = frozenset(name for job in remaining for name in (job.filename, *job.sidecars) if name)
    tmp_protect, active_prefixes = _active_protection(remaining)
    orphans = _delete_old_orphans(settings.downloads_dir, referenced, active_prefixes, cutoff.timestamp())
    tmp_removed = empty_dir(settings.tmp_dir, tmp_protect, active_prefixes)
    sweep_thumbs(store, settings, history)
    log.info("retention: removed %d jobs older than %s days, %d orphan files, %d tmp entries",
             len(expired), settings.retention_days, orphans, tmp_removed)
    return len(expired) + orphans + tmp_removed


UPDATE_JOB_ID = "yt_dlp_update"
SUBSCRIPTIONS_JOB_ID = "subscriptions"


def build_scheduler(store: JobStore, settings: Settings, update_job=None,
                    subscription_job=None, history=None) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    trigger = CronTrigger.from_crontab(settings.cleanup_cron, timezone=settings.tz)
    if settings.retention_days:
        scheduler.add_job(run_retention, trigger, args=[store, settings],
                          kwargs={"history": history}, id=JOB_ID, replace_existing=True,
                          misfire_grace_time=3600)
    else:
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
