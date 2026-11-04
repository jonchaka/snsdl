"""Inspect a URL with yt-dlp (no download): video heights, and playlist detection/expansion."""
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp

from .config import Settings
from .cookies import cookies_for
from .downloader import trim_error


class ProbeError(Exception):
    """yt-dlp could not inspect the URL; str(exc) is a one-line user-facing message."""


NOT_A_PLAYLIST = "URL is not a playlist"


def _common_opts(cookiefile: Optional[Path]) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)
    return opts


def build_probe_opts(settings: Settings, cookiefile: Optional[Path] = None) -> dict:
    """Flat pass: a playlist comes back as a list of {url, title} stubs capped at the ceiling;
    a single video is still fully extracted (formats present)."""
    return {
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlist_items": f"1:{settings.max_playlist_items}",
        **_common_opts(cookiefile),
    }


def build_video_opts(settings: Settings, cookiefile: Optional[Path] = None) -> dict:
    """Video pass: ignore any playlist in the URL and fully extract one video."""
    return {"noplaylist": True, "playlist_items": "1", **_common_opts(cookiefile)}


def _extract(url: str, opts: dict, ydl_class) -> dict:
    try:
        with ydl_class(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001 - surface any extractor failure as a message
        raise ProbeError(trim_error(exc)) from exc


def _positive_height(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _sizes(info: dict) -> list[dict]:
    """Distinct video heights, tallest first, each with the widest width seen for it (or None).
    The width lets the UI label portrait videos by their shorter side."""
    widths: dict[int, Optional[int]] = {}
    formats = info.get("formats")
    entries = [f for f in formats if f.get("vcodec") != "none"] if formats else [info]
    for f in entries:
        h = _positive_height(f.get("height"))
        if h is None:
            continue
        w = _positive_height(f.get("width"))
        if h not in widths or (w is not None and (widths[h] is None or w > widths[h])):
            widths[h] = w
    return [{"height": h, "width": widths[h]} for h in sorted(widths, reverse=True)]


def _heights(info: dict) -> list[int]:
    return [z["height"] for z in _sizes(info)]


def _is_http_url(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def playlist_entries(info: dict) -> list[dict]:
    """Stub entries of a playlist, in order: [{"url": str, "title": str | None}, ...].

    Only `_type` "url"/"url_transparent" stubs with a valid http(s) URL count; entries the
    extractor already resolved are full video dicts whose url is a CDN media URL."""
    out = []
    for e in info.get("entries") or []:
        if (isinstance(e, dict) and e.get("_type") in ("url", "url_transparent")
                and _is_http_url(e.get("url"))):
            out.append({"url": e["url"], "title": e.get("title")})
    return out


def _first_video(info: dict) -> dict:
    if info.get("_type") == "playlist":
        entries = [e for e in list(info.get("entries") or []) if e]
        return entries[0] if entries else {}
    return info


def _total(info: dict, entries: list) -> int:
    return max(info.get("playlist_count") or 0, len(entries))


def probe(url: str, settings: Settings, ydl_class=yt_dlp.YoutubeDL) -> dict:
    """Blocking. Returns {"title", "heights": [int, ...], "playlist": None | {"title", "count", "total"}}
    or raises ProbeError."""
    with cookies_for(settings, f"probe-{secrets.token_hex(4)}") as cookiefile:
        info = _extract(url, build_probe_opts(settings, cookiefile), ydl_class)
        if info.get("_type") != "playlist":
            return {"title": info.get("title"), "heights": _heights(info), "sizes": _sizes(info),
                    "playlist": None}

        entries = playlist_entries(info)
        try:
            video = _first_video(_extract(url, build_video_opts(settings, cookiefile), ydl_class))
        except ProbeError:
            video = {}
    playlist = None
    if entries:
        playlist = {"title": info.get("title"), "count": len(entries), "total": _total(info, entries)}
    return {"title": video.get("title"), "heights": _heights(video), "sizes": _sizes(video),
            "playlist": playlist}


def expand_playlist(url: str, settings: Settings, ydl_class=yt_dlp.YoutubeDL) -> dict:
    """Blocking. Returns {"title", "total", "entries": [{"url", "title"}, ...]} or raises ProbeError."""
    with cookies_for(settings, f"probe-{secrets.token_hex(4)}") as cookiefile:
        info = _extract(url, build_probe_opts(settings, cookiefile), ydl_class)
    entries = playlist_entries(info) if info.get("_type") == "playlist" else []
    if not entries:
        raise ProbeError(NOT_A_PLAYLIST)
    return {"title": info.get("title"), "total": _total(info, entries), "entries": entries}
