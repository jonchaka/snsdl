import collections
import logging
import math
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import functools
import importlib.util

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.postprocessor import EmbedThumbnailPP
from yt_dlp.utils import DownloadError, PostProcessingError, download_range_func, sanitize_filename

from . import files, sites, thumbs
from .config import Settings
from .cookies import CookieStore
from .jobs import AUTO_RETRIES, DEFAULT_FORMAT, DEFAULT_QUALITY, DONE, FAILED, QUEUED, RETRY_DELAY, JobStore, _now_iso

log = logging.getLogger(__name__)

FORMAT = "bestvideo*+bestaudio/best"
# Resolution first, then AV1 ahead of yt-dlp's default codec order (vp9, h265, h264).
# Native streams are kept as-is; this only picks the smaller codec when a site offers
# several at the chosen height.
MP4_FORMAT_SORT = ["res", "vcodec:av01"]
# "Plays everywhere": H.264 video and AAC audio preferred at the chosen height. Only a
# preference: a height without an H.264 stream still gets the best one available (no re-encode).
MP4_COMPAT_FORMAT_SORT = ["res", "vcodec:h264", "acodec:aac"]
# SponsorBlock segments cut from YouTube downloads (after_filter fetches the segments, then
# ModifyChapters removes them; keyframe cuts, so no re-encode).
SPONSORBLOCK_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro"]
THUMBNAIL_POSTPROCESSORS = [
    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
]
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")
RATE_WRITE_INTERVAL = 1.0   # seconds between speed/eta writes to the store (jobs.json)
EXT_FOR_FORMAT = {"mp4": ".mp4", "mp3": ".mp3"}   # "audio": container depends on the codec
LOG_LINES = 60          # yt-dlp log lines kept per job (ring buffer)
LOG_LINE_CHARS = 300    # each cut to this many characters
LOG_MAX_BYTES = 8192    # stored log cap (UTF-8), oldest lines dropped first
COOKIE_FILE_NAME = re.compile(r"cookies(?:-once)?-[A-Za-z0-9_-]+\.txt")
# Optional extras whose failure would otherwise fail the whole download: when yt-dlp's error
# names one of them, download() runs again once without it and leaves a note on the job.
SUBTITLES_FAILURE = "Unable to download video subtitles"
SUBTITLES_NOTE = "Subtitles could not be fetched (site refused); downloaded without them"
# yt-dlp: "Unable to communicate with SponsorBlock API" (not any error that merely names it).
SPONSORBLOCK_FAILURE = "SponsorBlock API"
SPONSORBLOCK_NOTE = "Sponsor segments could not be fetched; downloaded uncut"
DESCRIPTION_CHARS = 2000   # yt-dlp description kept on the job, cut to this many characters
CUSTOM_NAME_CHARS = 150    # longest custom file name stem
NO_CHAPTERS_NOTE = "No chapters found; downloaded whole"
SPLIT_POSTPROCESSOR = {"key": "FFmpegSplitChapters", "force_keyframes": False, "when": "post_process"}


def safe_stem(name: Optional[str]) -> Optional[str]:
    """A user-chosen file name made safe as a literal stem: yt-dlp's restricted sanitizer
    (ASCII, no separators or spaces), leading dots and dashes stripped, cut to
    CUSTOM_NAME_CHARS. None when nothing usable is left."""
    stem = sanitize_filename((name or "").strip(), restricted=True).lstrip(".-")[:CUSTOM_NAME_CHARS]
    return stem or None


def _template_literal(text: str) -> str:
    # yt-dlp output templates use %-formatting: a literal % must be doubled.
    return text.replace("%", "%%")


def metadata_fields(info: Optional[dict]) -> dict:
    """The job fields taken from yt-dlp's info dict: duration (seconds, rounded up; None for a
    live stream or when unknown), uploader (else channel), upload_date (YYYY-MM-DD) and
    description (cut to DESCRIPTION_CHARS)."""
    info = info or {}
    live = bool(info.get("is_live")) or info.get("live_status") == "is_live"
    duration = None
    raw = info.get("duration")
    if not live and isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0 \
            and math.isfinite(raw):
        duration = math.ceil(raw)
    uploader = info.get("uploader") or info.get("channel") or None
    date = info.get("upload_date")
    upload_date = (f"{date[:4]}-{date[4:6]}-{date[6:]}"
                   if isinstance(date, str) and re.fullmatch(r"\d{8}", date) else None)
    desc = info.get("description")
    description = desc[:DESCRIPTION_CHARS] if isinstance(desc, str) and desc else None
    return {"duration": duration, "uploader": uploader if isinstance(uploader, str) else None,
            "upload_date": upload_date, "description": description}


class JobLogger:
    """yt-dlp `logger` for one job: keeps the tail of its output for the failed row's Details.
    Secrets (the proxy URL), paths in tmp and cookie-copy names are scrubbed as lines arrive (before cutting, so a long
    path can't be cut in half and escape the replacement)."""

    def __init__(self, tmp_dir: Path, secrets: Iterable[str] = ()):
        self._tmp = str(tmp_dir)
        # e.g. the proxy URL (may carry user:password); yt-dlp echoes it in connection errors.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._lines: collections.deque[str] = collections.deque(maxlen=LOG_LINES)
        self._lock = threading.Lock()   # yt-dlp may log from fragment/ffmpeg helper threads

    def _scrub(self, line: str) -> str:
        for secret in self._secrets:
            line = line.replace(secret, "<redacted>")
        if self._tmp:
            line = line.replace(self._tmp, "<tmp>")
        return COOKIE_FILE_NAME.sub("<cookies>", line)

    def _add(self, msg) -> None:
        text = str(msg)
        if text.startswith("[debug]"):   # only sent in verbose mode; noisy and may echo options
            return
        lines = [self._scrub(line)[:LOG_LINE_CHARS] for line in text.splitlines() if line.strip()]
        with self._lock:
            self._lines.extend(lines)

    def debug(self, msg) -> None:
        self._add(msg)

    def info(self, msg) -> None:
        self._add(msg)

    def warning(self, msg) -> None:
        self._add(msg)

    def error(self, msg) -> None:
        self._add(msg)

    def text(self) -> str:
        """The kept lines, newest last, oldest dropped until the UTF-8 size is <= LOG_MAX_BYTES."""
        with self._lock:
            lines = list(self._lines)
        sizes = [len(line.encode("utf-8")) for line in lines]
        total = sum(sizes) + max(len(lines) - 1, 0)
        start = 0
        while total > LOG_MAX_BYTES and start < len(lines):
            total -= sizes[start] + (1 if start < len(lines) - 1 else 0)
            start += 1
        return "\n".join(lines[start:])


def _clock_time(seconds: float) -> str:
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clip_label(start: Optional[float], end: Optional[float], exact: bool = False) -> str:
    """'m:ss–m:ss' (h:mm:ss past an hour); an open start reads 0:00, an open end 'end';
    ' exact' appended for a frame-accurate (re-encoded) cut."""
    label = f"{_clock_time(start or 0)}–{'end' if end is None else _clock_time(end)}"
    return f"{label} exact" if exact else label


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


def impersonation_available() -> bool:
    """yt-dlp can impersonate a browser's TLS/HTTP fingerprint only with curl_cffi installed."""
    return importlib.util.find_spec("curl_cffi") is not None


@functools.lru_cache(maxsize=16)
def _target_supported(target: str, ytdlp_version: str) -> bool:
    # Ask yt-dlp itself: it also rejects curl_cffi versions outside its supported range, and
    # YoutubeDL() would raise on every run if we passed an unsupported target.
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return bool(ydl._impersonate_target_available(ImpersonateTarget.from_str(target)))
    except Exception:  # noqa: BLE001 - any failure here just means "don't impersonate"
        return False


def target_supported(target: Optional[str]) -> bool:
    """True when yt-dlp can impersonate `target` right now (curl_cffi present and compatible)."""
    if not target or not impersonation_available():
        return False
    return _target_supported(target, yt_dlp.version.__version__)


def impersonate_opts(settings: Settings) -> dict:
    """yt-dlp options that make requests look like a real browser (downloads and probes)."""
    if target_supported(settings.impersonate):
        return {"impersonate": ImpersonateTarget.from_str(settings.impersonate)}
    return {}


def build_opts(settings: Settings, job_id: str, hook: Callable[[dict], None],
               format: str = DEFAULT_FORMAT, quality: str = DEFAULT_QUALITY,
               cookiefile: Optional[Path] = None, subtitles: Optional[str] = None,
               clip_start: Optional[float] = None, clip_end: Optional[float] = None,
               logger: Optional[JobLogger] = None, clip_exact: bool = False,
               sponsorblock: bool = False, compat: bool = False, subtitles_mode: str = "embed",
               url: Optional[str] = None, split: bool = False,
               custom_name: Optional[str] = None) -> dict:
    name = _template_literal(custom_name) if custom_name else None
    default_tmpl = str(settings.tmp_dir / (f"{job_id}-{name}.%(ext)s" if name
                                           else f"{job_id}-%(title).80B.%(ext)s"))
    if split:
        # One file per chapter, cut by FFmpegSplitChapters from the finished whole file.
        chapter_tmpl = (f"{job_id}-chapter%(section_number)03d-{name}-%(section_title).60B.%(ext)s" if name
                        else f"{job_id}-chapter%(section_number)03d-%(section_title).80B.%(ext)s")
        outtmpl = {"default": default_tmpl, "chapter": str(settings.tmp_dir / chapter_tmpl)}
    else:
        outtmpl = default_tmpl
    opts = {
        "format": format_selector(format, quality),
        "outtmpl": outtmpl,
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
        opts["format_sort"] = list(MP4_COMPAT_FORMAT_SORT if compat else MP4_FORMAT_SORT)
        if subtitles:
            # Fetch one track (site's own or automatic captions). Embed mode muxes it into the
            # mp4 as a soft subtitle stream (mov_text); file mode converts it to a .srt that
            # download() moves next to the video. A video lacking that language only logs a warning.
            auto = subtitles.startswith(AUTO_PREFIX)
            opts["writesubtitles"] = not auto
            opts["writeautomaticsub"] = auto
            # yt-dlp treats entries as regexes (and "all"/"-x" as aliases): escape to match literally.
            opts["subtitleslangs"] = [re.escape(subtitles.removeprefix(AUTO_PREFIX))]
            if subtitles_mode == "file":
                opts["subtitlesformat"] = "srt/vtt/best"
                opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}]
            else:
                opts["subtitlesformat"] = "vtt/srt/best"
                opts["postprocessors"] = [{"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}]
    if settings.embed_thumbnails:
        # Fetch the thumbnail, convert to JPEG (sites often serve WebP, which the embedder
        # rejects for mp4/mp3), then embed it and delete the image file. For "audio" the
        # embed step is added in download() as TolerantEmbedThumbnailPP instead.
        opts["writethumbnail"] = True
        steps = THUMBNAIL_POSTPROCESSORS[:1] if format == "audio" else THUMBNAIL_POSTPROCESSORS
        opts.setdefault("postprocessors", []).extend(dict(pp) for pp in steps)
    if sponsorblock and url and sites.site_of_url(url) == "youtube.com":
        # First, so the segments are cut before subtitles/thumbnails are embedded. Other sites
        # have no SponsorBlock data: the job's flag is stored but nothing is added.
        opts["postprocessors"] = [
            {"key": "SponsorBlock", "categories": list(SPONSORBLOCK_CATEGORIES), "when": "after_filter"},
            {"key": "ModifyChapters", "remove_sponsor_segments": list(SPONSORBLOCK_CATEGORIES),
             "force_keyframes": False, "when": "post_process"},
            *opts.get("postprocessors", []),
        ]
    if split:
        # Last, so every part carries the embedded thumbnail and subtitles.
        opts.setdefault("postprocessors", []).append(dict(SPLIT_POSTPROCESSOR))
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)
    if settings.rate_limit:
        opts["ratelimit"] = settings.rate_limit
    if clip_start is not None or clip_end is not None:
        # Section download through ffmpeg. By default no force_keyframes_at_cuts: that re-encodes
        # the clip; without it the cut lands on the nearest keyframe (may start a little early).
        # clip_exact opts into the slower, frame-accurate cut.
        end = clip_end if clip_end is not None else float("inf")
        opts["download_ranges"] = download_range_func(None, [(clip_start or 0, end)])
        if clip_exact:
            opts["force_keyframes_at_cuts"] = True
    if settings.proxy:
        opts["proxy"] = settings.proxy
    if settings.fragments > 1:
        opts["concurrent_fragment_downloads"] = settings.fragments
    if logger is not None:
        opts["logger"] = logger
        # With a logger, yt-dlp sends every progress-bar redraw to logger.debug, which would
        # push the useful lines out of the ring buffer. The hooks still get progress.
        opts["noprogress"] = True
    opts.update(impersonate_opts(settings))
    return opts


def progress_percent(d: dict) -> Optional[int]:
    if d.get("status") == "finished":
        return 99
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    done = d.get("downloaded_bytes")
    if not total or done is None:
        return None
    return max(0, min(99, int(done * 100 / total)))


LOGIN_WALL = re.compile(
    r"--cookies|\blog ?in\b|\blogged[- ]in\b|\bsign in\b|registered users|\bprivate\b|only available for"
    r"|requires authentication|\bsubscribers?\b|subscription|not available for this account"
    r"|age[- ]restricted|\b18 years old\b|members[- ]only|rate-limit reached", re.IGNORECASE)


def looks_login_walled(text: str) -> bool:
    """True when yt-dlp's error reads like 'you need to be logged in' (so cookies may help)."""
    return bool(LOGIN_WALL.search(text or ""))


# Network hiccups and server-side throttling that a later attempt may get past.
TRANSIENT = re.compile(
    r"timed? ?out|timeout|connection (reset|refused|aborted)|remote end closed|temporary failure"
    r"|name or service not known|nodename nor servname|no address associated|eof occurred"
    r"|incomplete read|HTTP Error (429|5\d\d)|read error|network is unreachable"
    # curl_cffi (impersonation) surfaces network failures as TransportError / "Failed to perform,
    # curl: (NN)"; a server dropping the connection reads "connection closed abruptly".
    r"|TransportError|failed to perform, curl:|connection closed abruptly", re.IGNORECASE)


def is_transient(text: str) -> bool:
    """True when yt-dlp's error reads like a temporary network/server problem worth one
    automatic retry. Login-walled errors never are: retrying without cookies cannot help."""
    return bool(TRANSIENT.search(text or "")) and not looks_login_walled(text)


def trim_error(exc: BaseException) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first.removeprefix("ERROR: ")[:200]


def _remove_partials(tmp_dir: Path, job_id: str) -> None:
    for p in tmp_dir.glob(f"{job_id}-*"):
        if p.is_file():
            p.unlink(missing_ok=True)


def _part_pattern(job_id: str) -> re.Pattern:
    """File names of the chapter parts of a split job: `<job_id>-chapterNNN-...`."""
    return re.compile(rf"^{re.escape(job_id)}-chapter(\d{{3}})-")


def _locate_output(info: Optional[dict], tmp_dir: Path, job_id: str,
                   ext: Optional[str] = ".mp4", skip: Optional[re.Pattern] = None) -> Optional[Path]:
    """The finished file: yt-dlp's reported path if it exists, else the largest non-partial
    `{job_id}-*` file in tmp, preferring `ext` when given (None = any extension). Names
    matching `skip` (a split job's chapter parts) are never taken."""
    for rd in (info or {}).get("requested_downloads") or []:
        fp = rd.get("filepath")
        if fp and Path(fp).is_file():
            return Path(fp)
    candidates = [
        p for p in tmp_dir.glob(f"{job_id}-*")
        if p.is_file() and not p.name.endswith(PARTIAL_SUFFIXES)
        and not (skip is not None and skip.match(p.name))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (ext is not None and p.suffix != ext, -p.stat().st_size))
    return candidates[0]


def _sidecar_lang(name: str, stem: str) -> str:
    """The language part of a yt-dlp subtitle file name: '<stem>.<lang>.srt' -> lang. A name
    that does not start with the video's stem falls back to the text after its last dot."""
    base = name[:-len(".srt")]
    if base.startswith(stem + ".") and len(base) > len(stem) + 1:
        return base[len(stem) + 1:]
    return base.rpartition(".")[2] or "sub"


def _collect_sidecars(tmp_dir: Path, job_id: str, dest: Path) -> list[str]:
    """Move the job's `<job_id>-*.srt` files from tmp to `<dest dir>/<dest.stem>.<lang>.srt`
    (a taken name gets a ' (2)' suffix) and return their final names. None found is fine:
    the site may have no track in that language."""
    names: list[str] = []
    for src in sorted(tmp_dir.glob(f"{job_id}-*.srt")):
        try:
            if not src.is_file() or src.is_symlink():
                continue
            name = files.unique_name(dest.parent, f"{dest.stem}.{_sidecar_lang(src.name, dest.stem)}.srt")
            shutil.move(str(src), str(dest.parent / name))
        except OSError as exc:
            # The video is already in place: a subtitle file that cannot move must not fail the job.
            log.warning("job %s: could not move subtitle file %s: %s", job_id, src.name, exc)
            continue
        names.append(name)
    return names


def _fallback(exc: BaseException, subtitles: Optional[str], sponsorblock: bool) -> Optional[str]:
    """Which optional extra made yt-dlp fail ("subtitles" / "sponsorblock"), if the job asked
    for it; None for any other failure."""
    if not isinstance(exc, (DownloadError, PostProcessingError)):
        return None
    text = str(exc)
    if subtitles and SUBTITLES_FAILURE in text:
        return "subtitles"
    if sponsorblock and SPONSORBLOCK_FAILURE in text:
        return "sponsorblock"
    return None


def _as_int(value) -> Optional[int]:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def fetch_job_thumb(job_id: str, url: str, store: JobStore, settings: Settings) -> None:
    """Fetch the thumbnail at `url` for a job that came without a probe (playlist members,
    batches) and store its key on the job; a job removed meanwhile leaves no orphan file.
    Never raises."""
    try:
        key = thumbs.new_key()
        if thumbs.fetch_thumbnail(url, settings.thumbs_dir, key=key, settings=settings) is None:
            return
        if store.update(job_id, thumb=key) is None:
            thumbs.delete(settings.thumbs_dir, key)
    except Exception as exc:  # noqa: BLE001 - a thumbnail must never fail the download
        log.warning("job %s: thumbnail failed: %s", job_id, exc)


def _chapter_parts(tmp_dir: Path, job_id: str, ext: str) -> list[tuple[int, Path]]:
    """(number, path) of a split job's chapter files in tmp with the whole file's extension,
    sorted by number."""
    pattern = _part_pattern(job_id)
    parts = []
    for p in tmp_dir.glob(f"{job_id}-chapter*"):
        m = pattern.match(p.name)
        if m and p.suffix == ext and p.is_file() and not p.is_symlink():
            parts.append((int(m.group(1)), p))
    return sorted(parts, key=lambda item: item[0])


def _chapter_duration(chapters, index: int) -> Optional[int]:
    try:
        ch = chapters[index]
        length = float(ch["end_time"]) - float(ch["start_time"])
    except (TypeError, KeyError, IndexError, ValueError):
        return None
    return math.ceil(length) if math.isfinite(length) and length > 0 else None


def _chapter_title(chapters, index: int) -> Optional[str]:
    try:
        title = chapters[index].get("title")
    except (TypeError, IndexError, AttributeError):
        return None
    return title if isinstance(title, str) and title.strip() else None


def _finish_split(job_id: str, store: JobStore, settings: Settings, info: Optional[dict],
                  whole: Path, parts: list[tuple[int, Path]]) -> list[str]:
    """Turn a split job's chapter files into one done job per part (grouped by
    playlist_id `split-<job_id>`) and drop the parent record. Returns the part ids; [] when
    the parent was removed meanwhile (its files are deleted then)."""
    parent = store.get(job_id)
    if parent is None:
        for _, path in parts:
            path.unlink(missing_ok=True)
        whole.unlink(missing_ok=True)
        _remove_partials(settings.tmp_dir, job_id)
        return []
    whole.unlink(missing_ok=True)
    meta = metadata_fields(info)
    chapters = (info or {}).get("chapters") or []
    group_title = parent.title or (info or {}).get("title")
    finished_at = _now_iso()
    items = []
    for number, src in parts:
        dest = settings.downloads_dir / src.name
        shutil.move(str(src), str(dest))
        items.append({
            "url": parent.url, "format": parent.format, "quality": parent.quality,
            "compat": parent.compat, "sponsorblock": parent.sponsorblock,
            "subtitles": parent.subtitles, "subtitles_mode": parent.subtitles_mode,
            "thumb": parent.thumb, "subscription_id": parent.subscription_id,
            "custom_name": parent.custom_name, "uploader": meta["uploader"],
            "upload_date": meta["upload_date"], "description": meta["description"],
            "created_at": parent.created_at, "status": DONE, "progress": 100,
            "filename": dest.name, "size": dest.stat().st_size,
            "duration": _chapter_duration(chapters, number - 1),
            "title": _chapter_title(chapters, number - 1) or f"Part {number}",
            "playlist": group_title, "playlist_index": number, "playlist_id": f"split-{job_id}",
            "finished_at": finished_at, "split": True,
        })
    _remove_partials(settings.tmp_dir, job_id)
    created = store.add_many(items)
    store.remove(job_id)   # the parts keep referencing its thumbnail
    return [j.id for j in created]


def download(job_id: str, url: str, store: JobStore, settings: Settings,
             ydl_class=yt_dlp.YoutubeDL, clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Blocking. Downloads `url` for `job_id`, leaving the job `done` or `failed`. Never raises.

    A transient failure (see is_transient) with automatic retries left instead leaves the job
    `queued` with `retry_at` set (RETRY_DELAY seconds ahead); the worker re-enqueues it then.
    Use-once cookies are discarded after every attempt, so such a retry runs without them.

    Returns the ids of the jobs that ended: normally [job_id] (done, failed or requeued); for a
    split job with chapters, the ids of the done part jobs that replace it; [] when the job
    was removed (cancelled) before or during the download."""

    last_written: list = [None]   # (progress, title) last sent to the store
    rate_written_at: list = [None]  # clock() of the last speed/eta write; None once cleared
    thumb_tried: list = [False]   # one thumbnail attempt per job, whatever its outcome
    thumb_lock = threading.Lock()  # hooks fire from several fragment threads (FRAGMENTS > 1)

    def hook(d: dict) -> None:
        current = store.get(job_id)
        if current is None:
            raise DownloadCancelled(job_id)
        if not thumb_tried[0]:
            thumb_url = None
            with thumb_lock:   # claim the single attempt before fetching, outside the lock
                if not thumb_tried[0]:
                    candidate = (d.get("info_dict") or {}).get("thumbnail")
                    if thumbs.is_http_url(candidate):
                        thumb_tried[0] = True
                        thumb_url = candidate
            if thumb_url is not None and current.thumb is None:
                fetch_job_thumb(job_id, thumb_url, store, settings)
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

    cookie_store = CookieStore(settings)
    logger = JobLogger(settings.tmp_dir, secrets=[settings.proxy] if settings.proxy else ())
    job = store.get(job_id)
    if job is None:
        log.info("job %s was removed before download started", job_id)
        cookie_store.discard_once(job_id)
        return []
    ended = [job_id]
    try:
        fmt, quality = job.format, job.quality
        subtitles, sponsorblock = job.subtitles, job.sponsorblock
        notes: list[str] = []
        with cookie_store.merged_for(job_id, job_id=job_id) as cookiefile:
            while True:
                try:
                    with ydl_class(build_opts(settings, job_id, hook, format=fmt, quality=quality,
                                              cookiefile=cookiefile, subtitles=subtitles,
                                              clip_start=job.clip_start, clip_end=job.clip_end,
                                              clip_exact=job.clip_exact, logger=logger,
                                              sponsorblock=sponsorblock, compat=job.compat,
                                              subtitles_mode=job.subtitles_mode, url=url,
                                              split=job.split, custom_name=job.custom_name)) as ydl:
                        if fmt == "audio" and settings.embed_thumbnails:
                            ydl.add_post_processor(TolerantEmbedThumbnailPP(already_have_thumbnail=False),
                                                   when="post_process")
                        info = ydl.extract_info(url, download=True)
                    break
                except Exception as exc:  # noqa: BLE001 - re-raised unless an extra is to blame
                    # Each extra is dropped at most once, so this loop runs at most three times.
                    extra = _fallback(exc, subtitles, sponsorblock)
                    if extra is None:
                        raise
                    log.warning("job %s: %s failed (%s); downloading again without", job_id, extra,
                                trim_error(exc))
                    if extra == "subtitles":
                        subtitles = None
                        notes.append(SUBTITLES_NOTE)
                    else:
                        sponsorblock = False
                        notes.append(SPONSORBLOCK_NOTE)
                    _remove_partials(settings.tmp_dir, job_id)
        part_pattern = _part_pattern(job_id) if job.split else None
        out = _locate_output(info, settings.tmp_dir, job_id, ext=EXT_FOR_FORMAT.get(fmt), skip=part_pattern)
        if out is None:
            raise RuntimeError("download finished but no output file was found")
        if job.split:
            parts = _chapter_parts(settings.tmp_dir, job_id, out.suffix)
            if parts:
                return _finish_split(job_id, store, settings, info, out, parts)
            notes.append(NO_CHAPTERS_NOTE)
        dest = settings.downloads_dir / out.name
        shutil.move(str(out), str(dest))
        sidecars = (_collect_sidecars(settings.tmp_dir, job_id, dest)
                    if subtitles and job.subtitles_mode == "file" else [])
        _remove_partials(settings.tmp_dir, job_id)
        current = store.get(job_id)
        title = (current.title if current else None) or (info or {}).get("title")
        if store.update(job_id, status=DONE, progress=100, filename=dest.name, title=title,
                        size=dest.stat().st_size, speed=None, eta=None, sidecars=sidecars,
                        note="; ".join(notes) or None, finished_at=_now_iso(),
                        **metadata_fields(info)) is None:
            # job was cleared (nightly cleanup) while we were finishing: don't leave orphan files
            dest.unlink(missing_ok=True)
            for name in sidecars:
                (dest.parent / name).unlink(missing_ok=True)
            ended = []
    except DownloadCancelled:
        log.info("job %s cancelled", job_id)
        ended = []
        try:
            _remove_partials(settings.tmp_dir, job_id)
        except OSError as clean_exc:
            log.warning("job %s: could not remove partial files: %s", job_id, clean_exc)
    except Exception as exc:  # noqa: BLE001 - every failure must land on the job
        log.warning("job %s failed: %s", job_id, exc)
        if not isinstance(exc, DownloadError):
            # yt-dlp already logged its own errors; ours (no output file, move failed) it never saw.
            logger.error(f"ERROR: {exc}")
        try:
            _remove_partials(settings.tmp_dir, job_id)
        except OSError as clean_exc:
            log.warning("job %s: could not remove partial files: %s", job_id, clean_exc)
        if is_transient(str(exc)) and job.attempts < AUTO_RETRIES:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAY)
            store.update(job_id, status=QUEUED, attempts=job.attempts + 1, retry_at=retry_at.isoformat(),
                         error=f"retrying: {trim_error(exc)}", progress=0, speed=None, eta=None,
                         needs_login=False, log=logger.text(), finished_at=None)
        else:
            store.update(job_id, status=FAILED, error=trim_error(exc), speed=None, eta=None,
                         needs_login=looks_login_walled(str(exc)), log=logger.text(),
                         finished_at=_now_iso())
    finally:
        cookie_store.discard_once(job_id)   # use-once cookies live exactly one attempt
    return ended
