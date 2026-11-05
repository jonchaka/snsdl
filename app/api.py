import asyncio
import os
import re
import secrets
import shutil
from typing import Literal, Optional
from urllib.parse import quote, urlparse

import yt_dlp
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import cleanup as cleanup_mod
from . import cookies as cookies_mod
from . import downloader as downloader_mod
from . import probe as probe_mod
from . import thumbs
from . import worker as worker_mod
from . import zipstream
from .jobs import ACTIVE, DEFAULT_FORMAT, DEFAULT_QUALITY, DONE, FAILED, PAUSED, QUEUED, JobStore, check_format_quality

router = APIRouter()

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "sns-downloader"


def _require_csrf(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403)


def _human(n: float) -> str:
    """Binary units, one decimal: 629145600 -> "600.0 MB"."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _disk_guard(request: Request) -> None:
    """507 when the download disk is below MIN_FREE_SPACE: refuse new work up front rather than
    queue jobs that would only wait (the worker holds them until space is freed)."""
    settings = request.app.state.settings
    if not worker_mod.disk_low(settings):   # looked up at call time so tests can patch it
        return
    try:
        free = _human(shutil.disk_usage(settings.downloads_dir).free)
    except OSError:
        free = "too little space"
    raise HTTPException(status_code=507, detail=f"Only {free} free on the download disk; "
                                                f"downloads need at least {_human(settings.min_free_space)}")


def _validate_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must start with http:// or https:// and include a host")
    return value


class ProbeIn(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def must_be_http_url(cls, value: str) -> str:
        return _validate_url(value)


# A language key as the probe reports it, optionally prefixed "auto:". No leading "-" and never
# "all": yt-dlp reads those as its discard syntax / every-language alias.
SUBTITLES_PATTERN = r"^(auto:)?[A-Za-z0-9][A-Za-z0-9._-]{0,19}$"


MAX_CLIP_SECONDS = 9_999_999   # 7 digits, about 115 days


class JobIn(ProbeIn):
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    subtitles: Optional[str] = Field(default=None, pattern=SUBTITLES_PATTERN)
    clip_start: Optional[float] = Field(default=None, ge=0, le=MAX_CLIP_SECONDS)   # seconds
    clip_end: Optional[float] = Field(default=None, ge=0, le=MAX_CLIP_SECONDS)
    thumb: Optional[str] = Field(default=None, pattern=thumbs.KEY_PATTERN.pattern)   # key from /api/probe

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        if self.subtitles and self.format != "mp4":
            raise ValueError("subtitles can only be embedded in mp4 downloads")
        if self.subtitles and self.subtitles.removeprefix("auto:").lower() == "all":
            raise ValueError("subtitles must name one language")
        if self.clip_start is not None and self.clip_end is not None and self.clip_end <= self.clip_start:
            raise ValueError("clip end must be after clip start")
        return self


class PlaylistIn(JobIn):
    limit: int = Field(ge=1)

    @model_validator(mode="after")
    def no_clips(self):
        if self.clip_start is not None or self.clip_end is not None:
            raise ValueError("clips apply to single videos")
        return self


@router.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    _disk_guard(request)
    store: JobStore = request.app.state.store
    settings = request.app.state.settings
    if body.thumb is not None and thumbs.find(settings.thumbs_dir, body.thumb) is None:
        raise HTTPException(status_code=422, detail="Unknown thumbnail")
    existing = store.find_active(body.url, body.format, body.quality, body.subtitles,
                                 body.clip_start, body.clip_end)
    if existing is not None:
        return JSONResponse(existing.to_dict(public=True), status_code=200)
    job = store.add(body.url, format=body.format, quality=body.quality, subtitles=body.subtitles,
                    clip_start=body.clip_start, clip_end=body.clip_end, thumb=body.thumb)
    request.app.state.pool.enqueue(job.id)
    return JSONResponse(job.to_dict(public=True), status_code=201)


@router.post("/api/playlists", status_code=201)
async def create_playlist_jobs(body: PlaylistIn, request: Request):
    """Expand a playlist and queue one job per entry (up to `limit` and MAX_PLAYLIST_ITEMS)."""
    _require_csrf(request)
    _disk_guard(request)
    settings = request.app.state.settings
    store: JobStore = request.app.state.store
    try:
        playlist = await asyncio.to_thread(probe_mod.expand_playlist, body.url, settings)
    except probe_mod.ProbeError as exc:
        return JSONResponse({"detail": str(exc), "needs_login": exc.needs_login}, status_code=422)

    # Plan first (no store writes), then create all new jobs with a single save.
    plan: list = []          # existing Job, or index into `items`
    items: list[dict] = []
    seen: set[str] = set()
    reused = 0
    playlist_id = secrets.token_urlsafe(6)   # groups this request's jobs for "Cancel remaining"
    for index, entry in enumerate(playlist["entries"][:min(body.limit, settings.max_playlist_items)], start=1):
        if entry["url"] in seen:
            continue
        seen.add(entry["url"])
        existing = store.find_active(entry["url"], body.format, body.quality, body.subtitles)
        if existing is not None:
            plan.append(existing)
            reused += 1
            continue
        plan.append(len(items))
        items.append({"url": entry["url"], "format": body.format, "quality": body.quality,
                      "playlist": playlist["title"], "playlist_index": index, "title": entry["title"],
                      "playlist_id": playlist_id, "subtitles": body.subtitles})
    new_jobs = store.add_many(items)
    for job in new_jobs:
        request.app.state.pool.enqueue(job.id)
    jobs = [new_jobs[p] if isinstance(p, int) else p for p in plan]
    queued = len(new_jobs)
    return {
        "playlist": playlist["title"],
        "total": playlist["total"],
        "queued": queued,
        "reused": reused,
        "jobs": [j.to_dict(public=True) for j in jobs],
    }


@router.delete("/api/playlists/{playlist_id}")
async def remove_playlist_jobs(playlist_id: str, request: Request,
                               status: Literal["active", "finished"] = "active"):
    """status=active (default): cancel every queued, paused or downloading job of the playlist.
    status=finished: remove every done/failed row of the playlist and delete its file."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if not any(job.playlist_id == playlist_id for job in store.list()):
        raise HTTPException(status_code=404)
    # The status check runs under the store lock so a job changing state at this instant
    # is classified consistently (see remove_where). Removing an active job is the cancel
    # signal (see delete_job).
    wanted = ACTIVE if status == "active" else {DONE, FAILED}
    removed = store.remove_where(lambda job: job.playlist_id == playlist_id and job.status in wanted
                                 and not job.keep)   # kept rows only go via their own Remove button
    if status == "active":
        return {"cancelled": len(removed)}
    downloads = request.app.state.settings.downloads_dir
    for job in removed:
        if job.filename:
            (downloads / job.filename).unlink(missing_ok=True)
    return {"removed": len(removed)}


def _zip_filename(title: Optional[str], playlist_id: str) -> str:
    """ASCII-safe fallback name for the Content-Disposition header."""
    base = re.sub(r"[^A-Za-z0-9 ._-]+", "_", title or "").strip(" ._") or f"playlist-{playlist_id}"
    return base[:80].rstrip(" .") + ".zip"


def _content_disposition(title: Optional[str], playlist_id: str) -> str:
    ascii_name = _zip_filename(title, playlist_id)
    header = f'attachment; filename="{ascii_name}"'
    pretty = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", (title or "").strip())[:80]
    if pretty and pretty + ".zip" != ascii_name:
        header += f"; filename*=UTF-8''{quote(pretty + '.zip')}"   # RFC 6266: keeps non-ASCII titles
    return header


@router.get("/api/playlists/{playlist_id}/zip")
async def zip_playlist(playlist_id: str, request: Request):
    """Stream a stored zip of every finished file of one playlist expansion."""
    store: JobStore = request.app.state.store
    downloads = request.app.state.settings.downloads_dir
    jobs = sorted((j for j in store.list() if j.playlist_id == playlist_id),
                  key=lambda j: (j.playlist_index is None, j.playlist_index or 0, j.created_at))
    if not jobs:
        raise HTTPException(status_code=404)
    done = [j for j in jobs if j.status == DONE and j.filename and (downloads / j.filename).is_file()]
    if not done:
        raise HTTPException(status_code=404, detail="No finished files in this playlist yet")
    names = zipstream.arcnames((j.playlist_index, j.id, j.filename) for j in done)
    entries = [(arcname, downloads / filename) for arcname, filename in names]
    headers = {"Content-Disposition": _content_disposition(jobs[0].playlist, playlist_id)}
    return StreamingResponse(zipstream.stream_zip(entries), media_type="application/zip", headers=headers)


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request):
    """Put one failed job back in the queue, keeping its id and position."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404)
    _disk_guard(request)
    reset = store.requeue_where(lambda job: job.id == job_id)   # FAILED check is inside
    if not reset:
        raise HTTPException(status_code=409, detail="Only failed jobs can be retried")
    body = reset[0].to_dict(public=True)   # snapshot before a worker can pick the job up again
    request.app.state.pool.enqueue(job_id)
    return body


@router.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str, request: Request):
    """Hold a queued job back; the worker skips it until it is resumed."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404)
    moved = store.transition_where(lambda job: job.id == job_id, QUEUED, PAUSED)
    if not moved:
        raise HTTPException(status_code=409, detail="Only queued jobs can be paused")
    return moved[0].to_dict(public=True)


@router.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: Request):
    """Release a paused job back into the queue (at the end of the line)."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404)
    moved = store.transition_where(lambda job: job.id == job_id, PAUSED, QUEUED)
    if not moved:
        raise HTTPException(status_code=409, detail="Only paused jobs can be resumed")
    body = moved[0].to_dict(public=True)   # snapshot before a worker can pick the job up
    request.app.state.pool.enqueue(job_id)
    return body


def _set_keep(request: Request, job_id: str, value: bool) -> dict:
    _require_csrf(request)
    store: JobStore = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    if job.status != DONE:
        raise HTTPException(status_code=409, detail="Only finished downloads can be kept")
    updated = store.update(job_id, keep=value)
    if updated is None:   # cleared by Clear all / nightly cleanup in between
        raise HTTPException(status_code=404)
    return updated.to_dict(public=True)


@router.post("/api/jobs/{job_id}/keep")
async def keep_job(job_id: str, request: Request):
    """Exempt a finished download from the nightly cleanup and Clear all."""
    return _set_keep(request, job_id, True)


@router.post("/api/jobs/{job_id}/unkeep")
async def unkeep_job(job_id: str, request: Request):
    return _set_keep(request, job_id, False)


def _require_playlist(store: JobStore, playlist_id: str) -> None:
    if not any(job.playlist_id == playlist_id for job in store.list()):
        raise HTTPException(status_code=404)


@router.post("/api/playlists/{playlist_id}/pause")
async def pause_playlist_jobs(playlist_id: str, request: Request):
    _require_csrf(request)
    store: JobStore = request.app.state.store
    _require_playlist(store, playlist_id)
    moved = store.transition_where(lambda job: job.playlist_id == playlist_id, QUEUED, PAUSED)
    return {"paused": len(moved)}


@router.post("/api/playlists/{playlist_id}/resume")
async def resume_playlist_jobs(playlist_id: str, request: Request):
    _require_csrf(request)
    store: JobStore = request.app.state.store
    _require_playlist(store, playlist_id)
    moved = store.transition_where(lambda job: job.playlist_id == playlist_id, PAUSED, QUEUED)
    for job in moved:
        request.app.state.pool.enqueue(job.id)
    return {"resumed": len(moved)}


@router.post("/api/playlists/{playlist_id}/retry")
async def retry_playlist_jobs(playlist_id: str, request: Request):
    """Put every failed job of one playlist expansion back in the queue."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if not any(job.playlist_id == playlist_id for job in store.list()):
        raise HTTPException(status_code=404)
    _disk_guard(request)
    reset = store.requeue_where(lambda job: job.playlist_id == playlist_id)
    for job in reset:
        request.app.state.pool.enqueue(job.id)
    return {"retried": len(reset)}


@router.get("/api/jobs")
async def list_jobs(request: Request):
    # public: the failure log is fetched per job (below), not in every 2 s poll.
    return [job.to_dict(public=True) for job in request.app.state.store.list()]


@router.get("/api/jobs/{job_id}/log")
async def job_log(job_id: str, request: Request):
    """The last yt-dlp lines of a failed download (scrubbed of paths and cookie file names)."""
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    return {"log": job.log or ""}


@router.get("/api/thumbs/{key}")
async def get_thumb(key: str, request: Request):
    """A thumbnail fetched by the server (probe or download); keys are server-chosen."""
    path = thumbs.find(request.app.state.settings.thumbs_dir, key) if thumbs.KEY_PATTERN.match(key) else None
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=thumbs.media_type(path),
                        headers={"Cache-Control": "public, max-age=86400"})


@router.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, request: Request):
    _require_csrf(request)
    store: JobStore = request.app.state.store
    # Remove the record first, then unlink whatever filename it had at that moment.
    # Removing an active job is how it gets cancelled: the download thread's hook sees the
    # record is gone and aborts, cleaning up its partial files. If the thread finished
    # between our read and the remove, the returned record carries the final filename.
    job = store.remove(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    settings = request.app.state.settings
    if job.filename:
        (settings.downloads_dir / job.filename).unlink(missing_ok=True)
    cookies_mod.CookieStore(settings).discard_once(job_id)
    return Response(status_code=204)


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
        return await asyncio.to_thread(probe_mod.probe, body.url, request.app.state.settings)
    except probe_mod.ProbeError as exc:
        # needs_login lets the page offer "Add cookies" when the site wants a logged-in session.
        return JSONResponse({"detail": str(exc), "needs_login": exc.needs_login}, status_code=422)


def downloads_usage(downloads_dir) -> dict:
    """Count and total size of regular files; files removed mid-scan are simply skipped."""
    files = total = 0
    try:
        with os.scandir(downloads_dir) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
    except FileNotFoundError:
        pass
    return {"files": files, "bytes": total}


@router.get("/api/status")
def status(request: Request):
    # Plain def: FastAPI runs it in the threadpool, so the directory scan (polled every
    # 2 s by the page) never blocks the event loop.
    settings = request.app.state.settings
    try:
        free = shutil.disk_usage(settings.downloads_dir).free
    except OSError:
        free = None
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "downloads": downloads_usage(settings.downloads_dir),
        "queue_paused": request.app.state.pool.paused,
        "rate_limit": settings.rate_limit,
        "impersonate": {"target": settings.impersonate,
                        "active": downloader_mod.target_supported(settings.impersonate)},
        "update": {k: v for k, v in request.app.state.updater.status.to_dict().items() if k != "running"},
        "disk": {"free": free, "min_free": settings.min_free_space, "low": worker_mod.disk_low(settings)},
        "proxy": settings.proxy_display,
    }


@router.post("/api/update")
async def update_yt_dlp(request: Request):
    """Install the newest yt-dlp into data/lib now; restart when idle if the version changed."""
    _require_csrf(request)
    updater = request.app.state.updater
    status_ = await asyncio.to_thread(updater.update)
    updater.schedule_restart()
    return status_.to_dict()


def _set_queue_paused(request: Request, value: bool) -> dict:
    _require_csrf(request)
    request.app.state.app_state.set_queue_paused(value)
    pool = request.app.state.pool
    pool.pause() if value else pool.resume()
    return {"queue_paused": pool.paused}


@router.post("/api/queue/pause")
async def pause_queue(request: Request):
    """Stop starting new downloads; running ones finish. Survives restarts."""
    return _set_queue_paused(request, True)


@router.post("/api/queue/resume")
async def resume_queue(request: Request):
    return _set_queue_paused(request, False)


@router.post("/api/cleanup")
async def trigger_cleanup(request: Request):
    _require_csrf(request)
    removed = cleanup_mod.run_cleanup(request.app.state.store, request.app.state.settings)
    return {"removed": removed}


@router.get("/files/{name}")
async def get_file(name: str, request: Request):
    if not name or "/" in name or "\\" in name or "\x00" in name or name.startswith("."):
        raise HTTPException(status_code=404)
    base = request.app.state.settings.downloads_dir.resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=name, media_type="application/octet-stream")
