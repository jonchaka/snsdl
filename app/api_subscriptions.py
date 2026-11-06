"""Subscription routes and the per-subscription podcast feed."""
import asyncio
import hmac
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import auth
from . import feed as feed_mod
from . import files
from . import subscriptions as subs_mod
from . import thumbs
from .api_common import SUBTITLES_PATTERN, _background, _disk_guard, _require_csrf, _validate_url
from .api_jobs import _finished_files
from .jobs import DEFAULT_FORMAT, DEFAULT_QUALITY, DONE, check_format_quality

router = APIRouter()


class SubscriptionIn(BaseModel):
    url: str
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    subtitles: Optional[str] = Field(default=None, pattern=SUBTITLES_PATTERN)
    limit: int = Field(default=subs_mod.DEFAULT_LIMIT, ge=subs_mod.MIN_LIMIT, le=subs_mod.MAX_LIMIT)
    include_existing: bool = False
    auto_keep: bool = False
    backfill: bool = False   # catch up on older entries, `limit` per check

    @field_validator("url")
    @classmethod
    def must_be_http_url(cls, value: str) -> str:
        return _validate_url(value)

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        if self.format == "images":
            raise ValueError("subscriptions download videos or audio")
        if self.subtitles and self.format != "mp4":
            raise ValueError("subtitles apply to mp4 downloads only")
        if self.subtitles and self.subtitles.removeprefix("auto:").lower() == "all":
            raise ValueError("subtitles must name one language")
        return self


def _subscription_or_404(request: Request, sub_id: str):
    sub = request.app.state.subscriptions.get(sub_id)
    if sub is None:
        raise HTTPException(status_code=404)
    return sub


def _feed_secret(request: Request) -> bytes:
    """The per-install secret the feed keys derive from (read once, cached; never logged)."""
    secret = getattr(request.app.state, "feed_secret", None)
    if secret is None:
        secret = request.app.state.feed_secret = auth.secret_for(request.app.state.settings)
    return secret


def _sub_dict(request: Request, sub) -> dict:
    d = sub.to_dict()
    d["feed"] = feed_mod.feed_path(sub.id, feed_mod.feed_key(_feed_secret(request), sub.id))
    return d


@router.get("/api/subscriptions")
async def list_subscriptions(request: Request):
    return {"subscriptions": [_sub_dict(request, s) for s in request.app.state.subscriptions.list()]}


@router.post("/api/subscriptions", status_code=201)
async def add_subscription(body: SubscriptionIn, request: Request):
    """Subscribe to a channel or playlist; the first check runs in the background right away."""
    _require_csrf(request)
    state = request.app.state
    if body.auto_keep and state.settings.keep_dir is None:
        raise HTTPException(status_code=422, detail="keep automatically needs KEEP_DIR")
    _disk_guard(request)
    sub = state.subscriptions.add(body.url, body.format, body.quality, subtitles=body.subtitles,
                                  limit=body.limit, include_existing=body.include_existing,
                                  auto_keep=body.auto_keep, backfill=body.backfill)
    entry = _sub_dict(request, sub)   # snapshot before the background check changes it
    # Module attribute looked up at call time so tests can patch check_one.
    _background(request, asyncio.to_thread(subs_mod.check_one, sub, state.subscriptions, state.store,
                                           state.pool, state.settings))
    return entry


@router.delete("/api/subscriptions/{sub_id}", status_code=204)
async def remove_subscription(sub_id: str, request: Request):
    """Drop the subscription; the jobs it queued stay."""
    _require_csrf(request)
    if not request.app.state.subscriptions.remove(sub_id):
        raise HTTPException(status_code=404)
    return Response(status_code=204)


@router.post("/api/subscriptions/{sub_id}/check")
async def check_subscription(sub_id: str, request: Request):
    """Check now (in a thread) and queue what is new: {"queued", "error"}."""
    _require_csrf(request)
    state = request.app.state
    sub = _subscription_or_404(request, sub_id)
    _disk_guard(request)
    try:
        return await asyncio.to_thread(subs_mod.check_one, sub, state.subscriptions, state.store,
                                       state.pool, state.settings, blocking=False)
    except subs_mod.CheckRunning:
        raise HTTPException(status_code=409, detail="A check is already running")


async def _set_subscription_enabled(request: Request, sub_id: str, enabled: bool) -> dict:
    _require_csrf(request)
    _subscription_or_404(request, sub_id)
    sub = request.app.state.subscriptions.update(sub_id, enabled=enabled)
    if sub is None:   # removed in between
        raise HTTPException(status_code=404)
    return _sub_dict(request, sub)


@router.post("/api/subscriptions/{sub_id}/pause")
async def pause_subscription(sub_id: str, request: Request):
    """Scheduled checks skip a paused subscription (Check now still works)."""
    return await _set_subscription_enabled(request, sub_id, False)


@router.post("/api/subscriptions/{sub_id}/resume")
async def resume_subscription(sub_id: str, request: Request):
    return await _set_subscription_enabled(request, sub_id, True)


NO_STORE = {"Cache-Control": "no-store"}


def _feed_sub_or_404(request: Request, sub_id: str, key: str):
    """The subscription when `key` is its feed key; 404 on any mismatch (the key is the
    credential: these routes are exempt from the PIN)."""
    sub = request.app.state.subscriptions.get(sub_id)
    expected = feed_mod.feed_key(_feed_secret(request), sub_id)
    if not hmac.compare_digest(key.encode(), expected.encode()) or sub is None:
        raise HTTPException(status_code=404)
    return sub


def _feed_jobs(request: Request, sub_id: str) -> list:
    """The subscription's finished downloads; photo posts are left out (podcast apps cannot
    play them)."""
    return [job for job in request.app.state.store.list()
            if job.status == DONE and job.subscription_id == sub_id and job.filename
            and job.format != "images"]


@router.get("/feed/{sub_id}/{key}.xml")
async def feed_document(sub_id: str, key: str, request: Request):
    sub = _feed_sub_or_404(request, sub_id, key)
    settings = request.app.state.settings
    items = _finished_files(settings, _feed_jobs(request, sub_id))
    base = settings.public_url or f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"
    return Response(feed_mod.render(sub, items, base, key), media_type="application/rss+xml; charset=utf-8",
                    headers=NO_STORE)


@router.get("/feed/{sub_id}/{key}/thumbs/{tkey}")
async def feed_thumb(sub_id: str, key: str, tkey: str, request: Request):
    _feed_sub_or_404(request, sub_id, key)
    if not thumbs.KEY_PATTERN.match(tkey) or not any(job.thumb == tkey for job in request.app.state.store.list()
                                                      if job.subscription_id == sub_id):
        raise HTTPException(status_code=404)
    path = thumbs.find(request.app.state.settings.thumbs_dir, tkey)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=thumbs.media_type(path), headers=NO_STORE)


@router.get("/feed/{sub_id}/{key}/{name}")
async def feed_file(sub_id: str, key: str, name: str, request: Request):
    _feed_sub_or_404(request, sub_id, key)
    settings = request.app.state.settings
    for job in _feed_jobs(request, sub_id):
        if job.filename == name:
            path = files.file_path(settings, job)
            if path is not None:
                return FileResponse(path, media_type=feed_mod.mime_for(name), filename=name, headers=NO_STORE)
    raise HTTPException(status_code=404)
