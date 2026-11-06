import pytest
import yt_dlp

from app import probe as probe_mod
from app.probe import ProbeError, build_probe_opts, build_video_opts, expand_playlist, playlist_entries, probe


class _YDL:
    """Each construction pops the next scripted result; records the opts of every construction."""

    script: list = []      # items are dicts (returned) or exceptions (raised)
    opts_seen: list = []

    def __init__(self, opts):
        _YDL.opts_seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        assert download is False
        item = _YDL.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


ORIGINAL_FETCH = probe_mod.fetch if hasattr(probe_mod, "fetch") else None
NEW_KEYS = {"duration": None, "audio_bytes": None, "thumb": None, "chapters": [], "live": False}


@pytest.fixture(autouse=True)
def fetch_calls(monkeypatch):
    """No probe test touches the network: thumbnail fetches are recorded and succeed."""
    calls = []

    def fake(url, dest_dir, key, settings, opener=None):
        calls.append({"url": url, "dest_dir": dest_dir, "key": key, "settings": settings})
        return f"{key}.jpg"

    monkeypatch.setattr(probe_mod, "fetch", fake)
    return calls


def _scripted(*results):
    _YDL.script = list(results)
    _YDL.opts_seen = []
    return _YDL


FORMATS = [
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "height": None},
    {"format_id": "137", "vcodec": "avc1", "height": 1080},
    {"format_id": "248", "vcodec": "vp9", "height": 1080},     # duplicate height
    {"format_id": "401", "vcodec": "av01", "height": 2160},
    {"format_id": "22", "vcodec": "avc1", "acodec": "mp4a", "height": 720},
    {"format_id": "sb0", "vcodec": "none", "height": 90},      # storyboard: vcodec none
    {"format_id": "odd", "vcodec": "avc1"},                    # no height key
]

FLAT_PLAYLIST = {
    "_type": "playlist", "title": "PL", "playlist_count": 4,
    "entries": [
        {"_type": "url", "url": "https://www.youtube.com/watch?v=a1", "id": "a1", "title": "One"},
        {"_type": "url", "url": "b2", "id": "b2", "title": "Bare id"},          # not http: dropped
        {"_type": "url", "url": "https://www.youtube.com/watch?v=c3", "id": "c3", "title": "Three"},
        {"_type": "url_transparent", "url": "https://www.youtube.com/watch?v=d4", "id": "d4", "title": "Four"},
        # already resolved by the extractor: url is a CDN media URL, not a page to download
        {"id": "r1", "title": "Resolved", "url": "https://cdn.example/v.mp4",
         "formats": [{"vcodec": "avc1", "height": 720}]},
        {"_type": "url", "url": "http://", "id": "nohost"},                      # no host: dropped
    ],
}


# -- single video ------------------------------------------------------------

def test_probe_returns_title_and_distinct_heights_desc(settings):
    result = probe("https://youtu.be/x", settings, ydl_class=_scripted({"title": "T", "formats": FORMATS}))
    assert result["title"] == "T" and result["heights"] == [2160, 1080, 720] and result["playlist"] is None
    assert [z["height"] for z in result["sizes"]] == [2160, 1080, 720]
    assert len(_YDL.opts_seen) == 1


def test_probe_single_format_extractor_uses_top_level_height(settings):
    info = {"title": "Clip", "height": 1080, "vcodec": "h264"}
    assert probe("https://x", settings, ydl_class=_scripted(info)) == {
        "title": "Clip", "heights": [1080], "sizes": [{"height": 1080, "width": None, "bytes": None}],
        "subtitles": [], "playlist": None, **NEW_KEYS}


def test_probe_audio_only_source_has_no_heights(settings):
    info = {"title": "Pod", "formats": [{"vcodec": "none", "acodec": "opus"}]}
    assert probe("https://x", settings, ydl_class=_scripted(info)) == {
        "title": "Pod", "heights": [], "sizes": [], "subtitles": [], "playlist": None, **NEW_KEYS}


def test_probe_wraps_yt_dlp_errors(settings):
    exc = yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Private video\nmore")
    with pytest.raises(ProbeError) as e:
        probe("https://x", settings, ydl_class=_scripted(exc))
    assert str(e.value) == "[youtube] abc: Private video"


# -- playlists ---------------------------------------------------------------

def test_probe_playlist_reports_usable_count_and_video_heights(settings):
    video = {"title": "One", "formats": [{"vcodec": "avc1", "height": 720}]}
    result = probe("https://www.youtube.com/playlist?list=PL", settings, ydl_class=_scripted(FLAT_PLAYLIST, video))
    assert result == {"title": "One", "heights": [720], "sizes": [{"height": 720, "width": None, "bytes": None}],
                      "subtitles": [], "playlist": {"title": "PL", "count": 3, "total": 4}, **NEW_KEYS}
    flat, single = _YDL.opts_seen
    assert flat["extract_flat"] == "in_playlist" and flat["noplaylist"] is False
    assert single["noplaylist"] is True and single["playlist_items"] == "1"


def test_probe_playlist_video_pass_may_return_a_playlist(settings):
    """A pure playlist URL with noplaylist still yields a one-entry playlist; use its first entry."""
    single = {"_type": "playlist", "entries": [{"title": "First", "formats": [{"vcodec": "avc1", "height": 480}]}]}
    result = probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, single))
    assert result["title"] == "First" and result["heights"] == [480]
    assert result["playlist"]["count"] == 3


def test_probe_playlist_video_pass_failure_gives_empty_heights(settings):
    exc = yt_dlp.utils.DownloadError("ERROR: boom")
    result = probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, exc))
    assert result == {"title": None, "heights": [], "sizes": [], "subtitles": [],
                      "playlist": {"title": "PL", "count": 3, "total": 4}, **NEW_KEYS}


def test_probe_playlist_total_defaults_to_entry_count(settings):
    info = {k: v for k, v in FLAT_PLAYLIST.items() if k != "playlist_count"}
    result = probe("https://x/p", settings, ydl_class=_scripted(info, {"title": "One", "formats": []}))
    assert result["playlist"] == {"title": "PL", "count": 3, "total": 3}


def test_probe_playlist_without_usable_entries_is_not_a_playlist(settings):
    info = {"_type": "playlist", "title": "List", "entries": [
        {"title": "First", "formats": [{"vcodec": "avc1", "height": 480}]},
        {"title": "Second", "formats": [{"vcodec": "avc1", "height": 2160}]},
    ]}
    result = probe("https://youtu.be/p", settings, ydl_class=_scripted(info, info))
    assert result == {"title": "First", "heights": [480], "sizes": [{"height": 480, "width": None, "bytes": None}],
                      "subtitles": [], "playlist": None, **NEW_KEYS}


# -- expand ------------------------------------------------------------------

def test_expand_playlist_returns_ordered_http_entries(settings):
    result = expand_playlist("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST))
    assert result == {"title": "PL", "total": 4, "entries": [
        {"url": "https://www.youtube.com/watch?v=a1", "title": "One"},
        {"url": "https://www.youtube.com/watch?v=c3", "title": "Three"},
        {"url": "https://www.youtube.com/watch?v=d4", "title": "Four"},
    ]}
    assert _YDL.opts_seen[0]["extract_flat"] == "in_playlist"


def test_expand_playlist_rejects_single_video(settings):
    with pytest.raises(ProbeError, match="^URL is not a playlist$"):
        expand_playlist("https://x/v", settings, ydl_class=_scripted({"title": "T", "formats": FORMATS}))


def test_expand_playlist_rejects_playlist_without_usable_entries(settings):
    info = {"_type": "playlist", "title": "L", "entries": [{"url": "bare"}]}
    with pytest.raises(ProbeError, match="^URL is not a playlist$"):
        expand_playlist("https://x/p", settings, ydl_class=_scripted(info))


def test_expand_playlist_wraps_yt_dlp_errors(settings):
    with pytest.raises(ProbeError, match="^nope$"):
        expand_playlist("https://x/p", settings, ydl_class=_scripted(RuntimeError("nope")))


def test_playlist_entries_filters_and_keeps_order():
    assert playlist_entries(FLAT_PLAYLIST) == [
        {"url": "https://www.youtube.com/watch?v=a1", "title": "One"},
        {"url": "https://www.youtube.com/watch?v=c3", "title": "Three"},
        {"url": "https://www.youtube.com/watch?v=d4", "title": "Four"},
    ]
    assert playlist_entries({"entries": [None, "junk", {"url": None}]}) == []
    assert playlist_entries({}) == []


# -- options -----------------------------------------------------------------

def test_build_probe_opts_is_flat_and_capped(settings):
    opts = build_probe_opts(settings)
    assert opts["extract_flat"] == "in_playlist"
    assert opts["noplaylist"] is False
    assert opts["playlist_items"] == "1:500"
    assert opts["quiet"] is True and opts["no_warnings"] is True and opts["skip_download"] is True
    assert "cookiefile" not in opts
    settings.max_playlist_items = 20
    assert build_probe_opts(settings)["playlist_items"] == "1:20"


def test_build_video_opts_is_single_video(settings):
    opts = build_video_opts(settings)
    assert opts["noplaylist"] is True
    assert opts["playlist_items"] == "1"
    assert "extract_flat" not in opts
    assert opts["quiet"] is True and opts["no_warnings"] is True and opts["skip_download"] is True
    assert "cookiefile" not in opts


def test_opts_include_cookiefile_only_when_passed(settings):
    settings.cookies_file.write_text("# cookies\n")
    assert "cookiefile" not in build_probe_opts(settings)
    assert "cookiefile" not in build_video_opts(settings)
    copy = settings.tmp_dir / "cookies-probe-x.txt"
    assert build_probe_opts(settings, cookiefile=copy)["cookiefile"] == str(copy)
    assert build_video_opts(settings, cookiefile=copy)["cookiefile"] == str(copy)


def test_probe_uses_a_temporary_cookies_copy_and_removes_it(settings):
    settings.cookies_file.write_text("# cookies\nline\n")
    seen = []

    class YDL(_YDL):
        def __init__(self, opts):
            super().__init__(opts)
            seen.append((opts["cookiefile"], open(opts["cookiefile"]).read()))

        def extract_info(self, url, download=True):
            return _YDL.script.pop(0)

    _scripted(FLAT_PLAYLIST, {"title": "One", "formats": []})
    probe("https://x/p", settings, ydl_class=YDL)
    assert len(seen) == 2
    assert all(p.startswith(str(settings.tmp_dir / "cookies-probe-")) for p, _ in seen)
    assert all(c == "# Netscape HTTP Cookie File\n# cookies\nline\n" for _, c in seen)
    assert list(settings.tmp_dir.glob("cookies-*")) == []

    _scripted(FLAT_PLAYLIST)
    expand_playlist("https://x/p", settings, ydl_class=YDL)
    assert list(settings.tmp_dir.glob("cookies-*")) == []


def test_probe_error_still_removes_cookies_copy(settings):
    settings.cookies_file.write_text("# cookies\n")
    seen = []

    class YDL(_YDL):
        def __init__(self, opts):
            super().__init__(opts)
            seen.append(opts["cookiefile"])

        def extract_info(self, url, download=True):
            raise RuntimeError("boom")

    _scripted()
    with pytest.raises(ProbeError):
        probe("https://x", settings, ydl_class=YDL)
    assert len(seen) == 1 and seen[0].startswith(str(settings.tmp_dir / "cookies-probe-"))
    assert list(settings.tmp_dir.glob("cookies-*")) == []


def test_sizes_ignore_audio_only_formats_even_at_a_video_height(settings):
    formats = [
        {"vcodec": "avc1", "height": 720, "width": 1280},
        {"vcodec": "none", "height": 720, "width": 9999},   # audio-only entry must not widen 720
    ]
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))
    assert result["sizes"] == [{"height": 720, "width": 1280, "bytes": None}]


def test_probe_reports_sizes_with_max_width_per_height(settings):
    formats = [
        {"vcodec": "avc1", "height": 1920, "width": 1080},
        {"vcodec": "vp9", "height": 1920, "width": 1072},
        {"vcodec": "avc1", "height": 720, "width": None},
        {"vcodec": "avc1", "height": 480},
        {"vcodec": "none", "height": 90, "width": 160},
    ]
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))
    assert result["heights"] == [1920, 720, 480]
    assert result["sizes"] == [
        {"height": 1920, "width": 1080, "bytes": None},
        {"height": 720, "width": None, "bytes": None},
        {"height": 480, "width": None, "bytes": None},
    ]


def test_default_ydl_is_real_yt_dlp():
    assert probe.__defaults__[0] is yt_dlp.YoutubeDL
    assert expand_playlist.__defaults__[0] is yt_dlp.YoutubeDL


# -- subtitles -----------------------------------------------------------------

def test_probe_lists_manual_tracks_then_own_language_auto_captions(settings):
    info = {"title": "T", "language": "en", "formats": [{"vcodec": "avc1", "height": 720}],
            "subtitles": {"en": [{"ext": "vtt", "name": "English"}], "ja": [{"ext": "vtt", "name": "Japanese"}]},
            "automatic_captions": {"en-orig": [{"ext": "vtt", "name": "English (Original)"}],
                                   "en": [{"ext": "vtt", "name": "English"}],
                                   "de": [{"ext": "vtt", "name": "German"}]}}
    result = probe("https://x", settings, ydl_class=_scripted(info))
    assert result["subtitles"] == [
        {"lang": "en", "name": "English", "auto": False},
        {"lang": "ja", "name": "Japanese", "auto": False},
        {"lang": "en-orig", "name": "English (Original)", "auto": True},
    ]


def test_probe_auto_captions_fall_back_to_the_video_language_without_orig_track(settings):
    info = {"title": "T", "language": "fr", "formats": [],
            "automatic_captions": {"fr": [{"ext": "vtt"}], "en": [{"ext": "vtt", "name": "English"}]}}
    assert probe("https://x", settings, ydl_class=_scripted(info))["subtitles"] == [
        {"lang": "fr", "name": "fr", "auto": True}]


def test_probe_auto_captions_without_language_hint_or_orig_are_dropped(settings):
    info = {"title": "T", "formats": [], "automatic_captions": {"en": [{"ext": "vtt"}], "de": [{"ext": "vtt"}]}}
    assert probe("https://x", settings, ydl_class=_scripted(info))["subtitles"] == []


def test_probe_tolerates_odd_subtitle_shapes(settings):
    info = {"title": "T", "formats": [], "subtitles": "nope", "automatic_captions": None}
    assert probe("https://x", settings, ydl_class=_scripted(info))["subtitles"] == []
    info = {"title": "T", "formats": [], "language": ["en"],
            "subtitles": {"en": 3, 7: [{"ext": "vtt"}], "fr": "vtt"}, "automatic_captions": {"en": [{"ext": "vtt"}]}}
    assert probe("https://x", settings, ydl_class=_scripted(info))["subtitles"] == []


def test_probe_skips_live_chat_json_only_and_unembeddable_tracks(settings):
    info = {"title": "T", "formats": [], "subtitles": {
        "live_chat": [{"ext": "json", "name": "Live chat"}],
        "en": [{"ext": "json3"}, {"ext": "vtt", "name": "English"}],
        "de": [{"ext": "ttml", "name": "German"}],
        "all": [{"ext": "vtt"}], "-en": [{"ext": "vtt"}], "x" * 21: [{"ext": "vtt"}], "en us": [{"ext": "vtt"}],
        "fr\n": [{"ext": "vtt"}]}}
    assert probe("https://x", settings, ydl_class=_scripted(info))["subtitles"] == [
        {"lang": "en", "name": "English", "auto": False}]


def test_probe_playlist_reports_first_video_subtitles(settings):
    video = {"title": "One", "formats": [], "subtitles": {"en": [{"ext": "vtt", "name": "English"}]}}
    result = probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, video))
    assert result["subtitles"] == [{"lang": "en", "name": "English", "auto": False}]


# -- impersonation -----------------------------------------------------------------

def test_probe_opts_impersonate_like_downloads(settings, monkeypatch):
    from app import downloader
    from yt_dlp.networking.impersonate import ImpersonateTarget
    monkeypatch.setattr(downloader, "target_supported", lambda t: t is not None)
    assert build_probe_opts(settings)["impersonate"] == ImpersonateTarget.from_str("chrome")
    assert build_video_opts(settings)["impersonate"] == ImpersonateTarget.from_str("chrome")
    settings.impersonate = None
    assert "impersonate" not in build_probe_opts(settings) and "impersonate" not in build_video_opts(settings)


# -- login wall -------------------------------------------------------------------

def test_probe_error_carries_needs_login(settings):
    walled = yt_dlp.utils.DownloadError("ERROR: [instagram] abc: You need to log in to access this content")
    with pytest.raises(ProbeError) as e:
        probe("https://www.instagram.com/p/abc/", settings, ydl_class=_scripted(walled))
    assert e.value.needs_login is True
    other = yt_dlp.utils.DownloadError("ERROR: Unsupported URL")
    with pytest.raises(ProbeError) as e:
        probe("https://x", settings, ydl_class=_scripted(other))
    assert e.value.needs_login is False


# -- round 6: duration, size estimates, proxy, thumbnails -------------------------------

@pytest.mark.parametrize("info, expected", [
    ({"duration": 754}, 754),
    ({"duration": 12.2}, 13),          # rounded up so a clip can reach the very end
    ({"duration": 754, "is_live": True}, None),
    ({}, None),
    ({"duration": None}, None),
    ({"duration": 0}, None),
    ({"duration": -3}, None),
    ({"duration": "754"}, None),
    ({"duration": True}, None),
])
def test_probe_duration(settings, info, expected):
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", **info}))
    assert result["duration"] == expected


def test_probe_playlist_duration_comes_from_the_first_video(settings):
    video = {"title": "One", "duration": 61, "formats": []}
    assert probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, video))["duration"] == 61


SIZED_FORMATS = [
    {"format_id": "139", "vcodec": "none", "acodec": "mp4a", "filesize": 1_000},
    {"format_id": "251", "vcodec": "none", "acodec": "opus", "filesize_approx": 3_000},   # best audio (last)
    {"format_id": "sb0", "vcodec": "none", "acodec": "none", "height": 90, "filesize": 9},  # storyboard
    {"format_id": "137", "vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 50_000},
    {"format_id": "248", "vcodec": "vp9", "acodec": "none", "height": 1080, "filesize_approx": 40_000},
    {"format_id": "22", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "filesize": 20_000},     # muxed
    {"format_id": "136", "vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 15_000},    # 18 000 with audio
    {"format_id": "135", "vcodec": "avc1", "acodec": "none", "height": 480},                         # unknown
]


def test_probe_size_estimates_per_height(settings):
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": SIZED_FORMATS}))
    assert result["audio_bytes"] == 3_000
    assert [(z["height"], z["bytes"]) for z in result["sizes"]] == [(1080, 53_000), (720, 20_000), (480, None)]


def test_probe_size_estimates_without_audio_only_formats(settings):
    formats = [f for f in SIZED_FORMATS if f["vcodec"] != "none"]
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))
    assert result["audio_bytes"] is None
    # A video-only format whose audio size is unknown has no reliable total.
    assert [(z["height"], z["bytes"]) for z in result["sizes"]] == [(1080, None), (720, 20_000), (480, None)]


def test_probe_audio_bytes_ignores_garbage_sizes(settings):
    formats = [{"vcodec": "none", "acodec": "opus", "filesize": "big"},
               {"vcodec": "none", "acodec": "opus", "filesize": True}]
    assert probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))["audio_bytes"] is None


def test_probe_single_format_extractor_size_from_top_level(settings):
    info = {"title": "Clip", "height": 720, "vcodec": "h264", "acodec": "aac", "filesize_approx": 4_321.5}
    result = probe("https://x", settings, ydl_class=_scripted(info))
    assert result["sizes"] == [{"height": 720, "width": None, "bytes": 4_321}]
    assert result["audio_bytes"] is None


def test_probe_opts_include_proxy_only_when_set(settings):
    assert "proxy" not in build_probe_opts(settings) and "proxy" not in build_video_opts(settings)
    settings.proxy = "http://u:p@proxy.local:3128"
    assert build_probe_opts(settings)["proxy"] == "http://u:p@proxy.local:3128"
    assert build_video_opts(settings)["proxy"] == "http://u:p@proxy.local:3128"


def test_probe_fetches_the_video_thumbnail(settings, fetch_calls):
    info = {"title": "T", "thumbnail": "https://i.example/hq.jpg"}
    result = probe("https://x", settings, ydl_class=_scripted(info))
    assert len(fetch_calls) == 1
    call = fetch_calls[0]
    assert call["url"] == "https://i.example/hq.jpg" and call["dest_dir"] == settings.thumbs_dir
    assert call["settings"] is settings
    assert result["thumb"] == call["key"] and probe_mod.thumbs.KEY_PATTERN.fullmatch(result["thumb"])


def test_probe_falls_back_to_the_thumbnails_list(settings, fetch_calls):
    info = {"title": "T", "thumbnails": [{"id": "0"}, {"url": "file:///x"}, {"url": "https://i.example/1.jpg"}]}
    probe("https://x", settings, ydl_class=_scripted(info))
    assert [c["url"] for c in fetch_calls] == ["https://i.example/1.jpg"]


@pytest.mark.parametrize("info", [
    {"title": "T"},
    {"title": "T", "thumbnail": "file:///etc/passwd"},
    {"title": "T", "thumbnail": 5, "thumbnails": "junk"},
    {"title": "T", "thumbnails": [None, "x", {"url": "ftp://h/t.jpg"}]},
])
def test_probe_without_an_http_thumbnail_does_not_fetch(settings, fetch_calls, info):
    assert probe("https://x", settings, ydl_class=_scripted(info))["thumb"] is None
    assert fetch_calls == []


def test_probe_thumb_is_none_when_the_fetch_fails(settings, monkeypatch):
    monkeypatch.setattr(probe_mod, "fetch", lambda *a, **kw: None)
    info = {"title": "T", "thumbnail": "https://i.example/hq.jpg"}
    assert probe("https://x", settings, ydl_class=_scripted(info))["thumb"] is None


def test_probe_thumb_is_none_when_the_fetch_raises(settings, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(probe_mod, "fetch", boom)
    info = {"title": "T", "thumbnail": "https://i.example/hq.jpg"}
    assert probe("https://x", settings, ydl_class=_scripted(info))["thumb"] is None


def test_probe_playlist_uses_the_playlist_thumbnail_only(settings, fetch_calls):
    flat = {**FLAT_PLAYLIST, "thumbnails": [{"url": "https://i.example/pl.jpg"}],
            "entries": [{**e, "thumbnail": "https://i.example/entry.jpg"} for e in FLAT_PLAYLIST["entries"]]}
    video = {"title": "One", "formats": [], "thumbnail": "https://i.example/video.jpg"}
    result = probe("https://x/p", settings, ydl_class=_scripted(flat, video))
    assert [c["url"] for c in fetch_calls] == ["https://i.example/pl.jpg"]
    assert result["thumb"] == fetch_calls[0]["key"]


def test_probe_playlist_without_its_own_thumbnail_has_none(settings, fetch_calls):
    flat = {**FLAT_PLAYLIST,
            "entries": [{**e, "thumbnail": "https://i.example/e.jpg"} for e in FLAT_PLAYLIST["entries"]]}
    video = {"title": "One", "formats": [], "thumbnail": "https://i.example/video.jpg"}
    assert probe("https://x/p", settings, ydl_class=_scripted(flat, video))["thumb"] is None
    assert fetch_calls == []


def test_probe_fetch_is_the_thumbs_module_function():
    from app import thumbs
    assert ORIGINAL_FETCH is thumbs.fetch_thumbnail   # captured before the autouse patch


# -- review fixes: unknown acodec, portrait sizes ------------------------------------------

def test_probe_size_adds_audio_when_the_video_acodec_is_unknown(settings):
    formats = [
        {"vcodec": "none", "acodec": "opus", "filesize": 3_000},
        {"vcodec": "avc1", "height": 720, "filesize": 15_000},                   # acodec missing
        {"vcodec": "avc1", "acodec": None, "height": 480, "filesize": 9_000},    # acodec None
    ]
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))
    assert [(z["height"], z["bytes"]) for z in result["sizes"]] == [(720, 18_000), (480, 12_000)]


def test_probe_size_with_unknown_acodec_and_no_audio_format_keeps_the_video_size(settings):
    formats = [{"vcodec": "avc1", "height": 720, "filesize": 15_000}]
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))
    assert result["sizes"] == [{"height": 720, "width": None, "bytes": 15_000}]


def test_probe_portrait_video_size_estimate(settings):
    formats = [
        {"vcodec": "none", "acodec": "mp4a", "filesize": 2_000},
        {"vcodec": "avc1", "acodec": "none", "height": 1920, "width": 1080, "filesize": 40_000},
        {"vcodec": "avc1", "acodec": "none", "height": 1280, "width": 720, "filesize_approx": 20_000},
    ]
    result = probe("https://x", settings, ydl_class=_scripted({"title": "T", "formats": formats}))
    assert result["sizes"] == [{"height": 1920, "width": 1080, "bytes": 42_000},
                               {"height": 1280, "width": 720, "bytes": 22_000}]


# -- round 7: chapters ------------------------------------------------------------------

def test_probe_reports_chapters_as_floats(settings):
    info = {"title": "T", "chapters": [
        {"title": "Intro", "start_time": 0, "end_time": 12.5},
        {"title": "Main", "start_time": 12.5, "end_time": 300},
    ]}
    assert probe("https://x", settings, ydl_class=_scripted(info))["chapters"] == [
        {"title": "Intro", "start": 0.0, "end": 12.5},
        {"title": "Main", "start": 12.5, "end": 300.0},
    ]
    result = probe("https://x", settings, ydl_class=_scripted(info))
    assert all(isinstance(c["start"], float) and isinstance(c["end"], float) for c in result["chapters"])


@pytest.mark.parametrize("chapters", [None, [], "chapters", {"a": 1}, 7])
def test_probe_chapters_default_to_empty(settings, chapters):
    info = {"title": "T"} if chapters is None else {"title": "T", "chapters": chapters}
    assert probe("https://x", settings, ydl_class=_scripted(info))["chapters"] == []


def test_probe_chapters_drop_bad_entries_and_tidy_titles(settings):
    info = {"title": "T", "chapters": [
        "not a dict",
        {"title": "No end", "start_time": 5},
        {"title": "Text times", "start_time": "0", "end_time": "10"},
        {"title": "Bool", "start_time": True, "end_time": 10},
        {"title": "Inverted", "start_time": 20, "end_time": 10},
        {"title": "Empty", "start_time": 10, "end_time": 10},
        {"title": "Negative", "start_time": -1, "end_time": 10},
        {"title": "Infinite", "start_time": 0, "end_time": float("inf")},
        {"title": "NaN", "start_time": float("nan"), "end_time": 10},
        {"title": "  Spaced\n out  ", "start_time": 0, "end_time": 10},
        {"start_time": 10, "end_time": 20},
        {"title": 42, "start_time": 20, "end_time": 30},
        {"title": "x" * 500, "start_time": 30, "end_time": 40},
    ]}
    chapters = probe("https://x", settings, ydl_class=_scripted(info))["chapters"]
    assert chapters == [
        {"title": "Spaced out", "start": 0.0, "end": 10.0},
        {"title": "Chapter 2", "start": 10.0, "end": 20.0},
        {"title": "Chapter 3", "start": 20.0, "end": 30.0},
        {"title": "x" * probe_mod.CHAPTER_TITLE_CHARS, "start": 30.0, "end": 40.0},
    ]


def test_probe_chapters_are_capped(settings):
    info = {"title": "T", "chapters": [{"title": f"C{i}", "start_time": i, "end_time": i + 1} for i in range(250)]}
    chapters = probe("https://x", settings, ydl_class=_scripted(info))["chapters"]
    assert len(chapters) == probe_mod.MAX_CHAPTERS == 200
    assert chapters[-1]["title"] == "C199"


def test_probe_playlist_chapters_come_from_the_first_video(settings):
    video = {"title": "One", "formats": [], "chapters": [{"title": "A", "start_time": 0, "end_time": 5}]}
    result = probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, video))
    assert result["chapters"] == [{"title": "A", "start": 0.0, "end": 5.0}]


# -- search ----------------------------------------------------------------------------

SEARCH = {"_type": "playlist", "entries": [
    {"_type": "url", "id": "aaaaaaaaaaa", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
     "title": "First", "duration": 61.2, "uploader": "Up", "channel": "Chan",
     "thumbnails": [{"url": "https://i.ytimg.com/small.jpg"}, {"url": "https://i.ytimg.com/big.jpg"}]},
    {"_type": "url", "id": "bbbbbbbbbbb", "title": "No url", "channel": "OnlyChannel",
     "thumbnail": "https://i.ytimg.com/b.jpg"},
    {"_type": "url", "id": "ccccccccccc", "url": None},
    {"_type": "url", "title": "no id, no url"},
    {"_type": "url", "id": "ddddddddddd", "thumbnails": [{"url": "file:///etc/passwd"}],
     "thumbnail": "javascript:alert(1)"},
    "junk",
]}


def test_search_options_and_parsing(settings, fetch_calls):
    ydl = _scripted(dict(SEARCH))
    results = probe_mod.search("lofi beats", settings, ydl_class=ydl)
    opts = _YDL.opts_seen[0]
    assert opts["extract_flat"] is True and opts["skip_download"] is True and opts["quiet"] is True
    assert "cookiefile" not in opts
    assert [r["url"] for r in results] == [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        "https://www.youtube.com/watch?v=ccccccccccc", "https://www.youtube.com/watch?v=ddddddddddd"]
    first, second, third, fourth = results
    assert first["title"] == "First" and first["duration"] == 62 and first["uploader"] == "Up"
    assert second["uploader"] == "OnlyChannel" and second["duration"] is None
    assert third == {"url": "https://www.youtube.com/watch?v=ccccccccccc", "title": None, "duration": None,
                     "uploader": None, "thumb": None, "live": False}
    assert fourth["thumb"] is None   # no http(s) thumbnail: nothing fetched
    assert sorted(c["url"] for c in fetch_calls) == ["https://i.ytimg.com/b.jpg", "https://i.ytimg.com/big.jpg"]
    assert first["thumb"] and second["thumb"] and first["thumb"] != second["thumb"]
    assert all(c["dest_dir"] == settings.thumbs_dir for c in fetch_calls)


def test_search_query_string(settings, monkeypatch):
    urls = []

    class Spy(_YDL):
        def extract_info(self, url, download=True):
            urls.append(url)
            return {"entries": []}

    assert probe_mod.search("cats", settings, ydl_class=Spy) == []
    assert urls == [f"ytsearch{probe_mod.SEARCH_RESULTS}:cats"]
    assert probe_mod.SEARCH_RESULTS == 8


def test_search_thumbnail_failure_or_timeout_is_none(settings, monkeypatch):
    import threading
    release = threading.Event()

    def fetch(url, dest_dir, key, settings, opener=None):
        if "slow" in url:
            release.wait(5)
            return f"{key}.jpg"
        if "boom" in url:
            raise OSError("network")
        return None

    monkeypatch.setattr(probe_mod, "fetch", fetch)
    monkeypatch.setattr(probe_mod, "SEARCH_THUMB_TIMEOUT", 0.2)
    info = {"entries": [{"id": f"id{n}", "thumbnail": f"https://i.ytimg.com/{n}.jpg"}
                        for n in ("slow", "boom", "none")]}
    try:
        results = probe_mod.search("x", settings, ydl_class=_scripted(info))
    finally:
        release.set()
    assert [r["thumb"] for r in results] == [None, None, None]


def test_search_error_is_probe_error(settings):
    with pytest.raises(ProbeError, match="network down"):
        probe_mod.search("x", settings, ydl_class=_scripted(yt_dlp.utils.DownloadError("ERROR: network down")))


def test_search_keeps_avif_thumbnails(settings, monkeypatch):
    # YouTube serves search-result thumbnails as AVIF: the real fetch must store them.
    from app import thumbs
    avif = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 60

    class _Resp:
        def __init__(self):
            self._data = avif

        def read(self, n=-1):
            chunk, self._data = self._data[:n], self._data[n:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, url, timeout=None):
            return _Resp()

    def fetch(url, dest_dir, key, settings, opener=None):
        return thumbs.fetch_thumbnail(url, dest_dir, key, settings, opener=_Opener())

    monkeypatch.setattr(probe_mod, "fetch", fetch)
    info = {"entries": [{"id": "aaaaaaaaaaa", "thumbnails": [{"url": "https://i.ytimg.com/vi/a/hq720.avif"}]}]}
    [result] = probe_mod.search("x", settings, ydl_class=_scripted(info))
    assert result["thumb"]
    assert thumbs.find(settings.thumbs_dir, result["thumb"]).suffix == ".avif"


# -- live --------------------------------------------------------------------

def test_probe_reports_live(settings):
    assert probe("https://x", settings, ydl_class=_scripted({"title": "L", "is_live": True}))["live"] is True
    assert probe("https://x", settings, ydl_class=_scripted({"title": "V", "is_live": False}))["live"] is False
    assert probe("https://x", settings, ydl_class=_scripted({"title": "V"}))["live"] is False


def test_probe_upcoming_stream_is_an_error(settings):
    with pytest.raises(ProbeError, match="^This stream has not started yet; check again when it is live.$"):
        probe("https://x", settings, ydl_class=_scripted({"title": "Soon", "live_status": "is_upcoming"}))


def test_probe_upcoming_status_is_ignored_when_live(settings):
    info = {"title": "L", "is_live": True, "live_status": "is_upcoming"}
    assert probe("https://x", settings, ydl_class=_scripted(info))["live"] is True


def test_search_entries_report_live(settings):
    info = {"entries": [{"id": "aaaaaaaaaaa", "live_status": "is_live"},
                        {"id": "bbbbbbbbbbb", "is_live": True},
                        {"id": "ccccccccccc", "live_status": "was_live"},
                        {"id": "ddddddddddd"}]}
    results = probe_mod.search("x", settings, ydl_class=_scripted(info))
    assert [r["live"] for r in results] == [True, True, False, False]
