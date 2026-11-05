"""Finish webhook: one signed JSON POST when a job ends (done or failed).

The target comes from WEBHOOK_URL only (never from the browser). WEBHOOK_SECRET is used as the
HMAC key and is never logged or returned; the URL itself may carry a token, so only its host is
ever logged.
"""
import hmac
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from .config import Settings
from .jobs import Job

log = logging.getLogger(__name__)

WEBHOOK_RETRY = 30.0   # seconds before the single retry of a failed send
TIMEOUT = 5            # seconds per POST
PENDING_CAP = 100      # undelivered webhooks kept for a resend after a restart


def file_url(job: Job, settings: Settings) -> Optional[str]:
    """Absolute link when PUBLIC_URL is set, else the server-relative path; None without a file."""
    if not job.filename:
        return None
    path = "/files/" + quote(job.filename, safe="")
    return settings.public_url + path if settings.public_url else path


def payload(event: str, job: Job, settings: Settings) -> bytes:
    return json.dumps({
        "event": event,
        "job": job.to_dict(public=True),
        "file_url": file_url(job, settings),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).encode()


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, "sha256").hexdigest()


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except ValueError:
        return "?"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would otherwise follow 301/302/303 on a POST by switching
    to a GET at whatever host the Location names, keeping the signature headers. With this
    handler any 3xx surfaces as an HTTPError, i.e. a failed send."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def send(settings: Settings, body: bytes, event: str, opener=None) -> bool:
    """Blocking. POST body to WEBHOOK_URL; True on a 2xx answer, False on anything else
    (logged at WARNING with the host only: no body, URL path/query or secret). Redirects are
    not followed. `opener` is a urlopen-like callable (tests); default: a no-redirect opener."""
    url = settings.webhook_url
    if not url:
        return False
    host = _host(url)
    open_fn = opener if opener is not None else _OPENER.open
    try:
        headers = {"Content-Type": "application/json", "X-SNS-Event": event}
        if settings.webhook_secret:
            headers["X-SNS-Signature"] = sign(settings.webhook_secret, body)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with open_fn(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", None)
    except Exception as exc:  # noqa: BLE001 - a webhook must never break the worker
        if isinstance(exc, urllib.error.HTTPError):
            try:
                exc.close()   # an HTTPError carries the open response
            except Exception:  # noqa: BLE001
                pass
        log.warning("webhook to %s failed: %s", host, _describe(exc))
        return False
    if status is None or not 200 <= status < 300:
        log.warning("webhook to %s failed: HTTP %s", host, status)
        return False
    return True


def _describe(exc: Exception) -> str:
    """One-line reason without anything URL-shaped from the exception: str() of an arbitrary
    exception may contain the URL, so only urllib's own errors say more than the type name."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"{type(exc).__name__}: {exc.reason}"
    return type(exc).__name__


class PendingStore:
    """Webhooks whose retry also failed, persisted (temp file + replace) so they can be sent
    once more after a restart. Entries are {"job_id", "event", "body"} with the body as UTF-8
    text; the oldest are dropped beyond PENDING_CAP. A missing or unreadable file reads as
    empty and is overwritten by the next write."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> list[dict]:
        with self._lock:
            return self._read()

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text() or "[]")
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("pending webhooks file %s unreadable (%s); ignoring it", self._path.name, exc)
            return []
        if not isinstance(data, list):
            log.warning("pending webhooks file %s is not a list; ignoring it", self._path.name)
            return []
        return [{"job_id": e["job_id"], "event": e["event"], "body": e["body"]} for e in data
                if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("job_id", "event", "body"))]

    def _write(self, entries: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        tmp.replace(self._path)

    def add(self, job_id: str, event: str, body: bytes) -> None:
        with self._lock:
            entries = self._read()
            entries.append({"job_id": job_id, "event": event,
                            "body": body.decode("utf-8", errors="replace")})
            self._write(entries[-PENDING_CAP:])

    def remove(self, job_id: str, event: str) -> None:
        with self._lock:
            entries = self._read()
            kept = [e for e in entries if not (e["job_id"] == job_id and e["event"] == event)]
            if len(kept) != len(entries):
                self._write(kept)

    def clear(self) -> None:
        with self._lock:
            self._write([])
