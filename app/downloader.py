import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from .config import Settings
from .jobs import DONE, FAILED, JobStore

log = logging.getLogger(__name__)

FORMAT = "bestvideo*+bestaudio/best"
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")


def build_opts(settings: Settings, job_id: str, hook: Callable[[dict], None]) -> dict:
    opts = {
        "format": FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(settings.tmp_dir / f"{job_id}-%(title).80B.%(ext)s"),
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if settings.cookies_file.is_file():
        opts["cookiefile"] = str(settings.cookies_file)
    return opts


def progress_percent(d: dict) -> Optional[int]:
    if d.get("status") == "finished":
        return 100
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    done = d.get("downloaded_bytes")
    if not total or done is None:
        return None
    return max(0, min(100, int(done * 100 / total)))


def _trim_error(exc: BaseException) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first.removeprefix("ERROR: ")[:200]


def _remove_partials(tmp_dir: Path, job_id: str) -> None:
    for p in tmp_dir.glob(f"{job_id}-*"):
        if p.is_file():
            p.unlink(missing_ok=True)


def _locate_output(info: Optional[dict], tmp_dir: Path, job_id: str) -> Optional[Path]:
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
    candidates.sort(key=lambda p: (p.suffix != ".mp4", -p.stat().st_size))
    return candidates[0]


def download(job_id: str, url: str, store: JobStore, settings: Settings,
             ydl_class=yt_dlp.YoutubeDL) -> None:
    """Blocking. Downloads `url` for `job_id`, leaving the job `done` or `failed`. Never raises."""

    def hook(d: dict) -> None:
        fields: dict = {}
        pct = progress_percent(d)
        if pct is not None:
            fields["progress"] = pct
        title = (d.get("info_dict") or {}).get("title")
        if title:
            fields["title"] = title
        if fields:
            store.update(job_id, **fields)

    try:
        with ydl_class(build_opts(settings, job_id, hook)) as ydl:
            info = ydl.extract_info(url, download=True)
        out = _locate_output(info, settings.tmp_dir, job_id)
        if out is None:
            raise RuntimeError("download finished but no output file was found")
        dest = settings.downloads_dir / out.name
        shutil.move(str(out), str(dest))
        _remove_partials(settings.tmp_dir, job_id)
        title = store.get(job_id).title or (info or {}).get("title")
        store.update(job_id, status=DONE, progress=100, filename=dest.name, title=title)
    except Exception as exc:  # noqa: BLE001 - every failure must land on the job
        log.warning("job %s failed: %s", job_id, exc)
        _remove_partials(settings.tmp_dir, job_id)
        store.update(job_id, status=FAILED, error=_trim_error(exc))
