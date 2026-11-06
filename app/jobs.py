from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

QUEUED = "queued"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"
PAUSED = "paused"          # held back by the user; the worker skips it until resumed
ACTIVE = {QUEUED, DOWNLOADING, PAUSED}

FORMATS = ("mp4", "mp3", "audio", "images")   # audio = best audio stream copied as-is (m4a/opus/ogg…);
# images = the files of a photo post, through gallery-dl (app/images.py)
MP3_QUALITIES = ("best", "320", "192")
DEFAULT_FORMAT = "mp4"
DEFAULT_QUALITY = "best"

# Automatic retry of transient failures (timeouts, resets, HTTP 429/5xx). Shared by the
# downloader (which decides to retry) and the worker (which re-enqueues after the delay).
RETRY_DELAY = 30.0   # seconds between a transient failure and the automatic retry
AUTO_RETRIES = 1     # automatic retries per job; a manual Retry resets the count


def check_format_quality(fmt: str, quality: str) -> None:
    """Raise ValueError if (fmt, quality) is not an allowed combination."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if fmt == "mp3":
        ok = quality in MP3_QUALITIES
    elif fmt in ("audio", "images"):
        ok = quality == "best"
    else:
        ok = quality == "best" or (quality.isascii() and quality.isdigit() and int(quality) > 0)
    if not ok:
        raise ValueError(f"quality {quality} is not valid for format {fmt}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    url: str
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    status: str = QUEUED
    progress: int = 0
    title: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    playlist: Optional[str] = None
    playlist_index: Optional[int] = None
    playlist_id: Optional[str] = None   # groups the jobs one /api/playlists request created
    size: Optional[int] = None          # bytes of the finished file
    speed: Optional[int] = None         # bytes/second while downloading, else None
    eta: Optional[int] = None           # seconds remaining while downloading, else None
    keep: bool = False                  # done job whose file survives the nightly cleanup / Clear all
    subtitles: Optional[str] = None     # mp4 only: "en" (site track) or "auto:en" (automatic captions)
    needs_login: bool = False           # failed because the site wants a login (cookies may help)
    clip_start: Optional[float] = None  # seconds; with clip_end, only that range is downloaded
    clip_end: Optional[float] = None    # seconds; None with clip_start set = to the end
    priority: bool = False              # "download next": sits at the queue front until it starts
    log: Optional[str] = None           # last yt-dlp lines of a failed download (scrubbed)
    thumb: Optional[str] = None         # key of the thumbnail file in settings.thumbs_dir
    clip_exact: bool = False            # re-encode at the clip cuts for frame accuracy (slower)
    attempts: int = 0                   # automatic retries used so far
    retry_at: Optional[str] = None      # ISO UTC time of the scheduled automatic retry (job is queued)
    sponsorblock: bool = False          # YouTube: cut SponsorBlock segments
    compat: bool = False                # mp4: prefer H.264/AAC (plays everywhere)
    subtitles_mode: str = "embed"       # "embed" into the video or "file" (.srt beside it)
    sidecars: list[str] = field(default_factory=list)   # names of .srt files beside the video
    subscription_id: Optional[str] = None   # the subscription that queued this job
    now: bool = False                   # "download now": starts even outside the download window
    note: Optional[str] = None          # done job: an optional extra (subtitles, SponsorBlock) was dropped
    finished_at: Optional[str] = None   # ISO UTC time the job became done or failed; cleared by a requeue
    duration: Optional[int] = None      # seconds of the finished media (rounded up)
    uploader: Optional[str] = None      # yt-dlp uploader, else channel
    upload_date: Optional[str] = None   # YYYY-MM-DD
    description: Optional[str] = None   # yt-dlp description, cut to downloader.DESCRIPTION_CHARS
    live: bool = False                  # a live stream: recorded with ffmpeg instead of downloaded
    stop_requested: bool = False        # live: the user (or a restart) asked the recording to stop
    custom_name: Optional[str] = None   # sanitized file name stem chosen by the user
    split: bool = False                 # split the download into one file per chapter
    subdir: Optional[str] = None        # media layout: folder under KEEP_DIR of the kept file

    def to_dict(self, public: bool = False) -> dict:
        """public=True drops the potentially large log, which the job list does not need
        (it is fetched per job on demand)."""
        d = asdict(self)
        if public:
            d.pop("log")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        # Drop keys this version does not know, so a jobs.json written by a newer image loads.
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class JobStore:
    """In-memory job table persisted to a JSON file after every mutation."""

    def __init__(self, path: Path):
        self._path = path
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Call fn() (no arguments) after every save, once the file has been replaced. It runs
        under the store lock, which is not re-entrant, so it must be quick and must not call
        any JobStore method (reads included: list(), get() via find_active, ... all take the
        lock); schedule work elsewhere instead. Exceptions are logged, never raised."""
        self._listeners.append(fn)

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self._path.exists():
            self._jobs = {}
            return
        raw = self._path.read_text() or "[]"
        self._jobs = {d["id"]: Job.from_dict(d) for d in json.loads(raw)}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([j.to_dict() for j in self._jobs.values()], indent=2))
        tmp.replace(self._path)
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 - a listener must never break a save
                log.exception("job store listener failed")

    # -- CRUD --------------------------------------------------------------

    def add(self, url: str, format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY,
            playlist: Optional[str] = None, playlist_index: Optional[int] = None,
            title: Optional[str] = None, playlist_id: Optional[str] = None,
            subtitles: Optional[str] = None, clip_start: Optional[float] = None,
            clip_end: Optional[float] = None, thumb: Optional[str] = None,
            clip_exact: bool = False, sponsorblock: bool = False, compat: bool = False,
            subtitles_mode: str = "embed", subscription_id: Optional[str] = None,
            live: bool = False, split: bool = False, custom_name: Optional[str] = None) -> Job:
        return self.add_many([{"url": url, "format": format, "quality": quality,
                               "playlist": playlist, "playlist_index": playlist_index,
                               "title": title, "playlist_id": playlist_id, "subtitles": subtitles,
                               "clip_start": clip_start, "clip_end": clip_end, "thumb": thumb,
                               "clip_exact": clip_exact, "sponsorblock": sponsorblock,
                               "compat": compat, "subtitles_mode": subtitles_mode,
                               "subscription_id": subscription_id, "live": live,
                               "split": split, "custom_name": custom_name}])[0]

    def add_many(self, items: list[dict]) -> list[Job]:
        """Create one job per item (a dict of `add`'s keyword arguments) under one lock
        acquisition and one save. Returns the jobs in order; an empty list saves nothing."""
        if not items:
            return []
        with self._lock:
            jobs = []
            for item in items:
                job = Job(id=secrets.token_urlsafe(6), **item)
                self._jobs[job.id] = job
                jobs.append(job)
            self.save()
            return jobs

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                log.debug("update: job %s no longer exists (cleared?)", job_id)
                return None
            for key, value in fields.items():
                if not hasattr(job, key):
                    raise AttributeError(f"Job has no field {key!r}")
                setattr(job, key, value)
            self.save()
            return job

    def update_where(self, job_id: str, predicate: Callable[["Job"], bool], **fields) -> Optional[Job]:
        """Like update, but only if predicate(job) holds, checked under the same lock (so a
        worker cannot change the job in between). None if the job is unknown or the predicate fails."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or not predicate(job):
                return None
            for key in fields:
                if not hasattr(job, key):
                    raise AttributeError(f"Job has no field {key!r}")
            for key, value in fields.items():
                setattr(job, key, value)
            self.save()
            return job

    def list(self) -> list[Job]:
        with self._lock:   # snapshot; add_many/remove_* mutate the dict from other threads
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def thumbs_in_use(self) -> set[str]:
        """Thumbnail keys referenced by any job. One probe's key can be shared by several jobs
        (mp4 + mp3, or clips, from one Check), so a key may only be deleted once it is unused."""
        with self._lock:
            return {job.thumb for job in self._jobs.values() if job.thumb}

    def find_active(self, url: str, format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY,
                    subtitles: Optional[str] = None, clip_start: Optional[float] = None,
                    clip_end: Optional[float] = None, clip_exact: bool = False,
                    sponsorblock: bool = False, compat: bool = False,
                    subtitles_mode: str = "embed", split: bool = False,
                    custom_name: Optional[str] = None) -> Optional[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            # Different clips of one video are different downloads, not duplicates; an exact
            # cut of the same range is a different file too.
            if (job.url == url and job.format == format and job.quality == quality
                    and job.subtitles == subtitles and job.clip_start == clip_start
                    and job.clip_end == clip_end and job.clip_exact == clip_exact
                    and job.sponsorblock == sponsorblock and job.compat == compat
                    and job.subtitles_mode == subtitles_mode
                    and job.split == split and job.custom_name == custom_name
                    and job.status in ACTIVE):
                return job
        return None

    def clear(self, keep: Optional[Callable[["Job"], bool]] = None) -> list[Job]:
        """Drop every job except those for which keep(job) is true (evaluated under the lock).
        One save. Returns the jobs that were kept."""
        with self._lock:
            kept = [job for job in self._jobs.values() if keep is not None and keep(job)]
            self._jobs = {job.id: job for job in kept}
            self.save()
            return kept

    def remove(self, job_id: str) -> Optional[Job]:
        """Drop one record and persist. Returns the removed job, or None if unknown. Does not touch files."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is None:
                return None
            self.save()
            return job

    def transition_where(self, predicate, from_status: str, to_status: str, **fields) -> list[Job]:
        """Move every job in `from_status` for which predicate(job) is true to `to_status`,
        also setting any extra `fields` on them. Evaluated under the lock; one save; returns
        the changed jobs."""
        for key in fields:
            if key not in Job.__dataclass_fields__:
                raise AttributeError(f"Job has no field {key!r}")
        with self._lock:
            moved = [job for job in self._jobs.values() if job.status == from_status and predicate(job)]
            for job in moved:
                job.status = to_status
                for key, value in fields.items():
                    setattr(job, key, value)
            if moved:
                self.save()
            return moved

    def requeue_where(self, predicate) -> list[Job]:
        """Reset every FAILED job for which predicate(job) is true back to a fresh queued state
        (same id and metadata; progress 0, error, filename, log, note, priority, now, finished_at,
        stop_requested, duration and the automatic-retry state cleared, so a manual retry gets a
        fresh AUTO_RETRIES budget). Evaluated under the lock; one save; returns the reset jobs.
        Callers must enqueue the returned ids."""
        with self._lock:
            reset = [job for job in self._jobs.values() if job.status == FAILED and predicate(job)]
            for job in reset:
                job.status, job.progress, job.error, job.filename = QUEUED, 0, None, None
                job.speed = job.eta = None
                job.needs_login = job.priority = job.now = False
                job.log = job.note = None
                job.attempts, job.retry_at = 0, None
                job.finished_at = job.duration = None
                job.stop_requested = False
            if reset:
                self.save()
            return reset

    def remove_where(self, predicate) -> list[Job]:
        """Drop every job for which predicate(job) is true, evaluating the predicate under the
        lock so a status change racing with the removal cannot slip through. One save."""
        with self._lock:
            removed = [job for job in self._jobs.values() if predicate(job)]
            for job in removed:
                del self._jobs[job.id]
            if removed:
                self.save()
            return removed

    # -- startup -----------------------------------------------------------

    def recover_after_restart(self) -> list[Job]:
        """Fail jobs that were mid-download; return queued jobs to re-enqueue, "download
        next" jobs first, then oldest first (the order the user expects them to run in).
        retry_at is left alone: the job simply runs now, and the worker clears it on start.
        now ("download now") is left alone too, so such a job still bypasses the window."""
        with self._lock:
            requeue: list[Job] = []
            for job in self._jobs.values():
                if job.status == DOWNLOADING:
                    job.status = FAILED
                    job.error = "interrupted by restart"
                    job.speed = job.eta = None
                    job.priority = False
                    job.finished_at = _now_iso()
                elif job.status == QUEUED:
                    requeue.append(job)
            self.save()
            return sorted(requeue, key=lambda j: (not j.priority, j.created_at))
