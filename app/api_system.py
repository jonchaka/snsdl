"""Probe, search, cookies, status, events, certificate, update, cleanup, PIN and Telegram test routes."""
import asyncio
import os
import secrets
import shutil
from typing import Optional

import yt_dlp
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from . import auth
from . import cleanup as cleanup_mod
from . import cookies as cookies_mod
from . import downloader as downloader_mod
from . import events as events_mod
from . import images as images_mod
from . import probe as probe_mod
from . import telegram as telegram_mod
from . import tls
from . import worker as worker_mod
from .api_common import ProbeIn, _disk_guard, _public_jobs, _require_csrf
from .api_history import PREVIOUS_FIELDS
from .jobs import FAILED, PAUSED, QUEUED, JobStore

router = APIRouter()


class CookiesIn(BaseModel):
    text: str          # size is checked in the route: pydantic's own 422 would echo the whole text back
    keep: bool = False
    job_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,32}$")


@router.get("/api/cookies")
async def list_cookies(request: Request):
    """Metadata only (sites, counts, dates) — cookie names and values never leave the server."""
    _require_csrf(request)
    return {"entries": [e.to_dict() for e in cookies_mod.CookieStore(request.app.state.settings).entries()]}


@router.post("/api/cookies", status_code=201)
async def add_cookies(body: CookiesIn, request: Request):
    """Store a Netscape cookies.txt export: kept for every future download, or use-once for `job_id`.
    A failed `job_id` is re-queued straight away (same as Retry)."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if len(body.text) > cookies_mod.MAX_COOKIES_TEXT:
        raise HTTPException(status_code=422, detail="cookies file is larger than 1 MiB")
    if not body.keep and not body.job_id:
        raise HTTPException(status_code=422, detail="use-once cookies need the job they are for")
    if body.job_id:
        job = store.get(body.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        if not body.keep and job.status not in (FAILED, QUEUED, PAUSED):
            raise HTTPException(status_code=409, detail="That download is already running or finished")
        if job.status == FAILED:   # it would be re-queued below
            _disk_guard(request)
    try:
        entry = cookies_mod.CookieStore(request.app.state.settings).add(body.text, keep=body.keep, job_id=body.job_id)
    except cookies_mod.CookieFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    retried = False
    if body.job_id:
        reset = store.requeue_where(lambda job: job.id == body.job_id)   # only if it is failed
        if reset:
            request.app.state.pool.enqueue(body.job_id)
            retried = True
    return {"entry": entry.to_dict(), "retried": retried}


@router.delete("/api/cookies/{entry_id}", status_code=204)
async def delete_cookies(entry_id: str, request: Request):
    _require_csrf(request)
    if not cookies_mod.CookieStore(request.app.state.settings).delete(entry_id):
        raise HTTPException(status_code=404)
    return Response(status_code=204)


@router.post("/api/probe")
async def probe_url(body: ProbeIn, request: Request):
    _require_csrf(request)
    try:
        result = await asyncio.to_thread(probe_mod.probe, body.url, request.app.state.settings)
    except probe_mod.ProbeError as exc:
        # needs_login lets the page offer "Add cookies" when the site wants a logged-in session.
        return JSONResponse({"detail": str(exc), "needs_login": exc.needs_login}, status_code=422)
    entry = request.app.state.history.find(body.url)
    previous = None
    if entry is not None:
        d = entry.to_dict()
        previous = {k: d.get(k) for k in PREVIOUS_FIELDS}
    return {**result, "previous": previous}


def downloads_usage(*dirs) -> dict:
    """Count and total size of regular files in `dirs` (None entries skipped); files removed
    mid-scan are simply skipped."""
    count = total = 0
    for directory in dirs:
        if directory is None:
            continue
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except FileNotFoundError:
            pass
    return {"files": count, "bytes": total}


@router.get("/api/status")
def status(request: Request):
    # Plain def: FastAPI runs it in the threadpool, so the directory scan never blocks the
    # event loop.
    return status_payload(request)


def status_payload(request: Request) -> dict:
    """The /api/status body, shared with the event stream. Blocking (scans the download disk)."""
    settings = request.app.state.settings
    try:
        free = shutil.disk_usage(settings.downloads_dir).free
    except OSError:
        free = None
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "downloads": downloads_usage(settings.downloads_dir, settings.keep_dir),
        "queue_paused": request.app.state.pool.paused,
        "rate_limit": settings.rate_limit,
        "impersonate": {"target": settings.impersonate,
                        "active": downloader_mod.target_supported(settings.impersonate)},
        "update": {k: v for k, v in request.app.state.updater.status.to_dict().items() if k != "running"},
        "disk": {"free": free, "min_free": settings.min_free_space, "low": worker_mod.disk_low(settings)},
        "proxy": settings.proxy_display,
        "pacing": {"max_per_site": settings.max_per_site, "site_gap": settings.site_gap},
        "keep_dir": bool(settings.keep_dir),
        "https": settings.https,
        "cert_hosts": request.app.state.cert_hosts,
        "window": _window_status(settings),
        "webhook": bool(settings.webhook_url),   # never the URL or the secret
        "telegram": settings.telegram,            # never the token or the chat id
        "subscriptions": {"count": len(request.app.state.subscriptions.list()),
                          "cron": settings.subscription_cron or None},
        "defaults": {"sponsorblock": settings.sponsorblock, "prefer_h264": settings.prefer_h264},
        "retention_days": settings.retention_days,
        "live_max_hours": settings.live_max_hours,
        "recordings": request.app.state.pool.recordings,
        "media_layout": settings.media_layout,
        "public_url": settings.public_url,   # the configured origin only
        "history": {"count": request.app.state.history.count()},
        "images": {"sites": sorted(images_mod.SITES)},   # photo-post sites (gallery-dl categories)
    }


@router.post("/api/telegram/test")
async def telegram_test(request: Request):
    """Send a fixed test message to the configured chat; 404 when Telegram is not set up."""
    _require_csrf(request)
    settings = request.app.state.settings
    if not settings.telegram:
        raise HTTPException(status_code=404, detail="Telegram is not configured")
    req = telegram_mod.Request("sendMessage", {"text": telegram_mod.TEST_TEXT}, {})
    return {"ok": await asyncio.to_thread(telegram_mod.send, settings, req)}


@router.get("/api/events")
async def job_events(request: Request):
    """Server-Sent Events: `jobs` on every change (coalesced), `status` every few seconds."""
    stream = events_mod.stream(request, request.app.state.events, lambda: _public_jobs(request),
                               lambda: status_payload(request))
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _window_status(settings) -> Optional[dict]:
    w = settings.download_window
    # worker.now_fn: the same clock the worker uses, so a patched time affects both.
    return None if w is None else w.to_dict(worker_mod.now_fn(settings))


@router.get("/cert.pem")
@router.get("/cert.crt")
def ca_certificate(request: Request):
    """The CA certificate a phone installs once to trust HTTPS mode. Only ca.pem: never a key
    or the server certificate. Exempt from the PIN like the page (auth.is_exempt)."""
    settings = request.app.state.settings
    if not settings.https:
        raise HTTPException(status_code=404, detail="HTTPS is off")
    path = settings.tls_dir / tls.CA_CERT
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Certificate not found")
    return FileResponse(path, media_type="application/x-x509-ca-cert", filename="sns-downloader-ca.crt",
                        headers={"Cache-Control": "no-store"})


@router.post("/api/update")
async def update_yt_dlp(request: Request):
    """Install the newest yt-dlp into data/lib now; restart when idle if the version changed."""
    _require_csrf(request)
    updater = request.app.state.updater
    status_ = await asyncio.to_thread(updater.update)
    updater.schedule_restart()
    return status_.to_dict()


@router.post("/api/cleanup")
async def trigger_cleanup(request: Request):
    _require_csrf(request)
    removed = cleanup_mod.run_cleanup(request.app.state.store, request.app.state.settings,
                                      history=getattr(request.app.state, "history", None))
    return {"removed": removed}


MAX_SEARCH_CHARS = 200


class SearchIn(BaseModel):
    q: str = Field(min_length=1, max_length=400)   # raw cap, before stripping

    @field_validator("q")
    @classmethod
    def sane_query(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= MAX_SEARCH_CHARS:
            raise ValueError(f"search text must be 1 to {MAX_SEARCH_CHARS} characters")
        return value


@router.post("/api/search")
async def search(body: SearchIn, request: Request):
    """YouTube search: the first probe.SEARCH_RESULTS hits with server-fetched thumbnails."""
    _require_csrf(request)
    try:
        results = await asyncio.to_thread(probe_mod.search, body.q, request.app.state.settings)
    except probe_mod.ProbeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return {"results": results}


AUTH_FAIL_DELAY = 1.0   # seconds a wrong PIN waits before its 401 (tests set 0)
AUTH_MAX_AGE = 31536000  # one year: the PIN is asked once per browser


class AuthIn(BaseModel):
    pin: str = Field(max_length=256)


def _expected_token(request: Request) -> str:
    return auth.token(request.app.state.secret, request.app.state.settings.access_pin)


@router.get("/api/auth")
async def auth_status(request: Request):
    """Whether a PIN is required and whether this browser's cookie passes. Never 401."""
    if not request.app.state.settings.access_pin:
        return {"required": False, "ok": True}
    cookie = request.cookies.get(auth.COOKIE, "")
    ok = bool(cookie) and secrets.compare_digest(cookie.encode(), _expected_token(request).encode())
    return {"required": True, "ok": ok}


@router.post("/api/auth", status_code=204)
async def auth_login(body: AuthIn, request: Request):
    _require_csrf(request)
    settings = request.app.state.settings
    if not settings.access_pin:
        return Response(status_code=204)
    limiter: auth.FailureLimiter = request.app.state.auth_limiter
    addr = request.client.host if request.client else "unknown"
    if limiter.blocked(addr):   # before comparing: a blocked address learns nothing
        raise HTTPException(status_code=429, detail="Too many attempts, wait a minute")
    if not secrets.compare_digest(body.pin.encode(), settings.access_pin.encode()):
        limiter.hit(addr)
        await asyncio.sleep(AUTH_FAIL_DELAY)
        raise HTTPException(status_code=401, detail="Wrong PIN")
    limiter.clear(addr)
    response = Response(status_code=204)
    response.set_cookie(auth.COOKIE, _expected_token(request), httponly=True, samesite="lax", path="/",
                        max_age=AUTH_MAX_AGE, secure=(request.url.scheme == "https"))
    return response


@router.delete("/api/auth", status_code=204)
async def auth_logout(request: Request):
    _require_csrf(request)
    response = Response(status_code=204)
    response.delete_cookie(auth.COOKIE, path="/", httponly=True, samesite="lax",
                           secure=(request.url.scheme == "https"))
    return response
