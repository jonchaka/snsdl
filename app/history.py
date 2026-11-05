"""Download history: one entry per finished download, kept after the job list is cleared.

Persisted in data/history.json (newest first, capped at HISTORY_CAP). Cleanup, retention and
Clear all never touch it; its thumbnails count as in use for every thumbnail sweep.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .jobs import DONE, Job
from .sites import site_of_url

log = logging.getLogger(__name__)

HISTORY_CAP = 1000
SPLIT_PREFIX = "split-"   # playlist_id of the parts of one chapter-split download


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Entry:
    id: str
    url: str
    title: Optional[str] = None
    format: str = "mp4"
    quality: str = "best"
    subtitles: Optional[str] = None
    subtitles_mode: str = "embed"
    compat: bool = False
    sponsorblock: bool = False
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    clip_exact: bool = False
    split: bool = False
    size: Optional[int] = None
    duration: Optional[int] = None
    thumb: Optional[str] = None
    uploader: Optional[str] = None
    site: str = "other"
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def key(self) -> tuple:
        return (self.url, self.format, self.quality, self.subtitles, self.clip_start,
                self.clip_end, self.split)


def _sum(values) -> Optional[int]:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _entry(job: Job, id: str, **overrides) -> Entry:
    fields = dict(url=job.url, title=job.title, format=job.format, quality=job.quality,
                  subtitles=job.subtitles, subtitles_mode=job.subtitles_mode, compat=job.compat,
                  sponsorblock=job.sponsorblock, clip_start=job.clip_start, clip_end=job.clip_end,
                  clip_exact=job.clip_exact, split=job.split, size=job.size, duration=job.duration,
                  thumb=job.thumb, uploader=job.uploader, site=site_of_url(job.url),
                  finished_at=job.finished_at or _now_iso())
    fields.update(overrides)
    return Entry(id=id, **fields)


class HistoryStore:
    """In-memory history persisted to JSON (temp file + replace) after every change."""

    def __init__(self, path: Path):
        self._path = path
        self._entries: list[Entry] = []   # newest first
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            self._entries = []
            try:
                raw = self._path.read_text()
            except FileNotFoundError:
                return
            try:
                self._entries = [Entry.from_dict(d) for d in json.loads(raw or "[]")]
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                # Keep the unreadable file aside rather than overwriting it on the next save.
                bad = self._path.with_suffix(".json.bad")
                log.warning("history file %s unreadable (%s); moved to %s, starting empty",
                            self._path, exc, bad.name)
                try:
                    self._path.replace(bad)
                except OSError as move_exc:
                    log.warning("could not move %s aside (%s); starting empty anyway",
                                self._path, move_exc)

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([e.to_dict() for e in self._entries], indent=2))
        tmp.replace(self._path)

    def _new_id(self) -> str:
        taken = {e.id for e in self._entries}
        while True:
            entry_id = secrets.token_hex(4)
            if entry_id not in taken:
                return entry_id

    def record(self, jobs: list[Job]) -> list[Entry]:
        """Add an entry per done job (the parts of one chapter split become one entry). An
        entry with the same key replaces the older one and moves to the top. Returns the new
        entries in input order; nothing is saved when there are none."""
        done = [job for job in jobs if job.status == DONE]
        groups: dict[str, list[Job]] = {}
        singles: list[tuple[int, Job]] = []
        order: list[tuple[int, str]] = []
        for i, job in enumerate(done):
            pid = job.playlist_id
            if pid and pid.startswith(SPLIT_PREFIX):
                if pid not in groups:
                    groups[pid] = []
                    order.append((i, pid))
                groups[pid].append(job)
            else:
                singles.append((i, job))
        with self._lock:
            made: list[tuple[int, Entry]] = []
            for i, job in singles:
                made.append((i, _entry(job, self._new_id())))
            for i, pid in order:
                parts = sorted(groups[pid], key=lambda j: (j.playlist_index is None, j.playlist_index or 0))
                first = parts[0]
                finished = [p.finished_at for p in parts if p.finished_at]
                made.append((i, _entry(first, self._new_id(), title=first.playlist or first.title,
                                       split=True, size=_sum(p.size for p in parts),
                                       duration=_sum(p.duration for p in parts),
                                       finished_at=max(finished) if finished else _now_iso())))
            made.sort(key=lambda pair: pair[0])
            entries = [entry for _, entry in made]
            if not entries:
                return []
            for entry in entries:
                key = entry.key()
                self._entries = [e for e in self._entries if e.key() != key]
                self._entries.insert(0, entry)
            del self._entries[HISTORY_CAP:]
            self._save()
            return entries

    def list(self, q: str = "", limit: int = 200) -> list[Entry]:
        """Newest first; `q` matches title, url, site or uploader (case-insensitive)."""
        needle = q.strip().lower()
        with self._lock:
            entries = list(self._entries)
        if needle:
            entries = [e for e in entries
                       if any(needle in (v or "").lower() for v in (e.title, e.url, e.site, e.uploader))]
        return entries[:max(0, limit)]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def find(self, url: str) -> Optional[Entry]:
        """The newest entry for `url`, or None."""
        with self._lock:
            return next((e for e in self._entries if e.url == url), None)

    def remove(self, id: str) -> bool:
        with self._lock:
            kept = [e for e in self._entries if e.id != id]
            if len(kept) == len(self._entries):
                return False
            self._entries = kept
            self._save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    def thumbs_in_use(self) -> set[str]:
        with self._lock:
            return {e.thumb for e in self._entries if e.thumb}
