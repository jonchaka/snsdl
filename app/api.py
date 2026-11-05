import asyncio
import errno
import io
import logging
import os
import re
import secrets
import shutil
from typing import Literal, Optional
from urllib.parse import quote, urlparse

import segno
import yt_dlp
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import auth
from . import cleanup as cleanup_mod
from . import cookies as cookies_mod
from . import downloader as downloader_mod
from . import files
from . import probe as probe_mod
from . import subscriptions as subs_mod
from . import thumbs
from . import tls
from . import worker as worker_mod
from . import zipstream
from .jobs import ACTIVE, DEFAULT_FORMAT, DEFAULT_QUALITY, DONE, FAILED, PAUSED, QUEUED, JobStore, check_format_quality

log = logging.getLogger(__name__)

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


def _check_compat(fmt: str, compat: bool) -> None:
    if compat and fmt != "mp4":
        raise ValueError("the H.264 preference applies to mp4 downloads")


def _no_sponsorblock(sponsorblock: bool) -> None:
    if sponsorblock:
        raise ValueError("sponsor removal applies to single videos")


class JobIn(ProbeIn):
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    subtitles: Optional[str] = Field(default=None, pattern=SUBTITLES_PATTERN)
    clip_start: Optional[float] = Field(default=None, ge=0, le=MAX_CLIP_SECONDS)   # seconds
    clip_end: Optional[float] = Field(default=None, ge=0, le=MAX_CLIP_SECONDS)
    thumb: Optional[str] = Field(default=None, pattern=thumbs.KEY_PATTERN.pattern)   # key from /api/probe
    clip_exact: bool = False   # frame-accurate cut (re-encodes at the cuts)
    sponsorblock: bool = False   # YouTube: cut SponsorBlock segments (ignored on other sites)
    compat: bool = False         # mp4: prefer H.264/AAC
    subtitles_mode: Literal["embed", "file"] = "embed"   # "file": a separate .srt beside the video

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        if self.subtitles and self.format != "mp4":
            raise ValueError("subtitles apply to mp4 downloads only")
        if self.subtitles and self.subtitles.removeprefix("auto:").lower() == "all":
            raise ValueError("subtitles must name one language")
        if self.clip_start is not None and self.clip_end is not None and self.clip_end <= self.clip_start:
            raise ValueError("clip end must be after clip start")
        if self.clip_exact and self.clip_start is None and self.clip_end is None:
            raise ValueError("exact cut needs a clip")
        if self.sponsorblock and (self.clip_start is not None or self.clip_end is not None):
            raise ValueError("sponsor removal cannot be combined with a clip")
        _check_compat(self.format, self.compat)
        if self.subtitles_mode == "file" and not self.subtitles:
            raise ValueError("a separate subtitle file needs subtitles")
        return self


class PlaylistIn(JobIn):
    limit: int = Field(ge=1)

    @model_validator(mode="after")
    def no_clips(self):
        if self.clip_start is not None or self.clip_end is not None or self.clip_exact:
            raise ValueError("clips apply to single videos")
        _no_sponsorblock(self.sponsorblock)
        return self


class BatchIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=2000)   # checked one by one in the route
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    compat: bool = False
    sponsorblock: bool = False   # accepted only to be refused: single videos only

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        _check_compat(self.format, self.compat)
        _no_sponsorblock(self.sponsorblock)
        return self


@router.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    _disk_guard(request)
    store: JobStore = request.app.state.store
    settings = request.app.state.settings
    if body.thumb is not None and thumbs.find(settings.thumbs_dir, body.thumb) is None:
        raise HTTPException(status_code=422, detail="Unknown thumbnail")
    existing = store.find_active(body.url, body.format, body.quality, body.subtitles,
                                 body.clip_start, body.clip_end, clip_exact=body.clip_exact,
                                 sponsorblock=body.sponsorblock, compat=body.compat,
                                 subtitles_mode=body.subtitles_mode)
    if existing is not None:
        return JSONResponse(existing.to_dict(public=True), status_code=200)
    job = store.add(body.url, format=body.format, quality=body.quality, subtitles=body.subtitles,
                    clip_start=body.clip_start, clip_end=body.clip_end, thumb=body.thumb,
                    clip_exact=body.clip_exact, sponsorblock=body.sponsorblock, compat=body.compat,
                    subtitles_mode=body.subtitles_mode)
    request.app.state.pool.enqueue(job.id)
    return JSONResponse(job.to_dict(public=True), status_code=201)


BATCH_REJECTED_MAX = 20      # invalid links echoed back by /api/jobs/batch
BATCH_REJECTED_CHARS = 200   # each one cut to this length


@router.post("/api/jobs/batch", status_code=201)
async def create_batch_jobs(body: BatchIn, request: Request):
    """Queue one ordinary job per pasted link; invalid links are reported, not fatal."""
    _require_csrf(request)
    _disk_guard(request)
    settings = request.app.state.settings
    store: JobStore = request.app.state.store
    urls: list[str] = []
    rejected: list[str] = []
    for raw in body.urls:
        try:
            url = _validate_url(raw)
        except ValueError:
            if len(rejected) < BATCH_REJECTED_MAX:   # echo a bounded sample, not the whole paste
                rejected.append(raw.strip()[:BATCH_REJECTED_CHARS])
            continue
        if url not in urls:
            urls.append(url)
    if not urls:
        raise HTTPException(status_code=422, detail="None of those is an http:// or https:// link")
    if len(urls) > settings.max_playlist_items:
        raise HTTPException(status_code=422, detail=f"At most {settings.max_playlist_items} links at once "
                                                    f"(MAX_PLAYLIST_ITEMS); got {len(urls)}")

    # Same plan-then-add_many shape as playlists: one save for all new jobs.
    plan: list = []          # existing Job, or index into `items`
    items: list[dict] = []
    for url in urls:
        existing = store.find_active(url, body.format, body.quality, compat=body.compat)
        if existing is not None:
            plan.append(existing)
            continue
        plan.append(len(items))
        items.append({"url": url, "format": body.format, "quality": body.quality, "compat": body.compat})
    new_jobs = store.add_many(items)
    for job in new_jobs:
        request.app.state.pool.enqueue(job.id)
    jobs = [new_jobs[p] if isinstance(p, int) else p for p in plan]
    return {
        "queued": len(new_jobs),
        "reused": len(jobs) - len(new_jobs),
        "rejected": rejected,
        "jobs": [j.to_dict(public=True) for j in jobs],
    }


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
        existing = store.find_active(entry["url"], body.format, body.quality, body.subtitles,
                                     compat=body.compat, subtitles_mode=body.subtitles_mode)
        if existing is not None:
            plan.append(existing)
            reused += 1
            continue
        plan.append(len(items))
        items.append({"url": entry["url"], "format": body.format, "quality": body.quality,
                      "playlist": playlist["title"], "playlist_index": index, "title": entry["title"],
                      "playlist_id": playlist_id, "subtitles": body.subtitles,
                      "compat": body.compat, "subtitles_mode": body.subtitles_mode})
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
    settings = request.app.state.settings
    cleanup_mod.delete_unused_thumbs(store, settings, removed)
    if status == "active":
        return {"cancelled": len(removed)}
    for job in removed:
        files.delete_files(settings, job)   # the video and its sidecars
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


def _zip_entries(settings, done, indexed: bool) -> list[tuple[str, object]]:
    """(arcname, path) for each finished (job, path) followed by its sidecars. A sidecar is
    named after its video's arcname (`title.lang.srt`, `NNN-title.lang.srt` in a playlist zip,
    `Title (2).lang.srt` when the video's name was de-duplicated)."""
    videos = zipstream.arcnames((job.playlist_index if indexed else None, job.id, path.name)
                                for job, path in done)
    seen = {arc for arc, _ in videos}
    entries: list[tuple[str, object]] = []
    for (job, path), (arc, _) in zip(done, videos):
        entries.append((arc, path))
        stem = arc.rpartition(".")[0] or arc
        for p in files.sidecar_paths(settings, job):
            if p.name.startswith(path.stem + "."):
                suffix = p.name[len(path.stem) + 1:]       # "en.srt", "en (2).srt"
            else:
                suffix = ".".join(p.name.split(".")[-2:])  # unexpected name: keep "lang.srt"
            name = f"{stem}.{zipstream._UNSAFE.sub('_', suffix)}"
            base, _, ext = name.rpartition(".")
            candidate, n = name, 1
            while candidate in seen:
                n += 1
                candidate = f"{base} ({n}).{ext}"
            seen.add(candidate)
            entries.append((candidate, p))
    return entries


def _finished_files(settings, jobs) -> list:
    """[(job, path)] for the done jobs whose file exists (downloads or KEEP_DIR), in order."""
    out = []
    for job in jobs:
        path = files.file_path(settings, job) if job.status == DONE else None
        if path is not None:
            out.append((job, path))
    return out


@router.get("/api/playlists/{playlist_id}/zip")
async def zip_playlist(playlist_id: str, request: Request):
    """Stream a stored zip of every finished file of one playlist expansion."""
    store: JobStore = request.app.state.store
    settings = request.app.state.settings
    jobs = sorted((j for j in store.list() if j.playlist_id == playlist_id),
                  key=lambda j: (j.playlist_index is None, j.playlist_index or 0, j.created_at))
    if not jobs:
        raise HTTPException(status_code=404)
    done = _finished_files(settings, jobs)
    if not done:
        raise HTTPException(status_code=404, detail="No finished files in this playlist yet")
    entries = _zip_entries(settings, done, indexed=True)
    headers = {"Content-Disposition": _content_disposition(jobs[0].playlist, playlist_id)}
    return StreamingResponse(zipstream.stream_zip(entries), media_type="application/zip", headers=headers)


MAX_BULK_IDS = 500   # ids per bulk action or zip request


@router.get("/api/zip")
async def zip_jobs(request: Request, ids: str = ""):
    """Stream a stored zip of the finished files among `ids` (comma-separated job ids)."""
    wanted = list(dict.fromkeys(i.strip() for i in ids.split(",") if i.strip()))
    if not wanted:
        raise HTTPException(status_code=422, detail="No downloads given")
    if len(wanted) > MAX_BULK_IDS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_BULK_IDS} downloads at once")
    store: JobStore = request.app.state.store
    jobs = [job for job in map(store.get, wanted) if job is not None]
    settings = request.app.state.settings
    done = _finished_files(settings, jobs)
    if not done:
        raise HTTPException(status_code=404, detail="None of those downloads has finished")
    entries = _zip_entries(settings, done, indexed=False)
    headers = {"Content-Disposition": _content_disposition("downloads", "")}
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
    # One locked step: a paused job loses "download next" and "download now" (resume puts it
    # at the end of the line).
    paused = store.update_where(job_id, lambda job: job.status == QUEUED, status=PAUSED,
                                priority=False, now=False)
    if paused is None:
        raise HTTPException(status_code=409, detail="Only queued jobs can be paused")
    return paused.to_dict(public=True)


@router.post("/api/jobs/{job_id}/next")
async def download_next(job_id: str, request: Request):
    """Move a queued job to the front of the queue. Its old FIFO entry stays behind and is
    skipped by the worker once the job is no longer queued."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404)
    # Status check and flag set under one lock, so a worker starting the job in between
    # cannot leave a downloading job marked priority.
    updated = store.update_where(job_id, lambda job: job.status == QUEUED, priority=True)
    if updated is None:
        if store.get(job_id) is None:   # cancelled in between
            raise HTTPException(status_code=404)
        raise HTTPException(status_code=409, detail="Only queued jobs can be downloaded next")
    body = updated.to_dict(public=True)   # snapshot before a worker can pick the job up
    request.app.state.pool.enqueue_front(job_id)
    return body


@router.post("/api/jobs/{job_id}/now")
async def download_now(job_id: str, request: Request):
    """Start a queued job even while the download window is closed. A job parked by the window
    is released to the queue front; one still in the queue is moved to the front too."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404)
    updated = store.update_where(job_id, lambda job: job.status == QUEUED, now=True)
    if updated is None:
        if store.get(job_id) is None:   # cancelled in between
            raise HTTPException(status_code=404)
        raise HTTPException(status_code=409, detail="Only queued jobs can be downloaded now")
    body = updated.to_dict(public=True)   # snapshot before a worker can pick the job up
    pool = request.app.state.pool
    held = pool.is_held(job_id)
    # Not parked by the window: it is in the queue; move it up. A held job release() dropped
    # (no longer queued, or waiting for a retry) is not put back.
    if not pool.release(job_id) and not held:
        pool.enqueue_front(job_id)
    return body


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


async def _apply_keep(request: Request, job, value: bool):
    """Set or clear `keep` on a done job (files.keep, in a thread: when KEEP_DIR is on another
    filesystem the move is a full copy). Returns the updated job, or None when it is not done
    or vanished meanwhile. Any other OSError from the move propagates, with `keep` left as it was."""
    if job.status != DONE:
        return None
    return await asyncio.to_thread(files.keep, request.app.state.settings, request.app.state.store,
                                   job.id, value)


async def _set_keep(request: Request, job_id: str, value: bool) -> dict:
    _require_csrf(request)
    store: JobStore = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    if job.status != DONE:
        raise HTTPException(status_code=409, detail="Only finished downloads can be kept")
    try:
        updated = await _apply_keep(request, job, value)
    except OSError as exc:
        status = 507 if exc.errno == errno.ENOSPC else 500
        raise HTTPException(status_code=status, detail=f"Could not move the file: {exc.strerror or exc}")
    if updated is None:
        raise HTTPException(status_code=404)
    return updated.to_dict(public=True)


@router.post("/api/jobs/{job_id}/keep")
async def keep_job(job_id: str, request: Request):
    """Exempt a finished download from the nightly cleanup and Clear all (and move its file
    into KEEP_DIR when one is set)."""
    return await _set_keep(request, job_id, True)


@router.post("/api/jobs/{job_id}/unkeep")
async def unkeep_job(job_id: str, request: Request):
    return await _set_keep(request, job_id, False)


class BulkIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: Literal["keep", "unkeep", "remove"]


@router.post("/api/jobs/bulk")
async def bulk_jobs(body: BulkIn, request: Request):
    """Keep, unkeep or remove many jobs. keep/unkeep apply to done jobs only; remove is
    DELETE /api/jobs/{id} per id. Unknown (and repeated) ids count as skipped."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    done = 0
    removed = []
    for job_id in dict.fromkeys(body.ids):
        if body.action == "remove":
            job = _delete_one(request, job_id, clean_thumbs=False)
            ok = job is not None
            if ok:
                removed.append(job)
        else:
            job = store.get(job_id)
            try:
                ok = job is not None and await _apply_keep(request, job, body.action == "keep") is not None
            except OSError:   # skip this one; the rest of the batch still runs
                log.warning("bulk %s: could not move the file of job %s", body.action, job_id, exc_info=True)
                ok = False
        done += ok
    if removed:
        cleanup_mod.delete_unused_thumbs(store, request.app.state.settings, removed)
    return {"done": done, "skipped": len(body.ids) - done}


MAX_QR_URL = 1500   # well under the ~2300 bytes a version-40 QR code holds at level M


def _served_url_path(job) -> str:
    """files.served_path(job) with each segment URL-quoted: "name" or "subdir/name"."""
    return "/".join(quote(seg, safe="") for seg in files.served_path(job).split("/"))


def _file_url(request: Request, job) -> str:
    """Absolute /files URL of the job's file as the browser reached this server (Host header),
    so a phone on the same network can open it."""
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}/files/{_served_url_path(job)}"


@router.get("/api/jobs/{job_id}/qr.svg")
async def job_qr(job_id: str, request: Request):
    """A QR code of the file's URL, for opening the download on a phone."""
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    if job.status != DONE:
        raise HTTPException(status_code=409, detail="Only finished downloads have a QR code")
    if files.file_path(request.app.state.settings, job) is None:
        raise HTTPException(status_code=404, detail="The file is gone")
    url = _file_url(request, job)
    if len(url) > MAX_QR_URL:   # an oversized Host header would overflow the QR code (500)
        raise HTTPException(status_code=422, detail="URL too long for a QR code")
    qr = segno.make(url, error="m", micro=False)
    # A standalone SVG document (with xmlns) rather than svg_inline(): the page loads it in an
    # <img>, which does not render a namespace-less SVG.
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=6, xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def _require_playlist(store: JobStore, playlist_id: str) -> None:
    if not any(job.playlist_id == playlist_id for job in store.list()):
        raise HTTPException(status_code=404)


@router.post("/api/playlists/{playlist_id}/pause")
async def pause_playlist_jobs(playlist_id: str, request: Request):
    _require_csrf(request)
    store: JobStore = request.app.state.store
    _require_playlist(store, playlist_id)
    # Like the single-job pause: a paused job loses "download next" and "download now".
    moved = store.transition_where(lambda job: job.playlist_id == playlist_id, QUEUED, PAUSED,
                                   priority=False, now=False)
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


def _delete_one(request: Request, job_id: str, clean_thumbs: bool = True):
    """Remove one job with its file, unused thumbnail and use-once cookies; returns the removed
    job, or None if unknown. With clean_thumbs=False the caller deletes unused thumbnails
    itself (once for a whole batch).

    The record goes first, then whatever file it had at that moment. Removing an active job
    is how it gets cancelled: the download thread's hook sees the record is gone and aborts,
    cleaning up its partial files. If the thread finished between our read and the remove,
    the returned record carries the final filename."""
    store: JobStore = request.app.state.store
    job = store.remove(job_id)
    if job is None:
        return None
    settings = request.app.state.settings
    files.delete_files(settings, job)   # video and sidecars, in downloads or KEEP_DIR
    if clean_thumbs:
        cleanup_mod.delete_unused_thumbs(store, settings, [job])
    cookies_mod.CookieStore(settings).discard_once(job_id)
    return job


@router.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, request: Request):
    _require_csrf(request)
    if _delete_one(request, job_id) is None:
        raise HTTPException(status_code=404)
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
    # Plain def: FastAPI runs it in the threadpool, so the directory scan (polled every
    # 2 s by the page) never blocks the event loop.
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
        "subscriptions": {"count": len(request.app.state.subscriptions.list()),
                          "cron": settings.subscription_cron or None},
        "defaults": {"sponsorblock": settings.sponsorblock, "prefer_h264": settings.prefer_h264},
    }


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


class SubscriptionIn(BaseModel):
    url: str
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    subtitles: Optional[str] = Field(default=None, pattern=SUBTITLES_PATTERN)
    limit: int = Field(default=subs_mod.DEFAULT_LIMIT, ge=subs_mod.MIN_LIMIT, le=subs_mod.MAX_LIMIT)
    include_existing: bool = False
    auto_keep: bool = False

    @field_validator("url")
    @classmethod
    def must_be_http_url(cls, value: str) -> str:
        return _validate_url(value)

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        if self.subtitles and self.format != "mp4":
            raise ValueError("subtitles apply to mp4 downloads only")
        if self.subtitles and self.subtitles.removeprefix("auto:").lower() == "all":
            raise ValueError("subtitles must name one language")
        return self


def _background(request: Request, coro) -> None:
    """Run coro as a fire-and-forget task, kept referenced on app.state until it ends."""
    tasks = getattr(request.app.state, "background", None)
    if tasks is None:
        tasks = request.app.state.background = set()
    task = asyncio.create_task(coro)
    tasks.add(task)

    def done(t: asyncio.Task) -> None:
        tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("background task failed", exc_info=t.exception())

    task.add_done_callback(done)


def _subscription_or_404(request: Request, sub_id: str):
    sub = request.app.state.subscriptions.get(sub_id)
    if sub is None:
        raise HTTPException(status_code=404)
    return sub


@router.get("/api/subscriptions")
async def list_subscriptions(request: Request):
    return {"subscriptions": [s.to_dict() for s in request.app.state.subscriptions.list()]}


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
                                  auto_keep=body.auto_keep)
    entry = sub.to_dict()   # snapshot before the background check changes it
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
    return sub.to_dict()


@router.post("/api/subscriptions/{sub_id}/pause")
async def pause_subscription(sub_id: str, request: Request):
    """Scheduled checks skip a paused subscription (Check now still works)."""
    return await _set_subscription_enabled(request, sub_id, False)


@router.post("/api/subscriptions/{sub_id}/resume")
async def resume_subscription(sub_id: str, request: Request):
    return await _set_subscription_enabled(request, sub_id, True)


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


@router.get("/files/{path:path}")
async def get_file(path: str, request: Request):
    # "name": downloads, then KEEP_DIR; "subdir/name": a media-layout folder in KEEP_DIR only.
    found = files.find_served_file(request.app.state.settings, path)
    if found is None:
        raise HTTPException(status_code=404)
    return FileResponse(found, filename=path.rpartition("/")[2], media_type="application/octet-stream")
