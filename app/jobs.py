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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    url: str
    status: str = QUEUED
    progress: int = 0
    title: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)

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

    def add(self, url: str) -> Job:
        with self._lock:
            job = Job(id=secrets.token_urlsafe(6), url=url)
            self._jobs[job.id] = job
            self.save()
            return job

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
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def find_active(self, url: str) -> Optional[Job]:
        for job in self._jobs.values():
            if job.url == url and job.status in ACTIVE:
                return job
        return None

    def clear(self) -> None:
        with self._lock:
            self._jobs = {}
            self.save()

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
