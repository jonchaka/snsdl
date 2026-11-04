from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

QUEUED = "queued"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"
ACTIVE = {QUEUED, DOWNLOADING}

FORMATS = ("mp4", "mp3")
MP3_QUALITIES = ("best", "320", "192")
DEFAULT_FORMAT = "mp4"
DEFAULT_QUALITY = "best"


def check_format_quality(fmt: str, quality: str) -> None:
    """Raise ValueError if (fmt, quality) is not an allowed combination."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if fmt == "mp3":
        ok = quality in MP3_QUALITIES
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(**d)


class JobStore:
    """In-memory job table persisted to a JSON file after every mutation."""

    def __init__(self, path: Path):
        self._path = path
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

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

    # -- CRUD --------------------------------------------------------------

    def add(self, url: str, format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY,
            playlist: Optional[str] = None, playlist_index: Optional[int] = None,
            title: Optional[str] = None, playlist_id: Optional[str] = None) -> Job:
        return self.add_many([{"url": url, "format": format, "quality": quality,
                               "playlist": playlist, "playlist_index": playlist_index,
                               "title": title, "playlist_id": playlist_id}])[0]

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

    def list(self) -> list[Job]:
        with self._lock:   # snapshot; add_many/remove_* mutate the dict from other threads
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def find_active(self, url: str, format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY) -> Optional[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if (job.url == url and job.format == format and job.quality == quality
                    and job.status in ACTIVE):
                return job
        return None

    def clear(self) -> None:
        with self._lock:
            self._jobs = {}
            self.save()

    def remove(self, job_id: str) -> Optional[Job]:
        """Drop one record and persist. Returns the removed job, or None if unknown. Does not touch files."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is None:
                return None
            self.save()
            return job

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
        """Fail jobs that were mid-download; return queued jobs to re-enqueue."""
        with self._lock:
            requeue: list[Job] = []
            for job in self._jobs.values():
                if job.status == DOWNLOADING:
                    job.status = FAILED
                    job.error = "interrupted by restart"
                elif job.status == QUEUED:
                    requeue.append(job)
            self.save()
            return requeue
