import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from .config import Settings
from .jobs import DEFAULT_FORMAT, DEFAULT_QUALITY, DONE, FAILED, JobStore

log = logging.getLogger(__name__)

FORMAT = "bestvideo*+bestaudio/best"
# Resolution first, then AV1 ahead of yt-dlp's default codec order (vp9, h265, h264).
# Native streams are kept as-is; this only picks the smaller codec when a site offers
# several at the chosen height.
MP4_FORMAT_SORT = ["res", "vcodec:av01"]
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")
EXT_FOR_FORMAT = {"mp4": ".mp4", "mp3": ".mp3"}


def format_selector(format: str, quality: str) -> str:
    if format == "mp3":
        return "bestaudio/best"
    if quality == "best":
        return FORMAT
    return f"bestvideo*[height<={quality}]+bestaudio/best[height<={quality}]/best"


def build_opts(settings: Settings, job_id: str, hook: Callable[[dict], None],
               format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY) -> dict:
    opts = {
        "format": format_selector(format, quality),
        "outtmpl": str(settings.tmp_dir / f"{job_id}-%(title).80B.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if format == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "0" if quality == "best" else quality},
            {"key": "FFmpegMetadata"},
        ]
    else:
        opts["merge_output_format"] = "mp4"
        opts["format_sort"] = list(MP4_FORMAT_SORT)
    if settings.cookies_file.is_file():
        opts["cookiefile"] = str(settings.cookies_file)
    return opts


def progress_percent(d: dict) -> Optional[int]:
    if d.get("status") == "finished":
        return 99
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    done = d.get("downloaded_bytes")
    if not total or done is None:
        return None
    return max(0, min(99, int(done * 100 / total)))


def trim_error(exc: BaseException) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first.removeprefix("ERROR: ")[:200]


def _remove_partials(tmp_dir: Path, job_id: str) -> None:
    for p in tmp_dir.glob(f"{job_id}-*"):
        if p.is_file():
            p.unlink(missing_ok=True)


def _locate_output(info: Optional[dict], tmp_dir: Path, job_id: str, ext: str = ".mp4") -> Optional[Path]:
    for rd in (info or {}).get("requested_downloads") or []:
        fp = rd.get("filepath")
        if fp and Path(fp).is_file():
            return Path(fp)
    candidates = [
        p for p in tmp_dir.glob(f"{job_id}-*")
        if p.is_file() and not p.name.endswith(PARTIAL_SUFFIXES)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.suffix != ext, -p.stat().st_size))
    return candidates[0]


def download(job_id: str, url: str, store: JobStore, settings: Settings,
             ydl_class=yt_dlp.YoutubeDL) -> None:
    """Blocking. Downloads `url` for `job_id`, leaving the job `done` or `failed`. Never raises."""

    last_written: list = [None]  # (progress, title) last sent to the store

    def hook(d: dict) -> None:
        fields: dict = {}
        pct = progress_percent(d)
        if pct is not None:
            fields["progress"] = pct
        title = (d.get("info_dict") or {}).get("title")
        if title:
            fields["title"] = title
        if not fields:
            return
        key = (fields.get("progress"), fields.get("title"))
        if key == last_written[0]:
            return
        last_written[0] = key
        store.update(job_id, **fields)

    try:
        job = store.get(job_id)
        fmt = job.format if job else DEFAULT_FORMAT
        quality = job.quality if job else DEFAULT_QUALITY
        with ydl_class(build_opts(settings, job_id, hook, format=fmt, quality=quality)) as ydl:
            info = ydl.extract_info(url, download=True)
        out = _locate_output(info, settings.tmp_dir, job_id, ext=EXT_FOR_FORMAT.get(fmt, ".mp4"))
        if out is None:
            raise RuntimeError("download finished but no output file was found")
        dest = settings.downloads_dir / out.name
        shutil.move(str(out), str(dest))
        _remove_partials(settings.tmp_dir, job_id)
        current = store.get(job_id)
        title = (current.title if current else None) or (info or {}).get("title")
        if store.update(job_id, status=DONE, progress=100, filename=dest.name, title=title) is None:
            # job was cleared (nightly cleanup) while we were finishing: don't leave an orphan file
            dest.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - every failure must land on the job
        log.warning("job %s failed: %s", job_id, exc)
        try:
            _remove_partials(settings.tmp_dir, job_id)
        except OSError as clean_exc:
            log.warning("job %s: could not remove partial files: %s", job_id, clean_exc)
        store.update(job_id, status=FAILED, error=trim_error(exc))
