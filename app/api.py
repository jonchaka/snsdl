import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator, model_validator

from . import cleanup as cleanup_mod
from . import probe as probe_mod
from .jobs import DEFAULT_FORMAT, DEFAULT_QUALITY, JobStore, check_format_quality

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


@router.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    store: JobStore = request.app.state.store
    existing = store.find_active(body.url, body.format, body.quality)
    if existing is not None:
        return JSONResponse(existing.to_dict(), status_code=200)
    job = store.add(body.url, format=body.format, quality=body.quality)
    request.app.state.pool.enqueue(job.id)
    return JSONResponse(job.to_dict(), status_code=201)


@router.get("/api/jobs")
async def list_jobs(request: Request):
    return [job.to_dict() for job in request.app.state.store.list()]


@router.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, request: Request):
    _require_csrf(request)
    store: JobStore = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    # Removing an active job is how it gets cancelled: the download thread's hook sees the
    # record is gone and aborts, cleaning up its partial files.
    if job.filename:
        (request.app.state.settings.downloads_dir / job.filename).unlink(missing_ok=True)
    store.remove(job_id)
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
