"""Podcast feed (RSS 2.0 with iTunes tags) of a subscription's finished downloads.

The feed URL carries an unguessable key (HMAC of the server secret and the subscription id);
the routes that serve it and its files live in api.py."""
from __future__ import annotations

import hmac
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .jobs import Job

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
KEY_CHARS = 32
MIME = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "mp4": "video/mp4",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "flac": "audio/flac",
    "wav": "audio/wav",
}
DEFAULT_MIME = "application/octet-stream"
_XML_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")   # not allowed in XML 1.0

ET.register_namespace("itunes", ITUNES_NS)


def feed_key(secret: bytes, sub_id: str) -> str:
    """The feed's credential: 32 hex chars of HMAC-SHA256(secret, "feed:<sub_id>")."""
    return hmac.new(secret, f"feed:{sub_id}".encode(), "sha256").hexdigest()[:KEY_CHARS]


def feed_path(sub_id: str, key: str) -> str:
    return f"/feed/{sub_id}/{key}.xml"


def mime_for(name: str) -> str:
    """Media type of a feed file from its extension (the MIME table), else octet-stream."""
    _, dot, ext = (name or "").rpartition(".")
    return MIME.get(ext.lower(), DEFAULT_MIME) if dot else DEFAULT_MIME


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def rfc2822(iso: str) -> str:
    """An ISO 8601 time as RFC 2822 in GMT ("Fri, 06 Nov 2026 10:00:00 GMT"); naive = UTC."""
    return format_datetime(_parse(iso).astimezone(timezone.utc), usegmt=True)


def hms(seconds) -> str:
    """Seconds as H:MM:SS (itunes:duration)."""
    h, rest = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _text(parent: ET.Element, tag: str, value, attrib: Optional[dict] = None) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib or {})
    el.text = _XML_UNSAFE.sub("", str(value))
    return el


def _when(job: Job) -> str:
    return job.finished_at or job.created_at or ""


def _sort_key(job: Job) -> datetime:
    try:
        return _parse(_when(job))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def render(sub, items: list[tuple[Job, Path]], base: str, key: str) -> bytes:
    """The RSS document for `sub`: one item per (done job, its file), newest first. `base` is
    the absolute origin the URLs start with (no trailing slash)."""
    itunes = f"{{{ITUNES_NS}}}"
    title = sub.title or sub.url
    prefix = f"{base}/feed/{sub.id}/{key}"
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", title)
    _text(channel, "link", sub.url)
    _text(channel, "description", f"Downloads of {title} from SNS Downloader")
    _text(channel, f"{itunes}author", sub.title or title)
    for job, path in sorted(items, key=lambda it: _sort_key(it[0]), reverse=True):
        item = ET.SubElement(channel, "item")
        _text(item, "title", job.title or job.filename)
        _text(item, "description", job.description or "")
        _text(item, "guid", job.id, {"isPermaLink": "false"})
        try:
            _text(item, "pubDate", rfc2822(_when(job)))
        except (TypeError, ValueError):
            pass   # an unreadable time: the item goes without a date
        length = job.size
        if length is None:
            try:
                length = Path(path).stat().st_size
            except OSError:
                length = 0
        ET.SubElement(item, "enclosure", {"url": f"{prefix}/{quote(job.filename, safe='')}",
                                          "length": str(length), "type": mime_for(job.filename)})
        if job.duration:
            _text(item, f"{itunes}duration", hms(job.duration))
        if job.thumb:
            ET.SubElement(item, f"{itunes}image", {"href": f"{prefix}/thumbs/{quote(job.thumb, safe='')}"})
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode").encode("utf-8")
