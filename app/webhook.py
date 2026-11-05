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
