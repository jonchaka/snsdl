"""Cookie files for login-walled sites (Instagram/Facebook private posts, members-only videos).

Three sources, all Netscape cookies.txt format, all mode 600:
- legacy: settings.cookies_file (data/config/cookies.txt), managed by hand;
- kept:   data/config/cookies/<id>.txt, added through the page, deletable there;
- once:   data/tmp/cookies-once-<job_id>.txt, used by that one job and discarded afterwards.

yt-dlp rewrites the cookie jar it is given when it exits, so every run gets its own
temporary merge of the sources; the stored files are never modified. Cookie values are
parsed only to validate the file and never logged or returned by the API.
"""
import os
import re
import secrets
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .config import Settings

HTTPONLY_PREFIX = "#HttpOnly_"
MAX_COOKIES_TEXT = 1024 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LEGACY_ID = "legacy"


class CookieFormatError(ValueError):
    """The text is not a usable Netscape cookies file; str(exc) says why in one line."""


@dataclass(frozen=True)
class Cookie:
    domain: str
    name: str
    expires: int   # unix seconds; 0 = session cookie


def parse_netscape(text: str) -> list[Cookie]:
    """Validate a Netscape cookies.txt export and return its cookies (without values)."""
    if len(text) > MAX_COOKIES_TEXT:
        raise CookieFormatError("cookies file is larger than 1 MiB")
    cookies: list[Cookie] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if line.startswith(HTTPONLY_PREFIX):
            line = line[len(HTTPONLY_PREFIX):]
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise CookieFormatError(f"line {number}: expected 7 tab-separated fields (Netscape cookies.txt format)")
        domain, _flag, _path, _secure, expires, name, _value = fields
        try:
            expiry = int(float(expires)) if expires.strip() else 0
        except ValueError:
            raise CookieFormatError(f"line {number}: expiry {expires!r} is not a number") from None
        if not domain.strip() or not name.strip():
            raise CookieFormatError(f"line {number}: empty domain or cookie name")
        cookies.append(Cookie(domain=domain.strip(), name=name.strip(), expires=max(expiry, 0)))
    if not cookies:
        raise CookieFormatError("no cookies found; export the file in Netscape cookies.txt format")
    return cookies


def site_of(domain: str) -> str:
    d = domain.lower().lstrip(".")
    return d[4:] if d.startswith("www.") else d


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


@dataclass
class CookieEntry:
    id: str
    source: str            # kept | legacy | once
    sites: list[str]
    count: int
    added_at: str
    expires_at: Optional[str]
    expired: bool

    def to_dict(self) -> dict:
        return asdict(self)


def entry_from_cookies(entry_id: str, source: str, cookies: list[Cookie], added_at: float,
                       now: Optional[float] = None) -> CookieEntry:
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    persistent = [c.expires for c in cookies if c.expires]
    soonest = min(persistent) if persistent else None
    return CookieEntry(
        id=entry_id, source=source,
        sites=sorted({site_of(c.domain) for c in cookies}),
        count=len(cookies), added_at=_iso(added_at),
        expires_at=_iso(soonest) if soonest else None,
        expired=bool(persistent) and max(persistent) < now,
    )


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    path.chmod(0o600)   # in case the file pre-existed with wider permissions


class CookieStore:
    """Files only, no index: the directory is the truth."""

    def __init__(self, settings: Settings):
        self.dir = settings.data_dir / "config" / "cookies"
        self.legacy = settings.cookies_file
        self.tmp = settings.tmp_dir

    def once_path(self, job_id: str) -> Path:
        return self.tmp / f"cookies-once-{job_id}.txt"

    def _entry(self, path: Path, entry_id: str, source: str) -> CookieEntry:
        try:
            cookies = parse_netscape(path.read_text(encoding="utf-8", errors="replace"))
        except (CookieFormatError, OSError):
            cookies = []
        try:
            added = path.stat().st_mtime
        except OSError:
            added = 0.0
        return entry_from_cookies(entry_id, source, cookies, added)

    def add(self, text: str, keep: bool, job_id: Optional[str] = None) -> CookieEntry:
        cookies = parse_netscape(text)   # raises CookieFormatError
        if keep:
            entry_id = secrets.token_urlsafe(6)
            path = self.dir / f"{entry_id}.txt"
            source = "kept"
        else:
            if not job_id:
                raise ValueError("use-once cookies need a job id")
            entry_id, path, source = job_id, self.once_path(job_id), "once"
        _write_private(path, text)
        return entry_from_cookies(entry_id, source, cookies, path.stat().st_mtime)

    def kept_files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        files = [p for p in self.dir.glob("*.txt") if p.is_file() and ID_PATTERN.match(p.stem)]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def entries(self) -> list[CookieEntry]:
        """Kept entries (newest first) then the legacy file, if any. Use-once files are not listed."""
        entries = [self._entry(p, p.stem, "kept") for p in self.kept_files()]
        if self.legacy.is_file():
            entries.append(self._entry(self.legacy, LEGACY_ID, "legacy"))
        return entries

    def delete(self, entry_id: str) -> bool:
        if not ID_PATTERN.match(entry_id) or entry_id == LEGACY_ID:
            return False
        path = self.dir / f"{entry_id}.txt"
        if not path.is_file():
            return False
        path.unlink()
        return True

    def discard_once(self, job_id: str) -> None:
        self.once_path(job_id).unlink(missing_ok=True)

    def sources(self, job_id: Optional[str] = None) -> list[Path]:
        files = [self.legacy] if self.legacy.is_file() else []
        files += self.kept_files()
        if job_id and self.once_path(job_id).is_file():
            files.append(self.once_path(job_id))
        return files

    @contextmanager
    def merged_for(self, tag: str, job_id: Optional[str] = None) -> Iterator[Optional[Path]]:
        """Yield a private temporary cookies file for one yt-dlp run (None when there is nothing):
        a plain copy when a single source exists, otherwise all sources concatenated."""
        files = self.sources(job_id)
        if not files:
            yield None
            return
        # Name must not start with a job id: downloader globs "{job_id}-*" for outputs/partials.
        merged = self.tmp / f"cookies-{tag}.txt"
        self.tmp.mkdir(parents=True, exist_ok=True)
        try:
            if len(files) == 1:
                shutil.copyfile(files[0], merged)
                merged.chmod(0o600)
            else:
                parts = ["# Netscape HTTP Cookie File\n"]
                for f in files:
                    body = f.read_text(encoding="utf-8", errors="replace")
                    parts.append(body if body.endswith("\n") else body + "\n")
                _write_private(merged, "".join(parts))
            yield merged
        finally:
            merged.unlink(missing_ok=True)


@contextmanager
def cookies_for(settings: Settings, tag: str, job_id: Optional[str] = None) -> Iterator[Optional[Path]]:
    """Convenience wrapper: CookieStore(settings).merged_for(tag, job_id)."""
    with CookieStore(settings).merged_for(tag, job_id) as path:
        yield path
