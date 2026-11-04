import pytest
import yt_dlp

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
    "_type": "playlist", "title": "PL", "playlist_count": 3,
    "entries": [
        {"_type": "url", "url": "https://www.youtube.com/watch?v=a1", "id": "a1", "title": "One"},
        {"_type": "url", "url": "b2", "id": "b2", "title": "Bare id"},          # not http: dropped
        {"_type": "url", "url": "https://www.youtube.com/watch?v=c3", "id": "c3", "title": "Three"},
    ],
}


# -- single video ------------------------------------------------------------

def test_probe_returns_title_and_distinct_heights_desc(settings):
    result = probe("https://youtu.be/x", settings, ydl_class=_scripted({"title": "T", "formats": FORMATS}))
    assert result == {"title": "T", "heights": [2160, 1080, 720], "playlist": None}
    assert len(_YDL.opts_seen) == 1


def test_probe_single_format_extractor_uses_top_level_height(settings):
    info = {"title": "Clip", "height": 1080, "vcodec": "h264"}
    assert probe("https://x", settings, ydl_class=_scripted(info)) == {"title": "Clip", "heights": [1080], "playlist": None}


def test_probe_audio_only_source_has_no_heights(settings):
    info = {"title": "Pod", "formats": [{"vcodec": "none", "acodec": "opus"}]}
    assert probe("https://x", settings, ydl_class=_scripted(info)) == {"title": "Pod", "heights": [], "playlist": None}


def test_probe_wraps_yt_dlp_errors(settings):
    exc = yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Private video\nmore")
    with pytest.raises(ProbeError) as e:
        probe("https://x", settings, ydl_class=_scripted(exc))
    assert str(e.value) == "[youtube] abc: Private video"


# -- playlists ---------------------------------------------------------------

def test_probe_playlist_reports_usable_count_and_video_heights(settings):
    video = {"title": "One", "formats": [{"vcodec": "avc1", "height": 720}]}
    result = probe("https://www.youtube.com/playlist?list=PL", settings, ydl_class=_scripted(FLAT_PLAYLIST, video))
    assert result == {"title": "One", "heights": [720], "playlist": {"title": "PL", "count": 2, "total": 3}}
    flat, single = _YDL.opts_seen
    assert flat["extract_flat"] == "in_playlist" and flat["noplaylist"] is False
    assert single["noplaylist"] is True and single["playlist_items"] == "1"


def test_probe_playlist_video_pass_may_return_a_playlist(settings):
    """A pure playlist URL with noplaylist still yields a one-entry playlist; use its first entry."""
    single = {"_type": "playlist", "entries": [{"title": "First", "formats": [{"vcodec": "avc1", "height": 480}]}]}
    result = probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, single))
    assert result["title"] == "First" and result["heights"] == [480]
    assert result["playlist"]["count"] == 2


def test_probe_playlist_video_pass_failure_gives_empty_heights(settings):
    exc = yt_dlp.utils.DownloadError("ERROR: boom")
    result = probe("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST, exc))
    assert result == {"title": None, "heights": [], "playlist": {"title": "PL", "count": 2, "total": 3}}


def test_probe_playlist_total_defaults_to_entry_count(settings):
    info = {k: v for k, v in FLAT_PLAYLIST.items() if k != "playlist_count"}
    result = probe("https://x/p", settings, ydl_class=_scripted(info, {"title": "One", "formats": []}))
    assert result["playlist"] == {"title": "PL", "count": 2, "total": 2}


def test_probe_playlist_without_usable_entries_is_not_a_playlist(settings):
    info = {"_type": "playlist", "title": "List", "entries": [
        {"title": "First", "formats": [{"vcodec": "avc1", "height": 480}]},
        {"title": "Second", "formats": [{"vcodec": "avc1", "height": 2160}]},
    ]}
    result = probe("https://youtu.be/p", settings, ydl_class=_scripted(info, info))
    assert result == {"title": "First", "heights": [480], "playlist": None}


# -- expand ------------------------------------------------------------------

def test_expand_playlist_returns_ordered_http_entries(settings):
    result = expand_playlist("https://x/p", settings, ydl_class=_scripted(FLAT_PLAYLIST))
    assert result == {"title": "PL", "total": 3, "entries": [
        {"url": "https://www.youtube.com/watch?v=a1", "title": "One"},
        {"url": "https://www.youtube.com/watch?v=c3", "title": "Three"},
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


def test_opts_include_cookiefile_when_present(settings):
    settings.cookies_file.write_text("# cookies\n")
    assert build_probe_opts(settings)["cookiefile"] == str(settings.cookies_file)
    assert build_video_opts(settings)["cookiefile"] == str(settings.cookies_file)


def test_default_ydl_is_real_yt_dlp():
    assert probe.__defaults__[0] is yt_dlp.YoutubeDL
    assert expand_playlist.__defaults__[0] is yt_dlp.YoutubeDL
