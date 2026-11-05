"""Live streams: resolve the stream with yt-dlp, record it with ffmpeg until the stream ends or
the user (or a limit) stops it."""
import http.cookiejar
import logging
import math
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import sanitize_filename

from . import downloader, thumbs
from .config import Settings
from .cookies import CookieStore
from .jobs import DONE, FAILED, Job, JobStore, _now_iso
from .probe import ProbeError, UPCOMING, _common_opts, _extract

log = logging.getLogger(__name__)

MAX_RECORDINGS = 3      # recordings running at once (they take no worker or site slot)
STOP_GRACE = 10.0       # seconds between the quit key and a kill
WRITE_INTERVAL = 2.0    # seconds between progress writes
STREAM_TIMEOUT = 30     # ffmpeg -rw_timeout in seconds, so a dead stream ends the recording

EXT_FOR_FORMAT = {"mp4": "mp4", "mp3": "mp3", "audio": "m4a"}
FRAGMENTED_MP4 = "+frag_keyframe+empty_moov+default_base_moof"   # playable even if cut off

NOTE_ENDED = "Stream ended"
NOTE_ENDED_ERROR = "Stream ended with an error; the recording is kept up to that point"
NOTE_STOPPED = "Recording stopped"
NOTE_LOW_DISK = "Recording stopped: low disk space"
NOTE_MAX_HOURS = "Recording stopped after {hours} h (LIVE_MAX_HOURS)"
NO_SINGLE_STREAM = "no single live stream to record"
SOCKS_UNSUPPORTED = "live recording needs an HTTP proxy or none; SOCKS is not supported by ffmpeg"


def format_selector(fmt: str, quality: str) -> str:
    """One muxed, non-DASH stream (DASH live cannot be read by ffmpeg)."""
    if fmt in ("mp3", "audio"):
        return "bestaudio[protocol!*=dash]/best[protocol!*=dash]"
    if quality == "best":
        return "best[protocol!*=dash]"
    return f"best[protocol!*=dash][height<={quality}]/best[protocol!*=dash]"


@dataclass
class Stream:
    url: Optional[str]
    headers: dict[str, str] = field(default_factory=dict)
    title: Optional[str] = None
    uploader: Optional[str] = None
    upload_date: Optional[str] = None      # YYYY-MM-DD
    description: Optional[str] = None
    thumbnail: Optional[str] = None
    is_live: bool = False
    duration: Optional[int] = None


def cookie_header(cookiefile: Optional[Path], url: str) -> Optional[str]:
    """The Cookie header value the cookies file holds for `url`, or None. Never raises."""
    if cookiefile is None:
        return None
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookiefile))
        jar.load(ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            if not cookie.expires:   # 0 marks a session cookie in exports; the jar would see it expired
                cookie.expires = None
        request = urllib.request.Request(url)
        jar.add_cookie_header(request)
        return request.get_header("Cookie") or None
    except Exception as exc:  # noqa: BLE001 - recording without cookies beats not recording
        log.warning("live: could not read cookies for the stream: %s", exc.__class__.__name__)
        return None


def resolve(url: str, settings: Settings, fmt: str, quality: str, cookiefile: Optional[Path] = None,
            ydl_class=yt_dlp.YoutubeDL) -> Stream:
    """Blocking. Ask yt-dlp for the stream ffmpeg should read. Raises ProbeError."""
    opts = {**_common_opts(settings, cookiefile), "format": format_selector(fmt, quality), "noplaylist": True}
    # A stream that ended may have no non-DASH format; yt-dlp then returns the info (no url)
    # instead of raising, so the job can still be downloaded normally.
    info = _extract(url, {**opts, "ignore_no_formats_error": True}, ydl_class)
    is_live = info.get("is_live") is True
    if info.get("live_status") == "is_upcoming" and not is_live:
        raise ProbeError(UPCOMING)
    meta = downloader.metadata_fields(info)
    stream = Stream(url=None, title=info.get("title") if isinstance(info.get("title"), str) else None,
                    uploader=meta["uploader"], upload_date=meta["upload_date"],
                    description=meta["description"],
                    thumbnail=info.get("thumbnail") if thumbs.is_http_url(info.get("thumbnail")) else None,
                    is_live=is_live, duration=meta["duration"])
    if not is_live:
        return stream   # recorded as a normal download; no stream URL needed
    if info.get("requested_formats"):
        raise ProbeError(NO_SINGLE_STREAM)
    if not isinstance(info.get("url"), str) or not info.get("url"):
        # Nothing selected: extract again without the flag so the real reason (login wall, geo
        # block, no matching format) surfaces as the ProbeError.
        _extract(url, opts, ydl_class)
        raise ProbeError(NO_SINGLE_STREAM)
    stream.url = info["url"]
    raw = info.get("http_headers")
    headers = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    cookie = cookie_header(cookiefile, stream.url)
    if cookie:
        headers["Cookie"] = cookie
    stream.headers = headers
    return stream


def ffmpeg_args(stream: Stream, out: Path, fmt: str, quality: str, settings: Settings) -> list[str]:
    """The ffmpeg command line recording `stream` into `out`. ValueError for a SOCKS proxy."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
            "-rw_timeout", str(STREAM_TIMEOUT * 1_000_000)]
    if settings.proxy:
        if urlsplit(settings.proxy).scheme.lower() not in ("http", "https"):
            raise ValueError(SOCKS_UNSUPPORTED)
        args += ["-http_proxy", settings.proxy]
    if stream.headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in stream.headers.items())]
    args += ["-i", stream.url]
    if fmt == "mp3":
        args += ["-vn", "-c:a", "libmp3lame"]
        args += ["-q:a", "0"] if quality == "best" else ["-b:a", f"{quality}k"]
        args += ["-f", "mp3", str(out)]
    elif fmt == "audio":
        args += ["-vn", "-c:a", "copy", "-movflags", FRAGMENTED_MP4, "-f", "mp4", str(out)]
    else:
        args += ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", FRAGMENTED_MP4, "-f", "mp4", str(out)]
    return args


def output_path(settings: Settings, job: Job) -> Path:
    stem = job.custom_name or sanitize_filename(job.title or "", restricted=True)[:80] or "live"
    return settings.tmp_dir / f"{job.id}-{stem}.{EXT_FOR_FORMAT.get(job.format, 'mp4')}"


def parse_progress(line: str) -> Optional[tuple[str, str]]:
    """One `-progress` line `key=value` -> (key, value); None for anything else."""
    key, sep, value = line.strip().partition("=")
    if not sep or not key:
        return None
    return key.strip(), value.strip()


def _int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None   # "N/A" before the first packet


def _secrets(settings: Settings, headers: dict[str, str]) -> list[str]:
    """What the stderr log must never show: the proxy URL and the Cookie header (whole and per
    cookie)."""
    out = [settings.proxy] if settings.proxy else []
    cookie = headers.get("Cookie")
    if cookie:
        out.append(cookie)
        out += [part.strip() for part in cookie.split(";") if "=" in part]
    return out


def _quit(proc) -> None:
    """Ask ffmpeg to finish the file (the quit key), give it STOP_GRACE, then kill it."""
    try:
        proc.stdin.write(b"q\n")
        proc.stdin.flush()
    except (OSError, ValueError):
        pass   # ffmpeg may already be gone
    try:
        proc.wait(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        _kill(proc)


def _kill(proc) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        log.warning("live: ffmpeg did not exit after kill")


def _fail(store: JobStore, job_id: str, error: str, log_text: Optional[str] = None, **extra) -> list[str]:
    store.update(job_id, status=FAILED, error=error[:200], speed=None, eta=None, stop_requested=False,
                 log=log_text, finished_at=_now_iso(), **extra)
    return [job_id]


def record(job_id: str, store: JobStore, settings: Settings, popen=subprocess.Popen,
           clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Blocking. Record the live job `job_id` with ffmpeg, leaving it done or failed. Never raises.
    Returns [job_id], or [] when the job was removed (cancelled); a stream that is no longer live
    is downloaded normally (downloader.download's result)."""
    try:
        return _record(job_id, store, settings, popen, clock)
    except Exception as exc:  # noqa: BLE001 - every failure must land on the job
        log.exception("live: job %s failed", job_id)
        try:
            if store.get(job_id) is None:
                return []
            return _fail(store, job_id, downloader.trim_error(exc))
        except Exception:  # noqa: BLE001 - the store itself may be broken
            log.exception("live: could not mark job %s failed", job_id)
            return [job_id]


def _record(job_id, store, settings, popen, clock) -> list[str]:
    cookie_store = CookieStore(settings)
    job = store.get(job_id)
    if job is None:
        cookie_store.discard_once(job_id)
        return []
    delegated = False
    try:
        with cookie_store.merged_for(job_id, job_id=job_id) as cookiefile:
            try:
                stream = resolve(job.url, settings, job.format, job.quality, cookiefile)
            except ProbeError as exc:
                return _fail(store, job_id, str(exc), needs_login=exc.needs_login)
        if not stream.is_live:
            log.info("live: job %s is not live any more; downloading it", job_id)
            delegated = True   # download() uses (and then discards) the use-once cookies itself
            return downloader.download(job_id, job.url, store, settings)
    finally:
        if not delegated:
            cookie_store.discard_once(job_id)

    job = store.update(job_id, title=stream.title or job.title, uploader=stream.uploader,
                       upload_date=stream.upload_date, description=stream.description)
    if job is None:
        return []
    out = output_path(settings, job)
    try:
        args = ffmpeg_args(stream, out, job.format, job.quality, settings)
    except ValueError as exc:
        return _fail(store, job_id, str(exc))
    if job.thumb is None and stream.thumbnail:
        downloader.fetch_job_thumb(job_id, stream.thumbnail, store, settings)
    return _run_ffmpeg(job_id, store, settings, args, out, stream, popen, clock)


def _run_ffmpeg(job_id, store, settings, args, out: Path, stream: Stream, popen, clock) -> list[str]:
    logger = downloader.JobLogger(settings.tmp_dir, secrets=_secrets(settings, stream.headers))
    out.unlink(missing_ok=True)
    try:
        proc = popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return _fail(store, job_id, f"could not start ffmpeg: {downloader.trim_error(exc)}")
    reader: Optional[threading.Thread] = None
    finished = False
    try:
        reader = _start_reader(job_id, proc, logger)
        result = _supervise(job_id, store, settings, proc, reader, logger, out, clock)
        finished = True
        return result
    finally:
        _cleanup(proc, reader)
        if not finished:
            out.unlink(missing_ok=True)


def _cleanup(proc, reader: Optional[threading.Thread]) -> None:
    """Whatever happened: ffmpeg is not left running, its pipes are closed, the stderr reader
    is joined (bounded)."""
    try:
        if proc.poll() is None:
            _kill(proc)
    except Exception:  # noqa: BLE001 - cleanup must not mask the original outcome
        log.exception("live: could not stop ffmpeg")
    for name in ("stdin", "stdout"):
        _close(getattr(proc, name, None))
    if reader is not None:
        reader.join(timeout=STOP_GRACE)
    # Closing stderr while the reader still blocks in readline would block here too.
    if reader is None or not reader.is_alive():
        _close(getattr(proc, "stderr", None))


def _close(pipe) -> None:
    try:
        if pipe is not None:
            pipe.close()
    except Exception:  # noqa: BLE001 - best effort
        pass


def _start_reader(job_id, proc, logger) -> threading.Thread:
    def drain() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                logger.warning(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=drain, name=f"ffmpeg-stderr-{job_id}", daemon=True)
    reader.start()
    return reader


def _supervise(job_id, store, settings, proc, reader, logger, out: Path, clock) -> list[str]:
    from . import worker   # at call time: worker imports this module; tests patch worker.disk_low

    started = clock()
    seconds = 0.0                     # recorded length from ffmpeg's out_time
    size: Optional[int] = None
    written_at: Optional[float] = None
    stop_note: Optional[str] = None   # set once we asked ffmpeg to quit
    cancelled = False
    for raw in iter(proc.stdout.readline, b""):
        kv = parse_progress(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
        if kv is not None:
            key, value = kv
            if key in ("out_time_us", "out_time_ms") and _int(value) is not None:
                seconds = max(seconds, _int(value) / 1_000_000)   # both are microseconds in ffmpeg
            elif key == "total_size" and _int(value) is not None:
                size = _int(value)
        current = store.get(job_id)
        if current is None:
            cancelled = True
            break
        if current.stop_requested:
            stop_note = current.note or NOTE_STOPPED
            break
        now = clock()
        if written_at is not None and now - written_at < WRITE_INTERVAL:
            continue
        written_at = now
        if store.update(job_id, duration=int(seconds), size=size) is None:
            cancelled = True
            break
        if max(seconds, now - started) >= settings.live_max_hours * 3600:
            stop_note = NOTE_MAX_HOURS.format(hours=settings.live_max_hours)
            break
        if worker.disk_low(settings):
            stop_note = NOTE_LOW_DISK
            break

    if cancelled:
        log.info("live: job %s cancelled; stopping ffmpeg", job_id)
        _kill(proc)
        reader.join(timeout=STOP_GRACE)
        out.unlink(missing_ok=True)
        return []
    if stop_note is not None:
        log.info("live: job %s: %s", job_id, stop_note)
        _quit(proc)
        code = proc.returncode
    else:
        try:
            code = proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:   # stdout closed but still running: should not happen
            _kill(proc)
            code = proc.returncode
    reader.join(timeout=STOP_GRACE)

    recorded = out.is_file() and out.stat().st_size > 0
    if not recorded:
        out.unlink(missing_ok=True)
        lines = logger.text().splitlines()
        error = lines[-1] if lines else (stop_note or (f"ffmpeg exited with code {code}" if code is not None
                                                         else "ffmpeg was killed"))
        if store.get(job_id) is None:
            return []
        return _fail(store, job_id, error, log_text=logger.text() or None)
    note = stop_note or (NOTE_ENDED if code == 0 else NOTE_ENDED_ERROR)
    dest = settings.downloads_dir / out.name
    shutil.move(str(out), str(dest))
    duration = math.ceil(seconds) if seconds > 0 else None
    if store.update(job_id, status=DONE, progress=100, filename=dest.name, size=dest.stat().st_size,
                    duration=duration, finished_at=_now_iso(), speed=None, eta=None,
                    stop_requested=False, note=note) is None:
        dest.unlink(missing_ok=True)   # removed while we were finishing: no orphan file
        return []
    return [job_id]
