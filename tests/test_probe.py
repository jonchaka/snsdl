import pytest
import yt_dlp

from app import probe as probe_mod
from app.probe import ProbeError, probe


class _YDL:
    last_opts: dict = {}

    def __init__(self, opts):
        _YDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ydl_returning(info):
    class YDL(_YDL):
        def extract_info(self, url, download=True):
            assert download is False
            return info
    return YDL


def _ydl_raising(exc):
    class YDL(_YDL):
        def extract_info(self, url, download=True):
            raise exc
    return YDL


FORMATS = [
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "height": None},
    {"format_id": "137", "vcodec": "avc1", "height": 1080},
    {"format_id": "248", "vcodec": "vp9", "height": 1080},     # duplicate height
    {"format_id": "401", "vcodec": "av01", "height": 2160},
    {"format_id": "22", "vcodec": "avc1", "acodec": "mp4a", "height": 720},
    {"format_id": "sb0", "vcodec": "none", "height": 90},      # storyboard: vcodec none
    {"format_id": "odd", "vcodec": "avc1"},                    # no height key
]


def test_probe_returns_title_and_distinct_heights_desc(settings):
    result = probe("https://youtu.be/x", settings, ydl_class=_ydl_returning({"title": "T", "formats": FORMATS}))
    assert result == {"title": "T", "heights": [2160, 1080, 720]}


def test_probe_uses_first_entry_of_playlist(settings):
    info = {"_type": "playlist", "title": "List", "entries": [
        {"title": "First", "formats": [{"vcodec": "avc1", "height": 480}]},
        {"title": "Second", "formats": [{"vcodec": "avc1", "height": 2160}]},
    ]}
    assert probe("https://youtu.be/p", settings, ydl_class=_ydl_returning(info)) == {"title": "First", "heights": [480]}


def test_probe_single_format_extractor_uses_top_level_height(settings):
    info = {"title": "Clip", "height": 1080, "vcodec": "h264"}
    assert probe("https://x", settings, ydl_class=_ydl_returning(info)) == {"title": "Clip", "heights": [1080]}


def test_probe_audio_only_source_has_no_heights(settings):
    info = {"title": "Pod", "formats": [{"vcodec": "none", "acodec": "opus"}]}
    assert probe("https://x", settings, ydl_class=_ydl_returning(info)) == {"title": "Pod", "heights": []}


def test_probe_wraps_yt_dlp_errors(settings):
    exc = yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Private video\nmore")
    with pytest.raises(ProbeError) as e:
        probe("https://x", settings, ydl_class=_ydl_raising(exc))
    assert str(e.value) == "[youtube] abc: Private video"


def test_probe_opts(settings):
    probe("https://x", settings, ydl_class=_ydl_returning({"title": "T", "formats": []}))
    opts = _YDL.last_opts
    assert opts["noplaylist"] is True
    assert opts["playlist_items"] == "1"
    assert opts["quiet"] is True
    assert opts["no_warnings"] is True
    assert opts["skip_download"] is True
    assert "cookiefile" not in opts

    settings.cookies_file.write_text("# cookies\n")
    probe("https://x", settings, ydl_class=_ydl_returning({"title": "T", "formats": []}))
    assert _YDL.last_opts["cookiefile"] == str(settings.cookies_file)


def test_probe_default_ydl_is_real_yt_dlp():
    assert probe.__defaults__[0] is yt_dlp.YoutubeDL
