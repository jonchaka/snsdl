"""Finish webhook: one signed JSON POST when a job ends (done or failed).

The target comes from WEBHOOK_URL only (never from the browser). WEBHOOK_SECRET is used as the
HMAC key and is never logged or returned; the URL itself may carry a token, so only its host is
ever logged.
"""
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

from .config import Settings
from .jobs import Job

log = logging.getLogger(__name__)

WEBHOOK_RETRY = 30.0   # seconds before the single retry of a failed send
TIMEOUT = 5            # seconds per POST


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


def send(settings: Settings, body: bytes, event: str, opener=urllib.request.urlopen) -> bool:
    """Blocking. POST body to WEBHOOK_URL; True on a 2xx answer, False on anything else
    (logged at WARNING with the host only: no body, URL path/query or secret)."""
    url = settings.webhook_url
    if not url:
        return False
    host = _host(url)
    try:
        headers = {"Content-Type": "application/json", "X-SNS-Event": event}
        if settings.webhook_secret:
            headers["X-SNS-Signature"] = sign(settings.webhook_secret, body)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with opener(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", None)
        if status is None or not 200 <= status < 300:
            raise OSError(f"HTTP {status}")
        return True
    except Exception as exc:  # noqa: BLE001 - a webhook must never break the worker
        # str(HTTPError/URLError) does not include the URL; keep it that way.
        log.warning("webhook to %s failed: %s", host, _describe(exc))
        return False


def _describe(exc: Exception) -> str:
    """One-line reason without anything URL-shaped from the exception."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"{type(exc).__name__}: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"
