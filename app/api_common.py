"""Helpers, request models and constants shared by the API routers. No routes."""
import asyncio
import logging
import re
import shutil
from typing import Literal, Optional
from urllib.parse import quote, urlparse

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from . import cleanup as cleanup_mod
from . import cookies as cookies_mod
from . import downloader as downloader_mod
from . import files
from . import thumbs
from . import worker as worker_mod
from .jobs import DEFAULT_FORMAT, DEFAULT_QUALITY, JobStore, check_format_quality

log = logging.getLogger(__name__)

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


# One subtitles entry: a language key as the probe reports it, optionally prefixed "auto:". No
# leading "-" and never "all": yt-dlp reads those as its discard syntax / every-language alias.
SUBTITLE_ENTRY_PATTERN = r"^(auto:)?[A-Za-z0-9][A-Za-z0-9._-]{0,19}$"
_SUBTITLE_ENTRY_RE = re.compile(SUBTITLE_ENTRY_PATTERN)
# The same key as a regex fragment that also refuses "all" in any case (no lookahead: pydantic's
# regex engine has none): 1-2 or 4-20 characters, or 3 that are not a/l/l.
_LANG = (r"(?:[A-Za-z0-9][A-Za-z0-9._-]?|[A-Za-z0-9][A-Za-z0-9._-]{3,19}"
         r"|[B-Zb-z0-9][A-Za-z0-9._-]{2}|[Aa][A-Ka-kM-Zm-z0-9._-][A-Za-z0-9._-]"
         r"|[Aa][Ll][A-Ka-kM-Zm-z0-9._-])")
_MORE = downloader_mod.MAX_SUBTITLE_LANGS - 1
# The whole value: 1 to 5 comma-separated entries of one kind (all "auto:" or none). Used as is
# by SubscriptionIn; JobIn checks the same rules with friendlier messages (_check_subtitles).
SUBTITLES_PATTERN = rf"^(?:{_LANG}(?:,{_LANG}){{0,{_MORE}}}|auto:{_LANG}(?:,auto:{_LANG}){{0,{_MORE}}})$"
SUBTITLES_LIST_MESSAGE = f"subtitles: up to {downloader_mod.MAX_SUBTITLE_LANGS} languages, comma-separated"
SUBTITLES_KIND_MESSAGE = "choose either site subtitles or automatic captions, not both"


def _check_subtitles(value: Optional[str]) -> Optional[str]:
    """A JobIn subtitles value normalized ("en, de,en" -> "en,de"), or ValueError."""
    if value is None:
        return None
    entries = downloader_mod.split_subtitles(value)
    if not 1 <= len(entries) <= downloader_mod.MAX_SUBTITLE_LANGS:
        raise ValueError(SUBTITLES_LIST_MESSAGE)
    for entry in entries:
        if not _SUBTITLE_ENTRY_RE.match(entry):
            raise ValueError(SUBTITLES_LIST_MESSAGE)
        if entry.removeprefix(downloader_mod.AUTO_PREFIX).lower() == "all":
            raise ValueError("subtitles must name languages, not all")
    if downloader_mod.subtitles_kind(entries) is None:
        raise ValueError(SUBTITLES_KIND_MESSAGE)
    return ",".join(entries)


MAX_CLIP_SECONDS = 9_999_999   # 7 digits, about 115 days


def _check_compat(fmt: str, compat: bool) -> None:
    if compat and fmt != "mp4":
        raise ValueError("the H.264 preference applies to mp4 downloads")


def _no_sponsorblock(sponsorblock: bool) -> None:
    if sponsorblock:
        raise ValueError("sponsor removal applies to single videos")


def _check_images(body, clipped: bool) -> None:
    """A photo post (format "images") takes none of the video options; the file name stays."""
    if body.subtitles:
        raise ValueError("photos have no subtitles")
    if clipped:
        raise ValueError("photos cannot be clipped")
    if body.sponsorblock:
        raise ValueError("photos have no sponsor data")
    if body.compat:
        raise ValueError("photos have no codec preference")
    if body.split:
        raise ValueError("photos cannot be split")
    if body.live:
        raise ValueError("photos are not live")


def _no_images(fmt: str) -> None:
    if fmt == "images":
        raise ValueError("photos are single posts")


class JobIn(ProbeIn):
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    subtitles: Optional[str] = Field(default=None, max_length=200)   # "en" or "en,de" or "auto:en"
    clip_start: Optional[float] = Field(default=None, ge=0, le=MAX_CLIP_SECONDS)   # seconds
    clip_end: Optional[float] = Field(default=None, ge=0, le=MAX_CLIP_SECONDS)
    thumb: Optional[str] = Field(default=None, pattern=thumbs.KEY_PATTERN.pattern)   # key from /api/probe
    clip_exact: bool = False   # frame-accurate cut (re-encodes at the cuts)
    sponsorblock: bool = False   # YouTube: cut SponsorBlock segments (ignored on other sites)
    compat: bool = False         # mp4: prefer H.264/AAC
    subtitles_mode: Literal["embed", "file"] = "embed"   # "file": a separate .srt beside the video
    live: bool = False    # record a live stream (ffmpeg) from now until Stop or its end
    split: bool = False   # one file per chapter
    filename: Optional[str] = Field(default=None, max_length=downloader_mod.CUSTOM_NAME_CHARS)

    @field_validator("subtitles")
    @classmethod
    def subtitles_list(cls, value: Optional[str]) -> Optional[str]:
        return _check_subtitles(value)

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        clipped = self.clip_start is not None or self.clip_end is not None or self.clip_exact
        if self.format == "images":
            _check_images(self, clipped)
        if self.live:
            if clipped:
                raise ValueError("a live stream cannot be clipped")
            if self.sponsorblock:
                raise ValueError("a live stream has no sponsor data")
            if self.subtitles:
                raise ValueError("a live stream has no subtitles")
            if self.split:
                raise ValueError("a live stream cannot be split")
        if self.split:
            if clipped:
                raise ValueError("chapters cannot be split from a clip")
            if self.subtitles_mode == "file":
                raise ValueError("a separate subtitle file cannot be combined with splitting")
        if self.subtitles and self.format != "mp4":
            raise ValueError("subtitles apply to mp4 downloads only")
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
        _no_images(self.format)
        if self.clip_start is not None or self.clip_end is not None or self.clip_exact:
            raise ValueError("clips apply to single videos")
        _no_sponsorblock(self.sponsorblock)
        if self.live or self.split or self.filename:
            raise ValueError("live/split/file name apply to single videos")
        return self


class BatchIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=2000)   # checked one by one in the route
    format: str = DEFAULT_FORMAT
    quality: str = DEFAULT_QUALITY
    compat: bool = False
    sponsorblock: bool = False   # accepted only to be refused: single videos only
    live: bool = False           # likewise
    split: bool = False          # likewise
    filename: Optional[str] = None   # likewise

    @model_validator(mode="after")
    def quality_matches_format(self):
        check_format_quality(self.format, self.quality)  # raises ValueError -> 422
        _no_images(self.format)
        _check_compat(self.format, self.compat)
        _no_sponsorblock(self.sponsorblock)
        if self.live or self.split or self.filename:
            raise ValueError("live/split/file name apply to single videos")
        return self


BATCH_REJECTED_MAX = 20      # invalid links echoed back by /api/jobs/batch
BATCH_REJECTED_CHARS = 200   # each one cut to this length


MAX_BULK_IDS = 500   # ids per bulk action or zip request


class BulkIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: Literal["keep", "unkeep", "remove"]


MAX_QR_URL = 1500   # well under the ~2300 bytes a version-40 QR code holds at level M


def _served_url_path(job) -> str:
    """files.served_path(job) with each segment URL-quoted: "name" or "subdir/name"."""
    return "/".join(quote(seg, safe="") for seg in files.served_path(job).split("/"))


def _file_url(request: Request, job) -> str:
    """Absolute /files URL of the job's file as the browser reached this server (Host header),
    so a phone on the same network can open it."""
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}/files/{_served_url_path(job)}"


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
        cleanup_mod.delete_unused_thumbs(store, settings, [job], getattr(request.app.state, "history", None))
    cookies_mod.CookieStore(settings).discard_once(job_id)
    return job


def _public_jobs(request: Request) -> list[dict]:
    return [job.to_dict(public=True) for job in request.app.state.store.list()]


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
