from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from . import cleanup as cleanup_mod
from .jobs import JobStore

router = APIRouter()


class JobIn(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def must_be_http_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("URL must start with http:// or https:// and include a host")
        return value


@router.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    store: JobStore = request.app.state.store
    existing = store.find_active(body.url)
    if existing is not None:
        return JSONResponse(existing.to_dict(), status_code=200)
    job = store.add(body.url)
    request.app.state.pool.enqueue(job.id)
    return JSONResponse(job.to_dict(), status_code=201)


@router.get("/api/jobs")
async def list_jobs(request: Request):
    return [job.to_dict() for job in request.app.state.store.list()]


@router.post("/api/cleanup")
async def trigger_cleanup(request: Request):
    removed = cleanup_mod.run_cleanup(request.app.state.store, request.app.state.settings)
    return {"removed": removed}


@router.get("/files/{name}")
async def get_file(name: str, request: Request):
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise HTTPException(status_code=404)
    path = request.app.state.settings.downloads_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=name, media_type="application/octet-stream")
