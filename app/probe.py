"""Inspect a URL with yt-dlp (no download): video heights, and playlist detection/expansion."""
from typing import Optional

import yt_dlp

from .config import Settings
from .downloader import trim_error


class ProbeError(Exception):
    """yt-dlp could not inspect the URL; str(exc) is a one-line user-facing message."""


NOT_A_PLAYLIST = "URL is not a playlist"


def _common_opts(settings: Settings) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if settings.cookies_file.is_file():
        opts["cookiefile"] = str(settings.cookies_file)
    return opts


def build_probe_opts(settings: Settings) -> dict:
    """Flat pass: a playlist comes back as a list of {url, title} stubs capped at the ceiling;
    a single video is still fully extracted (formats present)."""
    return {
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlist_items": f"1:{settings.max_playlist_items}",
        **_common_opts(settings),
    }


def build_video_opts(settings: Settings) -> dict:
    """Video pass: ignore any playlist in the URL and fully extract one video."""
    return {"noplaylist": True, "playlist_items": "1", **_common_opts(settings)}


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


def _heights(info: dict) -> list[int]:
    heights: set[int] = set()
    formats = info.get("formats")
    if formats:
        for f in formats:
            if f.get("vcodec") == "none":
                continue
            h = _positive_height(f.get("height"))
            if h is not None:
                heights.add(h)
    else:
        h = _positive_height(info.get("height"))
        if h is not None:
            heights.add(h)
    return sorted(heights, reverse=True)


def _is_http(url) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def playlist_entries(info: dict) -> list[dict]:
    """Usable flat entries of a playlist, in order: [{"url": str, "title": str | None}, ...]."""
    out = []
    for e in info.get("entries") or []:
        if isinstance(e, dict) and _is_http(e.get("url")):
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
    info = _extract(url, build_probe_opts(settings), ydl_class)
    if info.get("_type") != "playlist":
        return {"title": info.get("title"), "heights": _heights(info), "playlist": None}

    entries = playlist_entries(info)
    try:
        video = _first_video(_extract(url, build_video_opts(settings), ydl_class))
    except ProbeError:
        video = {}
    playlist = None
    if entries:
        playlist = {"title": info.get("title"), "count": len(entries), "total": _total(info, entries)}
    return {"title": video.get("title"), "heights": _heights(video), "playlist": playlist}


def expand_playlist(url: str, settings: Settings, ydl_class=yt_dlp.YoutubeDL) -> dict:
    """Blocking. Returns {"title", "total", "entries": [{"url", "title"}, ...]} or raises ProbeError."""
    info = _extract(url, build_probe_opts(settings), ydl_class)
    entries = playlist_entries(info) if info.get("_type") == "playlist" else []
    if not entries:
        raise ProbeError(NOT_A_PLAYLIST)
    return {"title": info.get("title"), "total": _total(info, entries), "entries": entries}
