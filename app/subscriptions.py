"""Subscriptions: channels/playlists checked on a schedule; new entries are queued as jobs.

Persisted in data/subscriptions.json. A check expands the URL with the same flat extraction as
/api/playlists, queues the entries not seen before (and not already active), and remembers every
current url in `seen` (newest last, capped at SEEN_CAP).
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import probe, worker
from .config import Settings
from .jobs import JobStore

log = logging.getLogger(__name__)

SEEN_CAP = 5000
DEFAULT_LIMIT = 10
MIN_LIMIT, MAX_LIMIT = 1, 100
LOW_DISK = "low disk space"

# Module-level so tests (and the browser fakes) can patch them.
expander = probe.expand_playlist
CHECK_LOCK = threading.Lock()   # one check at a time (scheduled or manual)


class CheckRunning(Exception):
    """A check is already running and check_one was called with blocking=False."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Subscription:
    id: str
    url: str
    format: str
    quality: str
    subtitles: Optional[str] = None
    title: Optional[str] = None          # playlist/channel title, None until the first check
    limit: int = DEFAULT_LIMIT           # most new entries queued per check (1..100)
    enabled: bool = True                 # False = paused: scheduled checks skip it
    include_existing: bool = False       # first check also queues what is already there
    added_at: str = field(default_factory=_now_iso)
    last_checked: Optional[str] = None   # ISO UTC of the last attempt, failed ones included
    last_error: Optional[str] = None
    last_queued: int = 0
    seen: list[str] = field(default_factory=list)   # urls already handled, newest last
    seeded: bool = False                 # a listing was merged into seen (False: next good check seeds)

    def to_dict(self, public: bool = True) -> dict:
        """public=True (the API shape) drops the potentially long `seen` list and adds
        `seen_count`; public=False is the persisted form."""
        d = asdict(self)
        if public:
            d["seen_count"] = len(d.pop("seen"))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Subscription":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _clamp_limit(limit) -> int:
    return max(MIN_LIMIT, min(MAX_LIMIT, int(limit)))


class SubscriptionStore:
    """In-memory subscription table persisted to JSON (temp file + replace) after every change."""

    def __init__(self, path: Path):
        self._path = path
        self._subs: dict[str, Subscription] = {}
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            self._subs = {}
            try:
                raw = self._path.read_text()
            except FileNotFoundError:
                return
            try:
                self._subs = {d["id"]: Subscription.from_dict(d) for d in json.loads(raw or "[]")}
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                # Keep the unreadable file aside rather than overwriting it on the next save.
                bad = self._path.with_suffix(".json.bad")
                log.warning("subscriptions file %s unreadable (%s); moved to %s, starting empty",
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
        tmp.write_text(json.dumps([s.to_dict(public=False) for s in self._subs.values()], indent=2))
        tmp.replace(self._path)

    def add(self, url: str, format: str, quality: str, subtitles: Optional[str] = None,
            limit: int = DEFAULT_LIMIT, include_existing: bool = False) -> Subscription:
        with self._lock:
            sub_id = secrets.token_hex(4)
            while sub_id in self._subs:
                sub_id = secrets.token_hex(4)
            sub = Subscription(id=sub_id, url=url, format=format, quality=quality,
                               subtitles=subtitles, limit=_clamp_limit(limit),
                               include_existing=include_existing)
            self._subs[sub_id] = sub
            try:
                self._save()
            except BaseException:
                del self._subs[sub_id]
                raise
            return sub

    def get(self, sub_id: str) -> Optional[Subscription]:
        return self._subs.get(sub_id)

    def list(self) -> list[Subscription]:
        with self._lock:
            subs = list(self._subs.values())
        return sorted(subs, key=lambda s: s.added_at)

    def update(self, sub_id: str, **fields) -> Optional[Subscription]:
        for key in fields:
            if key not in Subscription.__dataclass_fields__:
                raise AttributeError(f"Subscription has no field {key!r}")
        if "limit" in fields:
            fields["limit"] = _clamp_limit(fields["limit"])
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is None:
                return None
            for key, value in fields.items():
                setattr(sub, key, value)
            self._save()
            return sub

    def remove(self, sub_id: str) -> bool:
        """Drop the subscription (its jobs stay). False if unknown."""
        with self._lock:
            if self._subs.pop(sub_id, None) is None:
                return False
            self._save()
            return True


def _merge_seen(seen: list[str], urls: list[str]) -> list[str]:
    """Append the urls not yet in seen, oldest first (yt-dlp lists newest first) so the newest
    ends up last; keep only the last SEEN_CAP."""
    known = set(seen)
    out = list(seen)
    for url in reversed(urls):
        if url not in known:
            known.add(url)
            out.append(url)
    return out[-SEEN_CAP:]


def check_one(sub: Subscription, subs: SubscriptionStore, store: JobStore, pool, settings: Settings,
              expander=None, blocking: bool = True) -> dict:
    """Blocking. Check one subscription now and queue its new entries.

    Returns {"queued": int, "error": str | None}. Raises CheckRunning if another check holds
    CHECK_LOCK and blocking is False (blocking=True waits for it)."""
    if not CHECK_LOCK.acquire(blocking=blocking):
        raise CheckRunning()
    try:
        return _check(sub.id, subs, store, pool, settings, expander or globals()["expander"])
    finally:
        CHECK_LOCK.release()


def _check(sub_id: str, subs: SubscriptionStore, store: JobStore, pool, settings: Settings,
           expand) -> dict:
    sub = subs.get(sub_id)
    if sub is None:   # removed meanwhile
        return {"queued": 0, "error": None}
    # Module-level lookup at call time so tests can patch worker.disk_low.
    if worker.disk_low(settings):
        subs.update(sub_id, last_checked=_now_iso(), last_error=LOW_DISK, last_queued=0)
        return {"queued": 0, "error": LOW_DISK}
    try:
        result = expand(sub.url, settings)
    except probe.ProbeError as exc:
        subs.update(sub_id, last_checked=_now_iso(), last_error=str(exc), last_queued=0)
        return {"queued": 0, "error": str(exc)}
    title = result.get("title") or sub.title
    entries: list[dict] = []
    positions: dict[str, int] = {}
    for index, entry in enumerate(result.get("entries") or [], start=1):
        if entry["url"] not in positions:
            positions[entry["url"]] = index
            entries.append(entry)
    urls = [e["url"] for e in entries]
    items: list[dict] = []
    # Seeding keys on `seeded`, not last_checked: a failed first check (probe error, low disk)
    # records last_checked too, and must not turn the next good check into a bulk queue.
    if sub.seeded or sub.include_existing:
        seen = set(sub.seen)
        for entry in entries:
            if len(items) >= sub.limit:
                break
            url = entry["url"]
            if url in seen or store.find_active(url, sub.format, sub.quality, sub.subtitles):
                continue
            items.append({"url": url, "format": sub.format, "quality": sub.quality,
                          "subtitles": sub.subtitles, "title": entry.get("title"),
                          "playlist": title, "playlist_index": positions[url],
                          "playlist_id": f"sub-{sub_id}", "subscription_id": sub_id})
    # else: first check without include_existing only seeds `seen`.
    jobs = store.add_many(items)
    for job in jobs:
        pool.enqueue(job.id)
    subs.update(sub_id, title=title, seen=_merge_seen(sub.seen, urls), seeded=True,
                last_checked=_now_iso(),
                last_queued=len(jobs), last_error=None)
    if jobs:
        log.info("subscription %s: queued %d new entr%s", sub_id, len(jobs), "y" if len(jobs) == 1 else "ies")
    return {"queued": len(jobs), "error": None}


def check_all(subs: SubscriptionStore, store: JobStore, pool, settings: Settings) -> dict:
    """Blocking. Check every enabled subscription in turn; never raises.
    Returns {"checked", "queued", "failed"} (failed = errors of any kind, incl. probe errors)."""
    out = {"checked": 0, "queued": 0, "failed": 0}
    try:
        enabled = [s for s in subs.list() if s.enabled]
    except Exception:  # noqa: BLE001 - the scheduler must never see an exception
        log.exception("subscriptions: could not list subscriptions")
        return out
    for sub in enabled:
        out["checked"] += 1
        try:
            res = check_one(sub, subs, store, pool, settings, blocking=True)
        except Exception as exc:  # noqa: BLE001 - one bad subscription must not stop the rest
            log.exception("subscription %s: check failed", sub.id)
            out["failed"] += 1
            try:
                subs.update(sub.id, last_checked=_now_iso(), last_error=f"check failed: {exc}"[:200],
                            last_queued=0)
            except Exception:  # noqa: BLE001
                log.exception("subscription %s: could not record the error", sub.id)
            continue
        out["queued"] += res["queued"]
        if res["error"]:
            out["failed"] += 1
    return out
