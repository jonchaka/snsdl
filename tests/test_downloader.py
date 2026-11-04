from pathlib import Path

import yt_dlp

from app import downloader
from app.jobs import DONE, FAILED


class FakeYDL:
    """Simulates yt-dlp: fires progress hooks, writes an output file, returns info."""

    last_opts: dict = {}

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Video"}
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "info_dict": info})
        out = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
        out.write_bytes(b"x" * 10)
        hook({"status": "finished", "info_dict": info})
        return {**info, "requested_downloads": [{"filepath": str(out)}]}


class FailingYDL(FakeYDL):
    def extract_info(self, url, download=True):
        partial = Path(self.opts["outtmpl"].replace("%(title).80B", "x").replace("%(ext)s", "mp4.part"))
        partial.write_bytes(b"partial")
        raise yt_dlp.utils.DownloadError("ERROR: [tiktok] Unable to extract video\nsecond line")


def _hook(d):
    pass


class CancelMidwayYDL(FakeYDL):
    """Fires one progress hook, lets the test cancel the job, then fires again with a .part on disk."""

    on_first_hook = staticmethod(lambda: None)

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Video"}
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "info_dict": info})
        CancelMidwayYDL.on_first_hook()
        partial = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4.part"))
        partial.write_bytes(b"partial")
        try:
            hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 200, "info_dict": info})
        except downloader.DownloadCancelled:
            CancelMidwayYDL.raised = True
            raise
        raise AssertionError("hook should have raised DownloadCancelled")


class CancelInPostprocessorYDL(FakeYDL):
    """Download completes, then the postprocessor hook fires after the test cancelled the job."""

    on_finished = staticmethod(lambda: None)

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Video"}
        out = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
        out.write_bytes(b"x" * 10)
        hook({"status": "finished", "info_dict": info})
        CancelInPostprocessorYDL.on_finished()
        try:
            self.opts["postprocessor_hooks"][0]({"status": "started", "postprocessor": "MoveFiles", "info_dict": info})
        except downloader.DownloadCancelled:
            CancelInPostprocessorYDL.raised = True
            raise
        raise AssertionError("postprocessor hook should have raised DownloadCancelled")


class NeverCalledYDL(FakeYDL):
    called = False

    def extract_info(self, url, download=True):
        NeverCalledYDL.called = True
        raise RuntimeError("extract_info should not be called")


def test_build_opts_core_values(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook)
    assert opts["format"] == "bestvideo*+bestaudio/best"
    assert opts["merge_output_format"] == "mp4"
    assert opts["noplaylist"] is True
    assert opts["playlist_items"] == "1"
    assert opts["restrictfilenames"] is True
    assert opts["progress_hooks"] == [_hook]
    assert opts["outtmpl"] == str(settings.tmp_dir / "abc12345-%(title).80B.%(ext)s")
    assert "cookiefile" not in opts


def test_build_opts_sets_cookiefile_only_when_passed(settings):
    assert "cookiefile" not in downloader.build_opts(settings, "abc12345", _hook)
    settings.cookies_file.write_text("# cookies\n")
    assert "cookiefile" not in downloader.build_opts(settings, "abc12345", _hook)   # never reads settings directly
    copy = settings.tmp_dir / "cookies-abc12345.txt"
    assert downloader.build_opts(settings, "abc12345", _hook, cookiefile=copy)["cookiefile"] == str(copy)


def test_progress_percent():
    assert downloader.progress_percent({"status": "finished"}) == 99
    assert downloader.progress_percent({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200}) == 25
    assert downloader.progress_percent({"status": "downloading", "downloaded_bytes": 50, "total_bytes_estimate": 100}) == 50
    assert downloader.progress_percent({"status": "downloading", "downloaded_bytes": 50}) is None
    assert downloader.progress_percent({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 200}) == 99


def test_download_success_moves_file_and_marks_done(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)

    job = store.get(job.id)
    assert job.status == DONE
    assert job.progress == 100
    assert job.title == "My Video"
    assert job.filename == f"{job.id}-My_Video.mp4"
    assert (settings.downloads_dir / job.filename).read_bytes() == b"x" * 10
    assert list(settings.tmp_dir.iterdir()) == []


def test_download_failure_marks_failed_and_cleans_partials(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FailingYDL)

    job = store.get(job.id)
    assert job.status == FAILED
    assert job.error == "[tiktok] Unable to extract video"
    assert job.filename is None
    assert list(settings.tmp_dir.iterdir()) == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_download_error_is_truncated_to_200_chars(settings, store):
    class LongError(FakeYDL):
        def extract_info(self, url, download=True):
            raise RuntimeError("x" * 500)

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=LongError)
    assert len(store.get(job.id).error) == 200


def test_download_tolerates_job_cleared_mid_download(settings, store):
    class ClearingYDL(FakeYDL):
        def extract_info(self, url, download=True):
            hook = self.opts["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200,
                  "info_dict": {"title": "My Video"}})
            store.clear()
            out = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
            out.write_bytes(b"x" * 10)
            hook({"status": "finished", "info_dict": {"title": "My Video"}})
            return {"title": "My Video", "requested_downloads": [{"filepath": str(out)}]}

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ClearingYDL)

    assert store.list() == []
    assert list(settings.downloads_dir.iterdir()) == []
    assert list(settings.tmp_dir.iterdir()) == []


def test_hook_skips_redundant_store_writes(settings, store, monkeypatch):
    class RepeatYDL(FakeYDL):
        def extract_info(self, url, download=True):
            hook = self.opts["progress_hooks"][0]
            for _ in range(5):
                hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200,
                      "info_dict": {"title": "My Video"}})
            out = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
            out.write_bytes(b"x")
            return {"title": "My Video", "requested_downloads": [{"filepath": str(out)}]}

    job = store.add("https://example.com/v")
    calls = []
    real_update = store.update
    monkeypatch.setattr(store, "update", lambda *a, **kw: (calls.append(kw), real_update(*a, **kw))[1])
    downloader.download(job.id, job.url, store, settings, ydl_class=RepeatYDL)

    progress_calls = [c for c in calls if "progress" in c and "status" not in c]
    assert len(progress_calls) == 1


import pytest


def test_build_opts_mp4_height_uses_height_cap(settings):
    settings.embed_thumbnails = False
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp4", quality="1080")
    assert opts["format"] == "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best"
    assert opts["merge_output_format"] == "mp4"
    assert "postprocessors" not in opts


def test_build_opts_mp4_best_is_default(settings):
    explicit = downloader.build_opts(settings, "abc12345", _hook, format="mp4", quality="best")
    implicit = downloader.build_opts(settings, "abc12345", _hook)
    assert explicit == implicit
    assert explicit["format"] == "bestvideo*+bestaudio/best"


@pytest.mark.parametrize("quality,preferred", [("best", "0"), ("320", "320"), ("192", "192")])
def test_build_opts_mp3(settings, quality, preferred):
    settings.embed_thumbnails = False
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp3", quality=quality)
    assert opts["format"] == "bestaudio/best"
    assert "merge_output_format" not in opts
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": preferred},
        {"key": "FFmpegMetadata"},
    ]
    # common keys still present
    assert opts["noplaylist"] is True
    assert opts["playlist_items"] == "1"


@pytest.mark.parametrize("quality", ["best", "1080", "720"])
def test_build_opts_mp4_prefers_av1_after_resolution(settings, quality):
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp4", quality=quality)
    assert opts["format_sort"] == downloader.MP4_FORMAT_SORT
    assert downloader.MP4_FORMAT_SORT == ["res", "vcodec:av01"]


@pytest.mark.parametrize("quality", ["best", "320", "192"])
def test_build_opts_mp3_has_no_format_sort(settings, quality):
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp3", quality=quality)
    assert "format_sort" not in opts
    assert opts["outtmpl"] == str(settings.tmp_dir / "abc12345-%(title).80B.%(ext)s")


class Mp3YDL(FakeYDL):
    """Simulates yt-dlp with FFmpegExtractAudio: downloads .webm, converts to .mp3, deletes the .webm.
    requested_downloads.filepath points at the already-deleted intermediate, like a real post-processed run."""

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Song"}
        base = self.opts["outtmpl"].replace("%(title).80B", "My_Song")
        webm = Path(base.replace("%(ext)s", "webm"))
        mp3 = Path(base.replace("%(ext)s", "mp3"))
        webm.write_bytes(b"w" * 20)
        hook({"status": "finished", "info_dict": info})
        mp3.write_bytes(b"m" * 10)
        webm.unlink()
        return {**info, "requested_downloads": [{"filepath": str(webm)}]}


def test_download_mp3_job_uses_mp3_options_and_locates_mp3(settings, store):
    job = store.add("https://example.com/v", format="mp3", quality="320")
    downloader.download(job.id, job.url, store, settings, ydl_class=Mp3YDL)

    assert Mp3YDL.last_opts["format"] == "bestaudio/best"
    assert Mp3YDL.last_opts["postprocessors"][0]["preferredquality"] == "320"
    job = store.get(job.id)
    assert job.status == DONE
    assert job.filename == f"{job.id}-My_Song.mp3"
    assert (settings.downloads_dir / job.filename).read_bytes() == b"m" * 10
    assert list(settings.tmp_dir.iterdir()) == []


def test_download_mp4_job_passes_height(settings, store):
    job = store.add("https://example.com/v", format="mp4", quality="720")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.last_opts["format"] == "bestvideo*[height<=720]+bestaudio/best[height<=720]/best"
    assert store.get(job.id).status == DONE


def test_locate_output_prefers_requested_ext(settings):
    tmp = settings.tmp_dir
    (tmp / "j1-x.mp4").write_bytes(b"1234")
    (tmp / "j1-x.mp3").write_bytes(b"1")
    assert downloader._locate_output(None, tmp, "j1", ext=".mp3").name == "j1-x.mp3"
    assert downloader._locate_output(None, tmp, "j1", ext=".mp4").name == "j1-x.mp4"
    assert downloader._locate_output(None, tmp, "j1").name == "j1-x.mp4"


# -- cancellation -----------------------------------------------------------

def _spy_updates(store):
    """Wrap store.update so the test can see which statuses the downloader tried to write."""
    statuses = []
    real = store.update

    def spy(job_id, **fields):
        if "status" in fields:
            statuses.append(fields["status"])
        return real(job_id, **fields)

    store.update = spy
    return statuses


def test_cancel_midway_stops_download_and_leaves_nothing(settings, store):
    job = store.add("https://example.com/v")
    statuses = _spy_updates(store)
    CancelMidwayYDL.on_first_hook = staticmethod(lambda: store.remove(job.id))
    CancelMidwayYDL.raised = False
    downloader.download(job.id, job.url, store, settings, ydl_class=CancelMidwayYDL)
    assert CancelMidwayYDL.raised
    assert FAILED not in statuses
    assert store.get(job.id) is None
    assert list(settings.tmp_dir.iterdir()) == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_cancel_during_postprocessing_discards_output(settings, store):
    job = store.add("https://example.com/v")
    statuses = _spy_updates(store)
    CancelInPostprocessorYDL.on_finished = staticmethod(lambda: store.remove(job.id))
    CancelInPostprocessorYDL.raised = False
    downloader.download(job.id, job.url, store, settings, ydl_class=CancelInPostprocessorYDL)
    assert CancelInPostprocessorYDL.raised
    assert FAILED not in statuses
    assert store.get(job.id) is None
    assert list(settings.tmp_dir.iterdir()) == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_download_of_missing_job_returns_without_calling_ytdlp(settings, store):
    NeverCalledYDL.called = False
    downloader.download("nope0000", "https://example.com/v", store, settings, ydl_class=NeverCalledYDL)
    assert NeverCalledYDL.called is False
    assert store.list() == []


def test_hook_raises_download_cancelled_when_job_is_gone(settings, store):
    job = store.add("https://example.com/v")
    captured = {}

    class CaptureYDL(FakeYDL):
        def extract_info(self, url, download=True):
            captured["hook"] = self.opts["progress_hooks"][0]
            raise RuntimeError("stop here")

    downloader.download(job.id, job.url, store, settings, ydl_class=CaptureYDL)
    store.remove(job.id)
    with pytest.raises(downloader.DownloadCancelled):
        captured["hook"]({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})


@pytest.mark.parametrize("fmt,quality", [("mp4", "best"), ("mp4", "720"), ("mp3", "320")])
def test_build_opts_registers_postprocessor_hook(settings, fmt, quality):
    opts = downloader.build_opts(settings, "abc12345", _hook, format=fmt, quality=quality)
    assert opts["postprocessor_hooks"] == [_hook]
    assert opts["progress_hooks"] == [_hook]


# -- size / cookies copy ---------------------------------------------------------

def test_done_update_records_file_size(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert store.get(job.id).status == DONE
    assert store.get(job.id).size == 10


class CookieSpyYDL(FakeYDL):
    """Records the cookiefile path and its content at construction time."""
    seen = None

    def __init__(self, opts):
        super().__init__(opts)
        path = opts.get("cookiefile")
        type(self).seen = (path, Path(path).read_text() if path else None)


def test_download_uses_a_per_run_cookies_copy_and_removes_it(settings, store):
    settings.cookies_file.write_text("# Netscape\nexample.com\tTRUE\t/\tFALSE\t0\tk\tv\n")
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=CookieSpyYDL)
    path, content = CookieSpyYDL.seen
    assert path == str(settings.tmp_dir / f"cookies-{job.id}.txt")
    assert content == settings.cookies_file.read_text()
    assert not Path(path).exists()
    assert settings.cookies_file.read_text().startswith("# Netscape")
    assert list(settings.tmp_dir.glob("cookies-*")) == []
    assert store.get(job.id).status == DONE


class CookieSpyFailingYDL(CookieSpyYDL):
    def extract_info(self, url, download=True):
        raise yt_dlp.utils.DownloadError("ERROR: nope")


class CookieSpyCancelYDL(CookieSpyYDL):
    """Records the cookie copy, then lets the test remove the job so the hook raises DownloadCancelled."""
    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        CookieSpyCancelYDL.on_first_hook()
        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
        raise AssertionError("hook should have raised")


def test_failed_download_still_removes_cookies_copy(settings, store):
    settings.cookies_file.write_text("# cookies\n")
    job = store.add("https://example.com/v")
    CookieSpyFailingYDL.seen = None
    downloader.download(job.id, job.url, store, settings, ydl_class=CookieSpyFailingYDL)
    path, content = CookieSpyFailingYDL.seen
    assert path == str(settings.tmp_dir / f"cookies-{job.id}.txt") and content == "# cookies\n"
    assert store.get(job.id).status == FAILED
    assert list(settings.tmp_dir.glob("cookies-*")) == []


def test_cancelled_download_removes_cookies_copy(settings, store):
    settings.cookies_file.write_text("# cookies\n")
    job = store.add("https://example.com/v")
    CookieSpyCancelYDL.seen = None
    CookieSpyCancelYDL.on_first_hook = staticmethod(lambda: store.remove(job.id))
    downloader.download(job.id, job.url, store, settings, ydl_class=CookieSpyCancelYDL)
    path, _ = CookieSpyCancelYDL.seen
    assert path == str(settings.tmp_dir / f"cookies-{job.id}.txt")
    assert store.get(job.id) is None
    assert list(settings.tmp_dir.glob("cookies-*")) == []


def test_cookies_copy_is_owner_only(settings, store):
    settings.cookies_file.write_text("# cookies\n")
    settings.cookies_file.chmod(0o644)
    seen = {}

    class ModeSpyYDL(FakeYDL):
        def __init__(self, opts):
            super().__init__(opts)
            seen["mode"] = Path(opts["cookiefile"]).stat().st_mode & 0o777

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ModeSpyYDL)
    assert seen["mode"] == 0o600


def test_download_without_cookies_file_passes_no_cookiefile(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert "cookiefile" not in FakeYDL.last_opts


# -- thumbnails -----------------------------------------------------------------

THUMB_PPS = [
    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
]


def test_build_opts_mp4_embeds_thumbnail_by_default(settings):
    assert settings.embed_thumbnails is True
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp4", quality="1080")
    assert opts["writethumbnail"] is True
    assert opts["postprocessors"] == THUMB_PPS
    assert opts["merge_output_format"] == "mp4"


def test_build_opts_mp3_appends_thumbnail_postprocessors(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp3", quality="320")
    assert opts["writethumbnail"] is True
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
        {"key": "FFmpegMetadata"},
        *THUMB_PPS,
    ]


@pytest.mark.parametrize("fmt", ["mp4", "mp3"])
def test_build_opts_without_thumbnails(settings, fmt):
    settings.embed_thumbnails = False
    opts = downloader.build_opts(settings, "abc12345", _hook, format=fmt, quality="best")
    assert "writethumbnail" not in opts
    assert not any(pp["key"] in ("EmbedThumbnail", "FFmpegThumbnailsConvertor") for pp in opts.get("postprocessors", []))
    if fmt == "mp4":
        assert "postprocessors" not in opts


class FailingWithThumbYDL(FakeYDL):
    def extract_info(self, url, download=True):
        base = self.opts["outtmpl"].replace("%(title).80B", "x")
        Path(base.replace("%(ext)s", "mp4.part")).write_bytes(b"partial")
        Path(base.replace("%(ext)s", "jpg")).write_bytes(b"thumb")
        raise yt_dlp.utils.DownloadError("ERROR: boom")


def test_failed_download_removes_leftover_thumbnail(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FailingWithThumbYDL)
    assert store.get(job.id).status == FAILED
    assert list(settings.tmp_dir.iterdir()) == []


# -- speed / eta -------------------------------------------------------------------

class RateYDL(FakeYDL):
    """Fires a scripted list of progress-hook dicts, then writes an output file."""
    script: list = []

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Video"}
        for d in type(self).script:
            hook({**d, "info_dict": info})
        out = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
        out.write_bytes(b"x" * 10)
        return {**info, "requested_downloads": [{"filepath": str(out)}]}


def _record_updates(store, monkeypatch):
    calls = []
    real_update = store.update
    monkeypatch.setattr(store, "update", lambda *a, **kw: (calls.append(dict(kw)), real_update(*a, **kw))[1])
    return calls


def test_hook_writes_speed_and_eta_as_ints(settings, store, monkeypatch):
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200,
                       "speed": 1234567.8, "eta": 42.6}]
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL)
    first = calls[0]
    assert first["speed"] == 1234567 and first["eta"] == 42


def test_speed_and_eta_writes_are_throttled_to_one_per_second(settings, store, monkeypatch):
    # Same progress every time (so progress/title never trigger a write); clock advances 0.3 s per hook.
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200,
                       "speed": 1000 * i, "eta": 100 - i} for i in range(1, 9)]
    ticks = iter(i * 0.3 for i in range(100))
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL, clock=lambda: next(ticks))
    rate_calls = [c for c in calls if "speed" in c and "status" not in c]
    # t=0 (first write), then t>=1.0 (4th hook, t=0.9 is too early -> t=1.2), then t>=2.2 -> none left (8 hooks end at t=2.1)
    assert [c["speed"] for c in rate_calls] == [1000, 5000]


def test_speed_and_eta_ride_along_with_progress_writes(settings, store, monkeypatch):
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 10, "eta": 9},
                      {"status": "downloading", "downloaded_bytes": 100, "total_bytes": 200, "speed": 20, "eta": 8}]
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL, clock=lambda: 0.0)
    progress_calls = [c for c in calls if "progress" in c and "status" not in c]
    assert [(c["progress"], c["speed"], c["eta"]) for c in progress_calls] == [(25, 10, 9), (50, 20, 8)]


def test_finished_hook_clears_speed_and_eta_once(settings, store, monkeypatch):
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 10, "eta": 9},
                      {"status": "finished"}, {"status": "finished"}]
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL, clock=lambda: 0.0)
    clears = [c for c in calls if "status" not in c and c.get("speed") is None and "speed" in c]
    assert len(clears) == 1
    assert clears[0]["eta"] is None


def test_done_job_has_no_speed_or_eta(settings, store):
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 10, "eta": 9}]
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL)
    done = store.get(job.id)
    assert done.status == DONE and done.speed is None and done.eta is None


def test_failed_job_has_no_speed_or_eta(settings, store):
    class FailAfterRateYDL(FakeYDL):
        def extract_info(self, url, download=True):
            self.opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200,
                                             "speed": 10, "eta": 9, "info_dict": {"title": "t"}})
            raise yt_dlp.utils.DownloadError("ERROR: boom")

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FailAfterRateYDL)
    failed = store.get(job.id)
    assert failed.status == FAILED and failed.speed is None and failed.eta is None


def test_as_int_tolerates_none_and_garbage():
    assert downloader._as_int(None) is None
    assert downloader._as_int(0) is None
    assert downloader._as_int("x") is None
    assert downloader._as_int(12.9) == 12


def test_unchanged_progress_does_not_write_until_rate_is_due_but_changed_progress_writes_at_once(settings, store, monkeypatch):
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 1, "eta": 9},
                      {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 2, "eta": 8},   # t=0.5: nothing
                      {"status": "downloading", "downloaded_bytes": 100, "total_bytes": 200, "speed": 3, "eta": 7}]  # t=1.0: progress changed
    ticks = iter([0.0, 0.5, 0.6])
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL, clock=lambda: next(ticks))
    hook_calls = [c for c in calls if "status" not in c]
    assert [(c.get("progress"), c["speed"]) for c in hook_calls] == [(25, 1), (50, 3)]
