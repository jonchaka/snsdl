"""Image posts (photos, carousels, mixed photo/video posts) through gallery-dl's Python API.

Used as a fallback when yt-dlp cannot handle a URL: `probe()` lists a post's files without
downloading, `download()` fetches them into the job's own tmp directory and moves them to
downloads/ as `<job_id>-<stem>-<nn>.<ext>`.

gallery-dl's configuration is process-global, so every operation (probe or download) runs
under CONFIG_LOCK on a freshly cleared config, cleared again afterwards: one gallery-dl
operation at a time per process.

Only single-post extractors are accepted (POSTS: (category, subcategory) pairs), never an
account, board, feed or search, so the fallback cannot turn the app into a generic scraper of
arbitrary web pages or of whole accounts. Share and redirect links (imgur gallery, pin.it,
vm.tiktok.com, reddit /s/) are followed one level deep through gallery-dl's Message.Queue, to
a child extractor passing the same check."""
import contextlib
import logging
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from gallery_dl import config as gdl_config
from gallery_dl import exception as gdl_exception
from gallery_dl import extractor as gdl_extractor
from gallery_dl import job as gdl_job
from gallery_dl import output as gdl_output
from gallery_dl.extractor.message import Message
from yt_dlp.utils import sanitize_filename

from . import downloader, files, probe as probe_mod, thumbs
from .config import Settings
from .cookies import CookieStore
from .jobs import AUTO_RETRIES, DONE, FAILED, QUEUED, RETRY_DELAY, JobStore, _now_iso

log = logging.getLogger(__name__)

MAX_FILES = 200        # files listed or downloaded per post
PROBE_TIMEOUT = 30     # gallery-dl HTTP timeout (seconds), probes and downloads
RETRIES = 2
TITLE_CHARS = 120
STEM_CHARS = 60
# The single-post extractors taken, as gallery-dl (category, subcategory) pairs: anything
# else (an account, a board, a feed, a search, an unknown new subcategory) is refused.
POSTS = {("instagram", "post"), ("twitter", "tweet"), ("twitter", "image"),
         ("facebook", "photo"), ("facebook", "set"), ("pinterest", "pin"), ("pinterest", "pinit"),
         ("tumblr", "post"), ("reddit", "submission"), ("reddit", "redirect"), ("reddit", "image"),
         ("bluesky", "post"), ("imgur", "image"), ("imgur", "album"), ("imgur", "gallery"),
         ("tiktok", "post"), ("tiktok", "vmpost"), ("flickr", "image"), ("flickr", "album"),
         ("vsco", "image"), ("vsco", "video"), ("weibo", "status")}
SITES = {c for c, _ in POSTS}   # categories (gallery-dl's whitelist, /api/status)
VIDEO_EXTS = {"mp4", "webm", "mov", "m4v"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic"} | VIDEO_EXTS
FALLBACK_EXT = "bin"
NO_FILES = "No photos found in this post"
UNSUPPORTED = "This site is not supported for photo posts"
TITLE_KEYS = ("content", "text", "description", "title", "caption")
UPLOADER_KEYS = ("username", "user", "author")
UPLOADER_DICT_KEYS = ("handle", "name", "username")
COOKIE_SECRET_MIN = 8   # cookie values at least this long are scrubbed from stored logs
INDEX_KEY = "sns_index"  # our own per-file counter, set in handle_url, used by the file name
FILENAME = "{" + INDEX_KEY + ":>02}.{extension}"
# Loggers that are not gallery-dl's (it logs under its extractor categories, "download",
# "http", "gallery-dl", ...), excluded from a job's captured log.
FOREIGN_LOGGERS = ("app", "uvicorn", "asyncio", "urllib3", "httpx", "httpcore", "apscheduler",
                   "yt_dlp", "multipart", "watchfiles")

# gallery-dl's http downloader warns "'429 Too Many Requests' for '<url>' (n/m)" per retry.
HTTP_RETRY_STATUS = re.compile(r"^'(429|5\d\d) ")
HTTP_LOGGER = "downloader.http"
CACHE_FILE = "gallery-dl-cache.sqlite3"   # under <data dir>/config, not the user's home

CONFIG_LOCK = threading.Lock()

# Indirections looked up at call time, so tests can inject fakes (no network).
find = gdl_extractor.find
job_class = gdl_job.DownloadJob


def _allowed(extr) -> bool:
    """An extractor (instance or class) we take: its (category, subcategory) is in POSTS."""
    return extr is not None and (getattr(extr, "category", None),
                                 getattr(extr, "subcategory", None)) in POSTS


def supports(url: str) -> bool:
    """True when gallery-dl has a single-post extractor for `url` (see POSTS). Never raises."""
    try:
        return _allowed(find(url))
    except Exception:  # noqa: BLE001 - anything odd means "not an image post we take"
        return False


def ext_of(value) -> str:
    """A lower-case extension from IMAGE_EXTS, else FALLBACK_EXT."""
    ext = str(value or "").strip().lstrip(".").lower()
    return ext if ext in IMAGE_EXTS else FALLBACK_EXT


def _positive_int(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _first_line(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    for line in value.splitlines():
        if line.strip():
            return line.strip()[:TITLE_CHARS]
    return None


def _caption(kwdicts: list[dict]) -> Optional[str]:
    """The post text: the first non-empty value among TITLE_KEYS, whole."""
    for kw in kwdicts:
        for key in TITLE_KEYS:
            value = kw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _uploader(kwdicts: list[dict]) -> Optional[str]:
    for kw in kwdicts:
        for key in UPLOADER_KEYS:
            value = kw.get(key)
            if isinstance(value, dict):
                for sub in UPLOADER_DICT_KEYS:
                    if isinstance(value.get(sub), str) and value[sub].strip():
                        return value[sub].strip()
            elif isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _title(kwdicts: list[dict]) -> Optional[str]:
    return _first_line(_caption(kwdicts)) or _uploader(kwdicts)


def _upload_date(kw: dict) -> Optional[str]:
    date = kw.get("date")
    if isinstance(date, datetime):
        return date.strftime("%Y-%m-%d")
    ts = kw.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        try:
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _cookie_values(cookiefile: Optional[Path]) -> list[str]:
    """Values in a Netscape cookies file worth scrubbing (long enough not to hit plain text)."""
    if cookiefile is None:
        return []
    try:
        text = Path(cookiefile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    values = []
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        if len(fields) >= 7 and len(fields[6].strip()) >= COOKIE_SECRET_MIN:
            values.append(fields[6].strip())
    return values


def _scrubber(settings: Settings, cookiefile: Optional[Path]) -> downloader.JobLogger:
    secrets = [settings.proxy or "", str(cookiefile) if cookiefile else "", *_cookie_values(cookiefile)]
    return downloader.JobLogger(settings.tmp_dir, secrets=secrets)


class _Capture(logging.Handler):
    """Root-logger handler feeding gallery-dl's records from one thread into a JobLogger
    (which scrubs and ring-buffers them); remembers the last error line and exception, and
    whether gallery-dl's http downloader warned of a transient failure (429, 5xx, timeout).
    Records below INFO are dropped."""

    def __init__(self, sink: downloader.JobLogger):
        super().__init__()
        self.sink = sink
        self.thread = threading.get_ident()
        self.last_error: Optional[str] = None
        self.last_exc: Optional[BaseException] = None
        self.http_transient = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if (record.levelno < logging.INFO or record.thread != self.thread
                    or record.name.split(".")[0] in FOREIGN_LOGGERS):
                return
            message = record.getMessage()
            if (record.name == HTTP_LOGGER and record.levelno >= logging.WARNING
                    and (HTTP_RETRY_STATUS.match(message) or downloader.is_transient(message))):
                self.http_transient = True
            args = record.args if isinstance(record.args, tuple) else ()
            for arg in args:
                if isinstance(arg, BaseException):
                    self.last_exc = arg
            if record.levelno >= logging.ERROR:
                self.last_error = message
            self.sink.error(f"[{record.name}][{record.levelname.lower()}] {message}")
        except Exception:  # noqa: BLE001 - logging must never break the download
            pass


@contextlib.contextmanager
def _session(settings: Settings, cookiefile: Optional[Path], base: Optional[Path],
             sink: Optional[downloader.JobLogger] = None) -> Iterator[Optional[_Capture]]:
    """Hold CONFIG_LOCK with gallery-dl configured for one operation; with `sink`, capture
    gallery-dl's log records into it. Config cleared and handler removed afterwards."""
    with CONFIG_LOCK:
        capture = _Capture(sink) if sink is not None else None
        root = logging.getLogger()
        try:
            gdl_config.clear()
            if base is not None:
                gdl_config.set((), "base-directory", str(base))
            gdl_config.set((), "directory", [])
            gdl_config.set((), "filename", FILENAME)
            gdl_config.set((), "skip", False)
            gdl_config.set(("output",), "mode", "null")
            if cookiefile is not None:
                gdl_config.set(("extractor",), "cookies", str(cookiefile))
                gdl_config.set(("extractor",), "cookies-update", False)
            if settings.proxy:
                gdl_config.set(("extractor",), "proxy", settings.proxy)
            gdl_config.set(("extractor",), "timeout", PROBE_TIMEOUT)
            gdl_config.set(("extractor",), "retries", RETRIES)
            gdl_config.set(("extractor",), "image-range", f"1-{MAX_FILES}")
            # DownloadJob follows Message.Queue children itself: only into SITES.
            gdl_config.set(("extractor",), "whitelist", sorted(SITES))
            if settings.rate_limit:
                gdl_config.set(("downloader",), "rate", int(settings.rate_limit))   # bytes/s
            cache_dir = settings.data_dir / "config"
            with contextlib.suppress(OSError):
                cache_dir.mkdir(parents=True, exist_ok=True)
            gdl_config.set(("cache",), "file", str(cache_dir / CACHE_FILE))
            if capture is not None:
                root.addHandler(capture)
            yield capture
        finally:
            if capture is not None:
                root.removeHandler(capture)
            gdl_config.clear()


def _extractor(url: str):
    extr = find(url)
    if not _allowed(extr):
        raise probe_mod.ProbeError(UNSUPPORTED)
    return extr


def _child(url, kwdict) -> Optional[object]:
    """The extractor a Message.Queue item points at (its kwdict's `_extractor` class, else
    find()), or None when there is none or it is not one we take (see _allowed)."""
    if not isinstance(url, str):
        return None
    cls = kwdict.get("_extractor") if isinstance(kwdict, dict) else None
    extr = cls.from_url(url) if cls is not None else find(url)
    return extr if _allowed(extr) else None


class _ListingJob:
    """What gallery-dl's Job.__init__ hands an extractor's logger (as `_logger_extra`), for the
    listing pass, where no Job runs. No file is being written there, so `pathfmt` is None."""

    pathfmt = None

    def __init__(self, extr):
        self._logger_extra = {"job": self, "extractor": extr,
                              "path": gdl_output.PathfmtProxy(self),
                              "keywords": gdl_output.KwdictProxy(self)}


def _wrap_log(extr):
    """Wrap the extractor's logger like Job.__init__ does (gallery-dl's output.LoggerAdapter):
    extractors call `self.log.traceback(exc)` in their recoverable error paths, which a plain
    logging.Logger lacks. An already wrapped logger is left alone."""
    logger = getattr(extr, "log", None)
    if not hasattr(logger, "traceback"):
        if not isinstance(logger, logging.Logger):
            logger = logging.getLogger(str(getattr(extr, "category", None) or "gallery-dl"))
        extr.log = gdl_output.LoggerAdapter(logger, _ListingJob(extr))
    return extr


def _list(url: str) -> tuple[list[tuple[str, dict]], list[dict]]:
    """Iterate the post's extractor (no download): ([(file url, kwdict)] up to MAX_FILES,
    [directory kwdicts]). Message.Queue items (share and redirect links) are followed one
    level deep into children _allowed; a Queue inside a child is ignored. An error part-way
    keeps the files listed so far (warned in the extractor's log, so in a job's log) unless
    none were listed or it is a login error, which is raised. Must run inside _session."""
    items: list[tuple[str, dict]] = []
    dirs: list[dict] = []

    def walk(extr, depth: int) -> None:
        _wrap_log(extr)
        try:
            for msg in extr:
                if not isinstance(msg, tuple) or not msg:
                    continue
                if msg[0] == Message.Directory and len(msg) >= 2 and isinstance(msg[-1], dict):
                    dirs.append(msg[-1])
                elif msg[0] == Message.Url and len(msg) >= 3 and isinstance(msg[2], dict):
                    items.append((msg[1], msg[2]))
                elif msg[0] == Message.Queue and depth == 0 and len(msg) >= 3:
                    child = _child(msg[1], msg[2])
                    if child is not None:
                        walk(child, 1)
                if len(items) >= MAX_FILES:
                    return
        except gdl_exception.StopExtraction:
            return   # gallery-dl's own "nothing more", not an error (Job.run ignores it too)
        except Exception as exc:  # noqa: BLE001 - kept files beat a late failure
            if not items or _needs_login(exc, str(exc)):
                raise
            extr.log.warning("Listing stopped after %d file(s) (%s: %s)",
                             len(items), exc.__class__.__name__, exc)

    walk(_extractor(url), 0)
    return items, dirs


def _http_status(exc: Optional[BaseException]) -> int:
    status = getattr(exc, "status", 0) if isinstance(exc, gdl_exception.HttpError) else 0
    return status if isinstance(status, int) else 0


def _needs_login(exc: Optional[BaseException], message: str) -> bool:
    if isinstance(exc, (gdl_exception.AuthorizationError, gdl_exception.AuthenticationError)):
        return True
    if _http_status(exc) in (401, 403):
        return True
    if isinstance(exc, probe_mod.ProbeError):
        return exc.needs_login
    return downloader.looks_login_walled(message)


def _transient(exc: Optional[BaseException], message: str) -> bool:
    if _needs_login(exc, message):
        return False
    status = _http_status(exc)
    return status == 429 or status >= 500 or downloader.is_transient(message)


def _kwdicts(items: list[tuple[str, dict]], dirs: list[dict]) -> list[dict]:
    return ([items[0][1]] if items else []) + dirs


def _thumb_url(items: list[tuple[str, dict]]) -> Optional[str]:
    if not items:
        return None
    url, kw = items[0]
    if ext_of(kw.get("extension")) in IMAGE_EXTS - VIDEO_EXTS and thumbs.is_http_url(url):
        return url
    return None


def probe(url: str, settings: Settings, cookiefile: Optional[Path] = None) -> dict:
    """Blocking. Lists the post's files without downloading: {"kind": "images", "title",
    "count", "video_count", "files": [{"ext", "width", "height"}], "thumb_url"}. Raises
    probe.ProbeError (needs_login when the site asked for a login; NO_FILES for none)."""
    scrub = _scrubber(settings, cookiefile)
    with _session(settings, cookiefile, None):
        try:
            items, dirs = _list(url)
        except probe_mod.ProbeError:
            raise
        except Exception as exc:  # noqa: BLE001 - any gallery-dl failure becomes a message
            message = scrub.scrub(downloader.trim_error(exc))
            raise probe_mod.ProbeError(message, needs_login=_needs_login(exc, str(exc))) from exc
    if not items:
        raise probe_mod.ProbeError(NO_FILES)
    listed = [{"ext": ext_of(kw.get("extension")), "width": _positive_int(kw.get("width")),
               "height": _positive_int(kw.get("height"))} for _, kw in items]
    return {"kind": "images", "title": _title(_kwdicts(items, dirs)), "count": len(listed),
            "video_count": sum(1 for f in listed if f["ext"] in VIDEO_EXTS), "files": listed,
            "thumb_url": _thumb_url(items)}


class _Failed(Exception):
    """gallery-dl's DownloadJob finished with a non-zero status (it logs, never raises)."""

    def __init__(self, message: str, cause: Optional[BaseException], transient: bool = False):
        super().__init__(message)
        self.cause = cause
        self.transient = transient   # a file failed after transient http warnings


def _make_job(base, on_file):
    """A subclass of `base` (gallery-dl's DownloadJob or a test fake) calling on_file(kwdict)
    before every file, and refusing Queue children whose `_extractor` class is not _allowed
    (gallery-dl applies the "whitelist" option only to children it finds by URL, and only to
    the category), so listing (_list) and download take the same children."""

    class _Job(base):
        def handle_url(self, url, kwdict):
            on_file(kwdict)
            return super().handle_url(url, kwdict)

        def handle_queue(self, url, kwdict):
            cls = kwdict.get("_extractor") if isinstance(kwdict, dict) else None
            if not _allowed(cls if cls is not None else find(url)):
                return None
            return super().handle_queue(url, kwdict)

    return _Job


def _remove_dir(work: Path, job_id: str) -> None:
    try:
        shutil.rmtree(work, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("job %s: could not remove %s: %s", job_id, work.name, exc)


def _downloaded(work: Path) -> list[Path]:
    """Finished files in the work directory, in listing (index) order."""
    out = []
    for p in work.iterdir() if work.is_dir() else []:
        if not p.is_file() or p.is_symlink() or p.name.endswith(downloader.PARTIAL_SUFFIXES):
            continue
        head = p.name.split(".", 1)[0]
        out.append((int(head) if head.isdigit() else 10 ** 9, p.name, p))
    return [p for _, _, p in sorted(out)]


def download(job_id: str, url: str, store: JobStore, settings: Settings,
             prior_error: Optional[str] = None) -> list[str]:
    """Blocking. Downloads every file of the post for `job_id`, leaving the job done, failed
    or (transient failure with retries left) queued with retry_at. Never raises. Returns
    [job_id], or [] when the job was removed (cancelled) before or during the download.
    `prior_error` (yt-dlp's trimmed error when it handed the job over) becomes the first line
    of the log of a failed job, so Details show why yt-dlp gave up."""
    cookie_store = CookieStore(settings)
    job = store.get(job_id)
    if job is None:
        cookie_store.discard_once(job_id)
        return []
    work = settings.tmp_dir / f"{job_id}-images"
    sink = downloader.JobLogger(settings.tmp_dir, secrets=[settings.proxy] if settings.proxy else ())
    state = {"done": 0, "cancelled": False, "written": None}
    ended = [job_id]
    try:
        work.mkdir(parents=True, exist_ok=True)
        with cookie_store.merged_for(job_id, job_id=job_id) as cookiefile:
            sink = _scrubber(settings, cookiefile)
            with _session(settings, cookiefile, work, sink) as capture:
                items, dirs = _list(url)
                if not items:
                    raise probe_mod.ProbeError(NO_FILES)
                if store.get(job_id) is None:
                    raise downloader.DownloadCancelled(job_id)
                total = len(items)

                def on_file(kwdict: dict) -> None:
                    if store.get(job_id) is None:
                        state["cancelled"] = True
                        raise downloader.DownloadCancelled(job_id)
                    if state["done"] >= MAX_FILES:
                        raise gdl_exception.StopExtraction()
                    pct = min(99, int(state["done"] * 100 / total))
                    if pct != state["written"]:
                        state["written"] = pct
                        store.update(job_id, progress=pct)
                    state["done"] += 1
                    kwdict[INDEX_KEY] = state["done"]
                    kwdict["extension"] = ext_of(kwdict.get("extension"))

                gjob = _make_job(job_class, on_file)(_extractor(url))
                status = gjob.run()
                if state["cancelled"]:
                    raise downloader.DownloadCancelled(job_id)
                if status:
                    message = capture.last_error or f"gallery-dl failed (status {status})"
                    raise _Failed(message, capture.last_exc, transient=capture.http_transient)
        got = _downloaded(work)
        if not got:
            raise RuntimeError("download finished but no files were found")
        kwdicts = _kwdicts(items, dirs)
        current = store.get(job_id)
        if current is None:
            raise downloader.DownloadCancelled(job_id)
        title = current.title or _title(kwdicts)
        stem = (downloader.safe_stem(current.custom_name)
                or sanitize_filename(title or "", restricted=True).lstrip(".-")[:STEM_CHARS] or "post")
        if current.thumb is None:
            thumb_url = _thumb_url(items)
            if thumb_url is not None:
                downloader.fetch_job_thumb(job_id, thumb_url, store, settings)
        names: list[str] = []
        size = 0
        try:
            for nn, src in enumerate(got, 1):
                name = files.unique_name(settings.downloads_dir,
                                         f"{job_id}-{stem}-{nn:02d}.{ext_of(src.suffix)}")
                shutil.move(str(src), str(settings.downloads_dir / name))
                names.append(name)
                size += (settings.downloads_dir / name).stat().st_size
        except BaseException:
            for name in names:
                (settings.downloads_dir / name).unlink(missing_ok=True)
            raise
        _remove_dir(work, job_id)
        caption = _caption(kwdicts)
        if store.update(job_id, status=DONE, progress=100, filename=names[0], sidecars=names[1:],
                        title=title, size=size, duration=None, speed=None, eta=None, note=None,
                        finished_at=_now_iso(), uploader=_uploader(kwdicts),
                        upload_date=_upload_date(items[0][1]),
                        description=caption[:downloader.DESCRIPTION_CHARS] if caption else None) is None:
            for name in names:   # removed while finishing: leave no orphan files
                (settings.downloads_dir / name).unlink(missing_ok=True)
            ended = []
    except downloader.DownloadCancelled:
        log.info("job %s cancelled", job_id)
        ended = []
        _remove_dir(work, job_id)
    except Exception as exc:  # noqa: BLE001 - every failure must land on the job
        cause = exc.cause if isinstance(exc, _Failed) else exc
        raw = str(exc)
        message = sink.scrub(downloader.trim_error(exc))
        log.warning("job %s failed: %s", job_id, message)
        if not isinstance(exc, _Failed):
            sink.error(f"ERROR: {raw}")   # gallery-dl never saw it (listing pass or ours)
        _remove_dir(work, job_id)
        text = sink.text()
        if prior_error:
            text = sink.scrub(f"yt-dlp: {prior_error}") + ("\n" + text if text else "")
        transient = _transient(cause, raw) or (
            isinstance(exc, _Failed) and exc.transient and not _needs_login(cause, raw))
        if transient and job.attempts < AUTO_RETRIES:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAY)
            store.update(job_id, status=QUEUED, attempts=job.attempts + 1, retry_at=retry_at.isoformat(),
                         error=f"retrying: {message}", progress=0, speed=None, eta=None,
                         needs_login=False, log=text, finished_at=None)
        else:
            store.update(job_id, status=FAILED, error=message, speed=None, eta=None,
                         needs_login=_needs_login(cause, raw), log=text, finished_at=_now_iso())
    finally:
        cookie_store.discard_once(job_id)   # use-once cookies live exactly one attempt
    return ended
