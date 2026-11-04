"""Inspect a URL with yt-dlp (no download) to list available video heights."""
from typing import Optional

import yt_dlp

from .config import Settings
from .downloader import trim_error


class ProbeError(Exception):
    """yt-dlp could not inspect the URL; str(exc) is a one-line user-facing message."""


def build_probe_opts(settings: Settings) -> dict:
    opts = {
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if settings.cookies_file.is_file():
        opts["cookiefile"] = str(settings.cookies_file)
    return opts


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


def probe(url: str, settings: Settings, ydl_class=yt_dlp.YoutubeDL) -> dict:
    """Blocking. Returns {"title": str | None, "heights": [int, ...]} or raises ProbeError."""
    try:
        with ydl_class(build_probe_opts(settings)) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001 - surface any extractor failure as a message
        raise ProbeError(trim_error(exc)) from exc

    if info.get("_type") == "playlist":
        entries = [e for e in list(info.get("entries") or []) if e]
        info = entries[0] if entries else {}

    return {"title": info.get("title"), "heights": _heights(info)}
