import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

import yt_dlp
from yt_dlp.postprocessor import EmbedThumbnailPP
from yt_dlp.utils import PostProcessingError

from .config import Settings
from .cookies import cookies_for
from .jobs import DEFAULT_FORMAT, DEFAULT_QUALITY, DONE, FAILED, JobStore

log = logging.getLogger(__name__)

FORMAT = "bestvideo*+bestaudio/best"
# Resolution first, then AV1 ahead of yt-dlp's default codec order (vp9, h265, h264).
# Native streams are kept as-is; this only picks the smaller codec when a site offers
# several at the chosen height.
MP4_FORMAT_SORT = ["res", "vcodec:av01"]
THUMBNAIL_POSTPROCESSORS = [
    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
]
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")
RATE_WRITE_INTERVAL = 1.0   # seconds between speed/eta writes to the store (jobs.json)
EXT_FOR_FORMAT = {"mp4": ".mp4", "mp3": ".mp3"}   # "audio": container depends on the codec


class TolerantEmbedThumbnailPP(EmbedThumbnailPP):
    """EmbedThumbnail that skips, instead of failing the download, a container it cannot tag.
    Used for "audio" (original) downloads, whose container follows the source codec and may be
    wav/wma/aiff; mp3 and mp4 outputs are always supported, so they use the plain step."""

    def run(self, info):
        try:
            return super().run(info)
        except PostProcessingError as exc:
            self.report_warning(f"thumbnail not embedded: {exc}")
            return [], info


class DownloadCancelled(Exception):
    """Raised from a yt-dlp hook when the job was removed from the store mid-download."""


def format_selector(format: str, quality: str) -> str:
    if format in ("mp3", "audio"):
        return "bestaudio/best"
    if quality == "best":
        return FORMAT
    return f"bestvideo*[height<={quality}]+bestaudio/best[height<={quality}]/best"


AUTO_PREFIX = "auto:"   # subtitles value for automatic captions, e.g. "auto:en"


def build_opts(settings: Settings, job_id: str, hook: Callable[[dict], None],
               format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY,
               cookiefile: Optional[Path] = None, subtitles: Optional[str] = None) -> dict:
    opts = {
        "format": format_selector(format, quality),
        "outtmpl": str(settings.tmp_dir / f"{job_id}-%(title).80B.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [hook],
    }
    if format == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "0" if quality == "best" else quality},
            {"key": "FFmpegMetadata"},
        ]
    elif format == "audio":
        # "best" = keep the codec, stream-copy into the matching audio container
        # (aac -> m4a, opus -> opus, vorbis -> ogg; mp3/flac/wav sources are left as they are).
        # yt-dlp only re-encodes (to mp3) codecs it has no container for, e.g. pcm or wma.
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        opts["merge_output_format"] = "mp4"
        opts["format_sort"] = list(MP4_FORMAT_SORT)
        if subtitles:
            # Fetch one track (site's own or automatic captions) and mux it into the mp4 as a
            # soft subtitle stream (mov_text). A video lacking that language only logs a warning.
            auto = subtitles.startswith(AUTO_PREFIX)
            opts["writesubtitles"] = not auto
            opts["writeautomaticsub"] = auto
            # yt-dlp treats entries as regexes (and "all"/"-x" as aliases): escape to match literally.
            opts["subtitleslangs"] = [re.escape(subtitles.removeprefix(AUTO_PREFIX))]
            opts["subtitlesformat"] = "vtt/srt/best"
            opts["postprocessors"] = [{"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}]
    if settings.embed_thumbnails:
        # Fetch the thumbnail, convert to JPEG (sites often serve WebP, which the embedder
        # rejects for mp4/mp3), then embed it and delete the image file. For "audio" the
        # embed step is added in download() as TolerantEmbedThumbnailPP instead.
        opts["writethumbnail"] = True
        steps = THUMBNAIL_POSTPROCESSORS[:1] if format == "audio" else THUMBNAIL_POSTPROCESSORS
        opts.setdefault("postprocessors", []).extend(dict(pp) for pp in steps)
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)
    if settings.rate_limit:
        opts["ratelimit"] = settings.rate_limit
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


def _locate_output(info: Optional[dict], tmp_dir: Path, job_id: str,
                   ext: Optional[str] = ".mp4") -> Optional[Path]:
    """The finished file: yt-dlp's reported path if it exists, else the largest non-partial
    `{job_id}-*` file in tmp, preferring `ext` when given (None = any extension)."""
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
    candidates.sort(key=lambda p: (ext is not None and p.suffix != ext, -p.stat().st_size))
    return candidates[0]


def _as_int(value) -> Optional[int]:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def download(job_id: str, url: str, store: JobStore, settings: Settings,
             ydl_class=yt_dlp.YoutubeDL, clock: Callable[[], float] = time.monotonic) -> None:
    """Blocking. Downloads `url` for `job_id`, leaving the job `done` or `failed`. Never raises."""

    last_written: list = [None]   # (progress, title) last sent to the store
    rate_written_at: list = [None]  # clock() of the last speed/eta write; None once cleared

    def hook(d: dict) -> None:
        if store.get(job_id) is None:
            raise DownloadCancelled(job_id)
        fields: dict = {}
        pct = progress_percent(d)
        if pct is not None:
            fields["progress"] = pct
        title = (d.get("info_dict") or {}).get("title")
        if title:
            fields["title"] = title
        key = (fields.get("progress"), fields.get("title"))
        changed = bool(fields) and key != last_written[0]
        if not changed:
            fields = {}   # nothing new in progress/title; only a due speed/eta write remains
        if d.get("status") == "downloading":
            # Speed/eta change on every hook call; write them at most once per
            # RATE_WRITE_INTERVAL, or for free when progress/title are being written anyway.
            now = clock()
            if changed or rate_written_at[0] is None or now - rate_written_at[0] >= RATE_WRITE_INTERVAL:
                fields["speed"], fields["eta"] = _as_int(d.get("speed")), _as_int(d.get("eta"))
                rate_written_at[0] = now
        elif rate_written_at[0] is not None:
            # Download phase over (post-processing); stop showing a transfer rate.
            fields["speed"] = fields["eta"] = None
            rate_written_at[0] = None
        if not fields:
            return
        if changed:
            last_written[0] = key
        store.update(job_id, **fields)

    job = store.get(job_id)
    if job is None:
        log.info("job %s was removed before download started", job_id)
        return
    try:
        fmt, quality = job.format, job.quality
        with cookies_for(settings, job_id) as cookiefile, \
                ydl_class(build_opts(settings, job_id, hook, format=fmt, quality=quality,
                                     cookiefile=cookiefile, subtitles=job.subtitles)) as ydl:
            if fmt == "audio" and settings.embed_thumbnails:
                ydl.add_post_processor(TolerantEmbedThumbnailPP(already_have_thumbnail=False),
                                       when="post_process")
            info = ydl.extract_info(url, download=True)
        out = _locate_output(info, settings.tmp_dir, job_id, ext=EXT_FOR_FORMAT.get(fmt))
        if out is None:
            raise RuntimeError("download finished but no output file was found")
        dest = settings.downloads_dir / out.name
        shutil.move(str(out), str(dest))
        _remove_partials(settings.tmp_dir, job_id)
        current = store.get(job_id)
        title = (current.title if current else None) or (info or {}).get("title")
        if store.update(job_id, status=DONE, progress=100, filename=dest.name, title=title,
                        size=dest.stat().st_size, speed=None, eta=None) is None:
            # job was cleared (nightly cleanup) while we were finishing: don't leave an orphan file
            dest.unlink(missing_ok=True)
    except DownloadCancelled:
        log.info("job %s cancelled", job_id)
        try:
            _remove_partials(settings.tmp_dir, job_id)
        except OSError as clean_exc:
            log.warning("job %s: could not remove partial files: %s", job_id, clean_exc)
    except Exception as exc:  # noqa: BLE001 - every failure must land on the job
        log.warning("job %s failed: %s", job_id, exc)
        try:
            _remove_partials(settings.tmp_dir, job_id)
        except OSError as clean_exc:
            log.warning("job %s: could not remove partial files: %s", job_id, clean_exc)
        store.update(job_id, status=FAILED, error=trim_error(exc), speed=None, eta=None)
