"""Video thumbnails kept on the server: fetched once from the URL yt-dlp reports, stored as
data/thumbs/<key>.<ext>, served to the page by key. The browser never supplies the URL."""
import logging
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .config import Settings

log = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,16}$")
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 5.0
READ_CHUNK = 64 * 1024
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
               "avif": "image/avif"}
HTTP_PROXY_SCHEMES = ("http", "https")   # all urllib's ProxyHandler can speak


def new_key() -> str:
    """A fresh unguessable key (11 url-safe chars)."""
    return secrets.token_urlsafe(8)


def sniff(data: bytes) -> Optional[str]:
    """File extension from the image's magic bytes; None for anything else (HTML error pages,
    SVG, unknown formats). The Content-Type header is not trusted."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):   # ISO-BMFF box; YouTube search thumbnails
        return "avif"
    return None


def is_http_url(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def _valid_key(key) -> bool:
    return isinstance(key, str) and bool(KEY_PATTERN.fullmatch(key))


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect to ftp://; thumbnails stay on http(s)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_http_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-http URL refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener(settings: Settings):
    """urllib opener routed through settings.proxy. None when the proxy is SOCKS: urllib cannot
    speak it, and going direct would bypass a proxy the operator configured on purpose."""
    handlers: list = [_HttpOnlyRedirects()]
    if settings.proxy:
        if urlsplit(settings.proxy).scheme not in HTTP_PROXY_SCHEMES:
            return None
        handlers.append(urllib.request.ProxyHandler({"http": settings.proxy, "https": settings.proxy}))
    return urllib.request.build_opener(*handlers)


def _read_capped(resp, deadline: float) -> Optional[bytes]:
    """At most MAX_BYTES + 1 bytes in total (Content-Length is not trusted); None when the body
    is larger or the whole read outlives the deadline (the socket timeout is per read)."""
    chunks, total = [], 0
    while total <= MAX_BYTES:
        chunk = resp.read(min(READ_CHUNK, MAX_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if time.monotonic() > deadline:
            return None
    return None if total > MAX_BYTES else b"".join(chunks)


def _write(dest_dir: Path, name: str, data: bytes) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Temp file + rename: a reader (the thumbnail route) never sees a half-written image.
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest_dir / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fetch_thumbnail(url, dest_dir: Path, key: str, settings: Settings, opener=None) -> Optional[str]:
    """Download an image to dest_dir/<key>.<ext>; returns that file name or None. Never raises."""
    try:
        if not is_http_url(url) or not _valid_key(key):
            return None
        if opener is None:
            opener = build_opener(settings)
            if opener is None:
                return None
        deadline = time.monotonic() + 2 * TIMEOUT
        with opener.open(url, timeout=TIMEOUT) as resp:
            data = _read_capped(resp, deadline)
        if data is None:
            log.info("thumbnail %s skipped: larger than %d bytes or too slow", key, MAX_BYTES)
            return None
        ext = sniff(data)
        if ext is None:
            log.info("thumbnail %s skipped: not a JPEG, PNG, WebP, GIF or AVIF", key)
            return None
        name = f"{key}.{ext}"
        _write(Path(dest_dir), name, data)
        return name
    except Exception as exc:  # noqa: BLE001 - a missing thumbnail must never fail a probe or download
        # Only the exception class and host: thumbnail URLs can carry signed tokens.
        host = urlsplit(url).hostname if isinstance(url, str) else None
        log.info("thumbnail %s from %s failed: %s", key, host, exc.__class__.__name__)
        return None


def find(dest_dir: Path, key) -> Optional[Path]:
    """The stored file for `key`, or None (also for a malformed key, so it can't glob)."""
    if not _valid_key(key):
        return None
    try:
        for p in sorted(Path(dest_dir).glob(f"{key}.*")):
            if p.suffix[1:] in MEDIA_TYPES and p.is_file():
                return p
    except OSError:
        pass
    return None


def delete(dest_dir: Path, key) -> None:
    """Remove the file(s) for `key`; a malformed or unknown key is a no-op."""
    if not _valid_key(key):
        return
    try:
        for p in Path(dest_dir).glob(f"{key}.*"):
            p.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete thumbnail %s: %s", key, exc)


def sweep_orphans(dest_dir: Path, referenced_keys: set[str], older_than: float = 3600,
                  now: Optional[float] = None) -> int:
    """Delete files no job references, older than `older_than` seconds (younger ones may belong
    to a probe whose job is about to be added). Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        paths = list(Path(dest_dir).iterdir())
    except OSError:
        return 0
    for p in paths:
        try:
            key = p.name.split(".", 1)[0]
            if not p.is_file() or (key and key in referenced_keys):
                continue
            if p.stat().st_mtime < now - older_than:
                p.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not sweep thumbnail %s: %s", p.name, exc)
    return removed


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(Path(path).suffix[1:].lower(), "application/octet-stream")
