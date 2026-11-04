import asyncio
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import cleanup as cleanup_mod
from . import probe as probe_mod
from .jobs import ACTIVE, DEFAULT_FORMAT, DEFAULT_QUALITY, JobStore, check_format_quality

router = APIRouter()

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "sns-downloader"


def _require_csrf(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403)


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


class JobIn(ProbeIn):
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        return self


class PlaylistIn(JobIn):
    limit: int = Field(ge=1)


@router.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    store: JobStore = request.app.state.store
    existing = store.find_active(body.url, body.format, body.quality)
    if existing is not None:
        return JSONResponse(existing.to_dict(), status_code=200)
    job = store.add(body.url, format=body.format, quality=body.quality)
    request.app.state.pool.enqueue(job.id)
    return JSONResponse(job.to_dict(), status_code=201)


@router.post("/api/playlists", status_code=201)
async def create_playlist_jobs(body: PlaylistIn, request: Request):
    """Expand a playlist and queue one job per entry (up to `limit` and MAX_PLAYLIST_ITEMS)."""
    _require_csrf(request)
    settings = request.app.state.settings
    store: JobStore = request.app.state.store
    try:
        playlist = await asyncio.to_thread(probe_mod.expand_playlist, body.url, settings)
    except probe_mod.ProbeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

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
        existing = store.find_active(entry["url"], body.format, body.quality)
        if existing is not None:
            plan.append(existing)
            reused += 1
            continue
        plan.append(len(items))
        items.append({"url": entry["url"], "format": body.format, "quality": body.quality,
                      "playlist": playlist["title"], "playlist_index": index, "title": entry["title"],
                      "playlist_id": playlist_id})
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
        "jobs": [j.to_dict() for j in jobs],
    }


@router.delete("/api/playlists/{playlist_id}")
async def cancel_playlist_jobs(playlist_id: str, request: Request):
    """Cancel every still-active job of one playlist expansion; finished and failed rows stay."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if not any(job.playlist_id == playlist_id for job in store.list()):
        raise HTTPException(status_code=404)
    # Removing an active job is the cancel signal (see delete_job). The status check runs
    # under the store lock so a job finishing at this instant keeps its row and file.
    removed = store.remove_where(lambda job: job.playlist_id == playlist_id and job.status in ACTIVE)
    return {"cancelled": len(removed)}


@router.get("/api/jobs")
async def list_jobs(request: Request):
    return [job.to_dict() for job in request.app.state.store.list()]


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
    if job.filename:
        (request.app.state.settings.downloads_dir / job.filename).unlink(missing_ok=True)
    return Response(status_code=204)


@router.post("/api/probe")
async def probe_url(body: ProbeIn, request: Request):
    _require_csrf(request)
    try:
        return await asyncio.to_thread(probe_mod.probe, body.url, request.app.state.settings)
    except probe_mod.ProbeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


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
