"""Inspect a URL with yt-dlp (no download): video heights and size estimates, duration, subtitle
tracks, thumbnail, and playlist detection/expansion."""
import logging
import math
import re
import secrets
from pathlib import Path
from typing import Optional

import yt_dlp

from . import thumbs
from .config import Settings
from .cookies import cookies_for
from .downloader import impersonate_opts, looks_login_walled, trim_error

log = logging.getLogger(__name__)

# Indirection so tests (and the browser-test server) can swap the network fetch out.
fetch = thumbs.fetch_thumbnail


class ProbeError(Exception):
    """yt-dlp could not inspect the URL; str(exc) is a one-line user-facing message."""

    def __init__(self, message: str, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


NOT_A_PLAYLIST = "URL is not a playlist"


def _common_opts(settings: Settings, cookiefile: Optional[Path]) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, **impersonate_opts(settings)}
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)
    if settings.proxy:
        opts["proxy"] = settings.proxy
    return opts


def build_probe_opts(settings: Settings, cookiefile: Optional[Path] = None) -> dict:
    """Flat pass: a playlist comes back as a list of {url, title} stubs capped at the ceiling;
    a single video is still fully extracted (formats present)."""
    return {
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlist_items": f"1:{settings.max_playlist_items}",
        **_common_opts(settings, cookiefile),
    }


def build_video_opts(settings: Settings, cookiefile: Optional[Path] = None) -> dict:
    """Video pass: ignore any playlist in the URL and fully extract one video."""
    return {"noplaylist": True, "playlist_items": "1", **_common_opts(settings, cookiefile)}


def _extract(url: str, opts: dict, ydl_class) -> dict:
    try:
        with ydl_class(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001 - surface any extractor failure as a message
        raise ProbeError(trim_error(exc), needs_login=looks_login_walled(str(exc))) from exc


def _positive_height(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _format_bytes(f: dict) -> Optional[int]:
    """A format's exact or approximate size in bytes, or None."""
    for key in ("filesize", "filesize_approx"):
        value = _positive_height(f.get(key))
        if value is not None:
            return value
    return None


def _audio_bytes(info: dict) -> Optional[int]:
    """Size of the best audio-only format (yt-dlp lists formats worst to best), or None."""
    audio = [f for f in info.get("formats") or []
             if isinstance(f, dict) and f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    return _format_bytes(audio[-1]) if audio else None


def _sizes(info: dict) -> list[dict]:
    """Distinct video heights, tallest first, each with the widest width seen for it (or None)
    and an estimated download size in bytes (or None). The width lets the UI label portrait
    videos by their shorter side."""
    widths: dict[int, Optional[int]] = {}
    sizes: dict[int, Optional[int]] = {}
    formats = info.get("formats")
    entries = [f for f in formats if f.get("vcodec") != "none"] if formats else [info]
    audio = _audio_bytes(info)
    for f in entries:
        h = _positive_height(f.get("height"))
        if h is None:
            continue
        w = _positive_height(f.get("width"))
        if h not in widths or (w is not None and (widths[h] is None or w > widths[h])):
            widths[h] = w
        # Largest candidate at that height (the pick may differ by codec, so err high); a
        # video-only stream gets the best audio added, as yt-dlp merges it in. An unknown
        # acodec (None/missing) also gets it when known: over-estimating beats under-estimating.
        size = _format_bytes(f)
        acodec = f.get("acodec")
        if size is not None and acodec == "none":
            size = size + audio if audio is not None else None
        elif size is not None and acodec is None and audio is not None:
            size += audio
        if size is not None and (sizes.get(h) is None or size > sizes[h]):
            sizes[h] = size
    return [{"height": h, "width": widths[h], "bytes": sizes.get(h)} for h in sorted(widths, reverse=True)]


def _duration(info: dict) -> Optional[int]:
    """Whole seconds, rounded up so a clip end can reach the very end; None for live or unknown."""
    if info.get("is_live"):
        return None
    value = info.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0 or math.isinf(value):
        return None
    return math.ceil(value)


EMBEDDABLE_SUB_EXTS = {"vtt", "srt", "ass", "ssa"}   # what FFmpegEmbedSubtitle can mux into mp4
SUBTITLE_LANG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$")


def _usable_track(lang, formats) -> bool:
    """A real caption track: a sane language key and at least one text format ffmpeg can embed.
    Drops YouTube's `live_chat` (json only) and anything the API would refuse anyway."""
    if not isinstance(lang, str) or not SUBTITLE_LANG.fullmatch(lang) or lang.lower() == "all":
        return False
    if not isinstance(formats, list):
        return False
    return any(isinstance(f, dict) and f.get("ext") in EMBEDDABLE_SUB_EXTS for f in formats)


def _track_name(formats: list, lang: str) -> str:
    for f in formats:
        if isinstance(f, dict) and f.get("name"):
            return str(f["name"])
    return lang


def _subtitles(info: dict) -> list[dict]:
    """Subtitle tracks worth offering: every manual track, then the automatic captions in the
    video's own language only (YouTube lists ~150 machine translations otherwise).
    [{"lang": "en", "name": "English", "auto": False}, ...]"""
    out = []
    manual = info.get("subtitles")
    if isinstance(manual, dict):
        for lang, formats in manual.items():
            if _usable_track(lang, formats):
                out.append({"lang": lang, "name": _track_name(formats, lang), "auto": False})
    auto = info.get("automatic_captions")
    if isinstance(auto, dict) and auto:
        picks = [k for k in auto if isinstance(k, str) and k.endswith("-orig")]   # YouTube: untranslated ASR
        language = info.get("language")
        if not picks and isinstance(language, str) and language in auto:
            picks = [language]
        for lang in picks:
            if _usable_track(lang, auto[lang]):
                out.append({"lang": lang, "name": _track_name(auto[lang], lang), "auto": True})
    return out


def _heights(info: dict) -> list[int]:
    return [z["height"] for z in _sizes(info)]


def playlist_entries(info: dict) -> list[dict]:
    """Stub entries of a playlist, in order: [{"url": str, "title": str | None}, ...].

    Only `_type` "url"/"url_transparent" stubs with a valid http(s) URL count; entries the
    extractor already resolved are full video dicts whose url is a CDN media URL."""
    out = []
    for e in info.get("entries") or []:
        if (isinstance(e, dict) and e.get("_type") in ("url", "url_transparent")
                and thumbs.is_http_url(e.get("url"))):
            out.append({"url": e["url"], "title": e.get("title")})
    return out


def _thumbnail_url(info: dict) -> Optional[str]:
    """The info's own http(s) thumbnail: `thumbnail`, else the first usable `thumbnails[].url`."""
    if thumbs.is_http_url(info.get("thumbnail")):
        return info["thumbnail"]
    candidates = info.get("thumbnails")
    for t in candidates if isinstance(candidates, list) else []:
        if isinstance(t, dict) and thumbs.is_http_url(t.get("url")):
            return t["url"]
    return None


def _thumb(info: dict, settings: Settings) -> Optional[str]:
    """Fetch the thumbnail now and return its key, or None. Never raises."""
    url = _thumbnail_url(info)
    if url is None:
        return None
    key = thumbs.new_key()
    try:
        return key if fetch(url, settings.thumbs_dir, key=key, settings=settings) else None
    except Exception as exc:  # noqa: BLE001 - a thumbnail is decoration; the probe must succeed
        log.warning("probe thumbnail failed: %s", exc.__class__.__name__)
        return None


def _result(video: dict, playlist: Optional[dict], thumb: Optional[str]) -> dict:
    return {"title": video.get("title"), "heights": _heights(video), "sizes": _sizes(video),
            "subtitles": _subtitles(video), "playlist": playlist, "duration": _duration(video),
            "audio_bytes": _audio_bytes(video), "thumb": thumb}


def _first_video(info: dict) -> dict:
    if info.get("_type") == "playlist":
        entries = [e for e in list(info.get("entries") or []) if e]
        return entries[0] if entries else {}
    return info


def _total(info: dict, entries: list) -> int:
    return max(info.get("playlist_count") or 0, len(entries))


def probe(url: str, settings: Settings, ydl_class=yt_dlp.YoutubeDL) -> dict:
    """Blocking. Returns {"title", "heights": [int, ...], "sizes": [{"height", "width", "bytes"}],
    "subtitles", "playlist": None | {"title", "count", "total"}, "duration", "audio_bytes",
    "thumb"} or raises ProbeError."""
    with cookies_for(settings, f"probe-{secrets.token_hex(4)}") as cookiefile:
        info = _extract(url, build_probe_opts(settings, cookiefile), ydl_class)
        if info.get("_type") == "playlist":
            entries = playlist_entries(info)
            try:
                video = _first_video(_extract(url, build_video_opts(settings, cookiefile), ydl_class))
            except ProbeError:
                video = {}
    # Thumbnail fetched after the cookies copy is gone (it is not needed for it).
    if info.get("_type") != "playlist":
        return _result(info, None, _thumb(info, settings))
    playlist = None
    if entries:
        playlist = {"title": info.get("title"), "count": len(entries), "total": _total(info, entries)}
    # The playlist's own thumbnail only: one fetch per probe, never one per entry.
    return _result(video, playlist, _thumb(info, settings))


def expand_playlist(url: str, settings: Settings, ydl_class=yt_dlp.YoutubeDL) -> dict:
    """Blocking. Returns {"title", "total", "entries": [{"url", "title"}, ...]} or raises ProbeError."""
    with cookies_for(settings, f"probe-{secrets.token_hex(4)}") as cookiefile:
        info = _extract(url, build_probe_opts(settings, cookiefile), ydl_class)
    entries = playlist_entries(info) if info.get("_type") == "playlist" else []
    if not entries:
        raise ProbeError(NOT_A_PLAYLIST)
    return {"title": info.get("title"), "total": _total(info, entries), "entries": entries}
