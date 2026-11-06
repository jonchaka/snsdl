"""Telegram delivery: send a finished download (or a link to it) to one chat via the Bot API.

TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID come from the environment only. The token is part of
every request URL, so the URL is never logged, and Telegram's error descriptions are logged
only after the token has been replaced by `***` (some errors echo it back).

Bot API limits that shape the plan: an upload is at most 50 MB (a photo 10 MB), an album holds
2..10 items and a caption 1024 characters. Anything too large is sent as a link instead.
"""
import dataclasses
import json
import logging
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple, Optional

from . import files, webhook
from .config import Settings
from .jobs import DONE, FAILED, Job

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_FILE_BYTES = 50 * 1024 * 1024    # Bot API upload limit
MAX_PHOTO_BYTES = 10 * 1024 * 1024   # Bot API photo limit
ALBUM_MAX = 10                       # items per sendMediaGroup
TIMEOUT = 60                         # seconds per request (uploads of up to 50 MB)
RETRY = 30.0                         # seconds before the single retry of a failed delivery
CAPTION_MAX = 1000                   # under Telegram's 1024-character caption limit
TEXT_MAX = 4000                      # under Telegram's 4096-character message limit
DESCRIPTION_MAX = 120                # characters of Telegram's error description in the log
PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}   # what Telegram takes as a `photo` item
NO_PUBLIC_URL = "(set PUBLIC_URL for a clickable link)"
TEST_TEXT = "SNS Downloader is connected."


class Request(NamedTuple):
    """One Bot API call: method name, text form fields, and files to upload {field: path}.
    `chat_id` is added by send()."""
    method: str
    fields: dict
    files: dict


def redact(text: str, token: Optional[str]) -> str:
    return text.replace(token, "***") if token else text


def _hms(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


def caption(job: Job) -> str:
    """The title (else the URL), then ` · h:mm:ss` when the duration is known; at most
    CAPTION_MAX characters."""
    text = job.title or job.url
    if job.duration:
        text += f" · {_hms(job.duration)}"
    return text[:CAPTION_MAX]


def _link(settings: Settings, path: str) -> str:
    return settings.public_url + path if settings.public_url else path


def _with_link(text: str, settings: Settings, link: str) -> str:
    note = "" if settings.public_url else f"\n{NO_PUBLIC_URL}"
    return f"{text}\n{link}{note}"


def _message(text: str) -> Request:
    return Request("sendMessage", {"text": text[:TEXT_MAX]}, {})


def _existing(settings: Settings, job: Job, name: str) -> Optional[Path]:
    """The file `name` of the job (same directory rules as its main file), None when missing."""
    path = files.file_path(settings, dataclasses.replace(job, filename=name))
    return path if path is not None and path.is_file() else None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return MAX_FILE_BYTES + 1


def _plan_single(job: Job, settings: Settings, method: str, field: str, extra: dict) -> list[Request]:
    path = _existing(settings, job, job.filename)
    if path is None:
        log.info("telegram: file of job %s is missing; nothing sent", job.id)
        return []
    if _size(path) <= MAX_FILE_BYTES:
        return [Request(method, {"caption": caption(job), **extra}, {field: path})]
    return [_message(_with_link(caption(job), settings, webhook.file_url(job, settings)))]


def _plan_images(job: Job, settings: Settings) -> list[Request]:
    names = [job.filename, *job.sidecars]
    present = [p for p in (_existing(settings, job, n) for n in names) if p is not None]
    sendable = [p for p in present
                if p.suffix.lower().lstrip(".") in PHOTO_EXTS and _size(p) <= MAX_PHOTO_BYTES][:ALBUM_MAX]
    if not present:   # like a video or audio post whose file is gone
        log.info("telegram: files of job %s are missing; nothing sent", job.id)
        return []
    left = len(present) - len(sendable)
    zip_link = _link(settings, f"/api/zip?ids={job.id}")
    if not sendable:
        n = len(present)
        return [_message(_with_link(f"{caption(job)}\n{n} file{'s' if n != 1 else ''}", settings, zip_link))]
    out: list[Request]
    if len(sendable) == 1:   # an album needs at least two items
        out = [Request("sendPhoto", {"caption": caption(job)}, {"photo": sendable[0]})]
    else:
        media, attached = [], {}
        for i, path in enumerate(sendable):
            item = {"type": "photo", "media": f"attach://photo{i}"}
            if i == 0:
                item["caption"] = caption(job)
            media.append(item)
            attached[f"photo{i}"] = path
        out = [Request("sendMediaGroup", {"media": json.dumps(media)}, attached)]
    if left:
        out.append(_message(_with_link(f"{left} more file{'s' if left != 1 else ''}", settings, zip_link)))
    return out


def plan(job: Job, settings: Settings) -> list[Request]:
    """The Bot API calls that deliver this ended job: the file itself when Telegram takes it,
    else a message with a link; a failed job gets one message. Missing files are skipped."""
    if job.status == FAILED:
        text = f"Failed: {job.title or job.url}"
        if job.error:
            text += f" · {job.error}"
        return [_message(text)]
    if job.status != DONE or not job.filename:
        return []
    if job.format == "images":
        return _plan_images(job, settings)
    if job.format in ("mp3", "audio"):
        extra = {k: v for k, v in (("title", job.title), ("performer", job.uploader),
                                   ("duration", job.duration)) if v}
        return _plan_single(job, settings, "sendAudio", "audio", {k: str(v) for k, v in extra.items()})
    extra = {"supports_streaming": "true"}
    if job.duration:
        extra["duration"] = str(job.duration)
    return _plan_single(job, settings, "sendVideo", "video", extra)


def _quote(value: str) -> str:
    """A Content-Disposition parameter value: no quotes or line breaks."""
    return value.replace('"', "_").replace("\r", "_").replace("\n", "_").replace("\\", "_")


def encode_multipart(fields: dict, upload: dict) -> tuple[bytes, str]:
    """(body, content type) of a multipart/form-data request. Files are read whole into memory:
    they are at most MAX_FILE_BYTES (50 MB), which keeps this dependency-free and simple."""
    boundary = "sns-" + secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"\r\n\r\n'
                     .encode() + str(value).encode() + b"\r\n")
    for name, path in upload.items():
        path = Path(path)
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"; '
                f'filename="{_quote(path.name)}"\r\nContent-Type: application/octet-stream\r\n\r\n')
        parts.append(head.encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


_OPENER = urllib.request.build_opener(webhook._NoRedirect())


def _description(raw: bytes, token: str) -> str:
    try:
        desc = json.loads(raw.decode("utf-8", errors="replace")).get("description")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(desc, str):
        return ""
    return redact(desc, token).replace("\n", " ")[:DESCRIPTION_MAX]


def _fail(method: str, reason: str, desc: str) -> bool:
    log.warning("telegram %s failed: %s%s", method, reason, f" ({desc})" if desc else "")
    return False


def send(settings: Settings, request: Request, opener=None) -> bool:
    """Blocking. POST the request (multipart, chat_id added) to the Bot API; True on HTTP 200
    with "ok": true. Failures are logged with the method, the HTTP status or exception class
    and the redacted description only: never the URL (it holds the token) nor the body.
    Redirects are not followed. `opener` is a urlopen-like callable (tests)."""
    token = settings.telegram_bot_token
    if not settings.telegram:
        return False
    method = request.method
    open_fn = opener if opener is not None else _OPENER.open
    try:
        body, ctype = encode_multipart({"chat_id": settings.telegram_chat_id, **request.fields},
                                       request.files)
        req = urllib.request.Request(f"{API}/bot{token}/{method}", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        with open_fn(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", None)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        try:
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        return _fail(method, f"HTTP {exc.code}", _description(raw or b"", token))
    except Exception as exc:  # noqa: BLE001 - delivery must never break the worker
        return _fail(method, type(exc).__name__, "")
    if status != 200:
        return _fail(method, f"HTTP {status}", _description(raw or b"", token))
    try:
        ok = json.loads(raw.decode("utf-8", errors="replace")).get("ok") is True
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        return _fail(method, "not ok", _description(raw, token))
    return True


def deliver(settings: Settings, job: Job, skip: int = 0) -> Optional[int]:
    """Blocking. Send the requests of plan() from number `skip` on; stop at the first failure.
    None when everything (possibly nothing) was sent, else the number of requests of the plan
    sent before the failure, which a retry passes back as `skip` so nothing (an album, say)
    goes out twice."""
    requests = plan(job, settings)
    for number in range(skip, len(requests)):
        if not send(settings, requests[number]):
            return number
    return None
