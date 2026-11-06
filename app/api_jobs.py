"""Job, playlist, zip and file routes."""
import asyncio
import errno
import io
import logging
import re
import secrets
from typing import Literal, Optional
from urllib.parse import quote

import segno
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import cleanup as cleanup_mod
from . import downloader as downloader_mod
from . import files
from . import probe as probe_mod
from . import thumbs
from . import zipstream
from .api_common import (BATCH_REJECTED_CHARS, BATCH_REJECTED_MAX, MAX_BULK_IDS, MAX_QR_URL, BatchIn, BulkIn,
                         JobIn, PlaylistIn, _delete_one, _disk_guard, _file_url, _public_jobs, _require_csrf,
                         _validate_url)
from .api_system import _set_queue_paused
from .jobs import ACTIVE, DONE, DOWNLOADING, FAILED, PAUSED, QUEUED, JobStore

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    _disk_guard(request)
    store: JobStore = request.app.state.store
    settings = request.app.state.settings
    if body.thumb is not None and thumbs.find(settings.thumbs_dir, body.thumb) is None:
        raise HTTPException(status_code=422, detail="Unknown thumbnail")
    custom_name = downloader_mod.safe_stem(body.filename) if body.filename else None
    if body.filename and custom_name is None:
        raise HTTPException(status_code=422, detail="file name has no usable characters")
    existing = store.find_active(body.url, body.format, body.quality, body.subtitles,
                                 body.clip_start, body.clip_end, clip_exact=body.clip_exact,
                                 sponsorblock=body.sponsorblock, compat=body.compat,
                                 subtitles_mode=body.subtitles_mode, split=body.split,
                                 custom_name=custom_name)
    if existing is not None:
        return JSONResponse(existing.to_dict(public=True), status_code=200)
    job = store.add(body.url, format=body.format, quality=body.quality, subtitles=body.subtitles,
                    clip_start=body.clip_start, clip_end=body.clip_end, thumb=body.thumb,
                    clip_exact=body.clip_exact, sponsorblock=body.sponsorblock, compat=body.compat,
                    subtitles_mode=body.subtitles_mode, live=body.live, split=body.split,
                    custom_name=custom_name)
    if body.live:   # a recording ignores the download window
        job = store.update(job.id, now=True) or job
    request.app.state.pool.enqueue(job.id)
    return JSONResponse(job.to_dict(public=True), status_code=201)


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
    cleanup_mod.delete_unused_thumbs(store, settings, removed, getattr(request.app.state, "history", None))
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


@router.post("/api/jobs/{job_id}/stop")
async def stop_recording(job_id: str, request: Request):
    """Ask a running live recording to stop; the recorder ends ffmpeg and keeps the file."""
    _require_csrf(request)
    store: JobStore = request.app.state.store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404)
    updated = store.update_where(job_id, lambda job: job.status == DOWNLOADING and job.live,
                                 stop_requested=True)
    if updated is None:
        if store.get(job_id) is None:   # cancelled in between
            raise HTTPException(status_code=404)
        raise HTTPException(status_code=409, detail="Only a running live recording can be stopped")
    return updated.to_dict(public=True)


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
        cleanup_mod.delete_unused_thumbs(store, request.app.state.settings, removed,
                                         getattr(request.app.state, "history", None))
    return {"done": done, "skipped": len(body.ids) - done}


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
    # public: the failure log is fetched per job (below), not in every update.
    return _public_jobs(request)


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
    if _delete_one(request, job_id) is None:
        raise HTTPException(status_code=404)
    return Response(status_code=204)


@router.post("/api/queue/pause")
async def pause_queue(request: Request):
    """Stop starting new downloads; running ones finish. Survives restarts."""
    return _set_queue_paused(request, True)


@router.post("/api/queue/resume")
async def resume_queue(request: Request):
    return _set_queue_paused(request, False)


@router.get("/files/{path:path}")
async def get_file(path: str, request: Request):
    # "name": downloads, then KEEP_DIR; "subdir/name": a media-layout folder in KEEP_DIR only.
    found = files.find_served_file(request.app.state.settings, path)
    if found is None:
        raise HTTPException(status_code=404)
    return FileResponse(found, filename=path.rpartition("/")[2], media_type="application/octet-stream")
