import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yt_dlp
from yt_dlp.utils import download_range_func

from app import downloader, thumbs
from app.jobs import AUTO_RETRIES, DONE, FAILED, QUEUED, RETRY_DELAY


class FakeYDL:
    """Simulates yt-dlp: fires progress hooks, writes an output file, returns info."""

    last_opts: dict = {}

    def __init__(self, opts):
        self.opts = opts
        self.params = opts   # as YoutubeDL; postprocessors built against it read it
        FakeYDL.last_opts = opts
        FakeYDL.added_pps = []

    def add_post_processor(self, pp, when="post_process"):
        FakeYDL.added_pps.append((type(pp).__name__, when))

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
    assert downloader.progress_percent(
        {"status": "downloading", "downloaded_bytes": 50, "total_bytes_estimate": 100}) == 50
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
    settings.cookies_file.write_text("# Netscape\nexample.com\tFALSE\t/\tFALSE\t0\tk\tv\n")
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=CookieSpyYDL)
    path, content = CookieSpyYDL.seen
    assert path == str(settings.tmp_dir / f"cookies-{job.id}.txt")
    # magic line added
    assert content == "# Netscape HTTP Cookie File\n# Netscape\nexample.com\tFALSE\t/\tFALSE\t0\tk\tv\n"
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
    assert path == str(settings.tmp_dir / f"cookies-{job.id}.txt")
    assert content == "# Netscape HTTP Cookie File\n# cookies\n"
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
    assert not any(pp["key"] in ("EmbedThumbnail", "FFmpegThumbnailsConvertor")
                   for pp in opts.get("postprocessors", []))
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
    # t=0 (first write), then t>=1.0 (4th hook, t=0.9 is too early -> t=1.2),
    # then t>=2.2 -> none left (8 hooks end at t=2.1)
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


def test_unchanged_progress_does_not_write_until_rate_is_due_but_changed_progress_writes_at_once(
        settings, store, monkeypatch):
    RateYDL.script = [{"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 1, "eta": 9},
                      # t=0.5: nothing
                      {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 2, "eta": 8},
                      # t=1.0: progress changed
                      {"status": "downloading", "downloaded_bytes": 100, "total_bytes": 200, "speed": 3, "eta": 7}]
    ticks = iter([0.0, 0.5, 0.6])
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=RateYDL, clock=lambda: next(ticks))
    hook_calls = [c for c in calls if "status" not in c]
    assert [(c.get("progress"), c["speed"]) for c in hook_calls] == [(25, 1), (50, 3)]


# -- rate limit --------------------------------------------------------------------

def test_build_opts_has_no_ratelimit_by_default(settings):
    assert "ratelimit" not in downloader.build_opts(settings, "abc", _hook)
    assert "ratelimit" not in downloader.build_opts(settings, "abc", _hook, format="mp3", quality="192")


@pytest.mark.parametrize("fmt, quality", [("mp4", "best"), ("mp4", "720"), ("mp3", "192")])
def test_build_opts_passes_rate_limit_for_every_format(settings, fmt, quality):
    settings.rate_limit = 5 * 1024 ** 2
    assert downloader.build_opts(settings, "abc", _hook, format=fmt, quality=quality)["ratelimit"] == 5 * 1024 ** 2


# -- audio (original) ---------------------------------------------------------------

class OpusYDL(FakeYDL):
    """Simulates FFmpegExtractAudio(best) on an opus stream: .webm in, .opus out, .webm deleted,
    requested_downloads.filepath left pointing at the vanished intermediate."""

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Song"}
        base = self.opts["outtmpl"].replace("%(title).80B", "My_Song")
        webm = Path(base.replace("%(ext)s", "webm"))
        opus = Path(base.replace("%(ext)s", "opus"))
        webm.write_bytes(b"w" * 20)
        hook({"status": "finished", "info_dict": info})
        opus.write_bytes(b"o" * 10)
        webm.unlink()
        return {**info, "requested_downloads": [{"filepath": str(webm)}]}


def test_build_opts_audio_copies_best_stream_without_reencoding(settings):
    settings.embed_thumbnails = False
    opts = downloader.build_opts(settings, "abc12345", _hook, format="audio", quality="best")
    assert opts["format"] == "bestaudio/best"
    assert "merge_output_format" not in opts and "format_sort" not in opts
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "best"},
        {"key": "FFmpegMetadata"},
    ]


def test_build_opts_audio_converts_thumbnail_but_leaves_embedding_to_the_tolerant_step(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, format="audio", quality="best")
    assert opts["writethumbnail"] is True
    assert opts["postprocessors"][2:] == [downloader.THUMBNAIL_POSTPROCESSORS[0]]   # convertor only
    assert all(pp["key"] != "EmbedThumbnail" for pp in opts["postprocessors"])


def test_download_audio_adds_tolerant_thumbnail_embedder_only_when_enabled(settings, store):
    job = store.add("https://example.com/v", format="audio", quality="best")
    downloader.download(job.id, job.url, store, settings, ydl_class=OpusYDL)
    assert FakeYDL.added_pps == [("TolerantEmbedThumbnailPP", "post_process")]
    settings.embed_thumbnails = False
    job = store.add("https://example.com/v2", format="audio", quality="best")
    downloader.download(job.id, job.url, store, settings, ydl_class=OpusYDL)
    assert FakeYDL.added_pps == []


@pytest.mark.parametrize("fmt, quality", [("mp4", "best"), ("mp3", "192")])
def test_download_mp4_and_mp3_use_the_plain_embed_step(settings, store, fmt, quality):
    job = store.add("https://example.com/v", format=fmt, quality=quality)
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.added_pps == []
    assert any(pp["key"] == "EmbedThumbnail" for pp in FakeYDL.last_opts["postprocessors"])


def test_tolerant_embedder_turns_unsupported_container_into_a_warning(monkeypatch):
    warnings = []
    pp = downloader.TolerantEmbedThumbnailPP()   # real yt-dlp init (run() is wrapped by its metaclass)
    monkeypatch.setattr(downloader.EmbedThumbnailPP, "run",
                        lambda self, info: (_ for _ in ()).throw(
                            yt_dlp.utils.PostProcessingError("Supported filetypes...")))
    monkeypatch.setattr(pp, "report_warning", lambda text: warnings.append(text))
    files, info = pp.run({"ext": "wav", "filepath": "x.wav"})
    assert (files, info) == ([], {"ext": "wav", "filepath": "x.wav"})
    assert warnings and "Supported filetypes" in warnings[0]


def test_tolerant_embedder_passes_success_through(monkeypatch):
    pp = downloader.TolerantEmbedThumbnailPP()   # real yt-dlp init (run() is wrapped by its metaclass)
    monkeypatch.setattr(downloader.EmbedThumbnailPP, "run", lambda self, info: (["t.jpg"], info))
    assert pp.run({"ext": "m4a"}) == (["t.jpg"], {"ext": "m4a"})


def test_download_audio_job_locates_whatever_container_came_out(settings, store):
    job = store.add("https://example.com/v", format="audio", quality="best")
    downloader.download(job.id, job.url, store, settings, ydl_class=OpusYDL)
    job = store.get(job.id)
    assert job.status == DONE
    assert job.filename == f"{job.id}-My_Song.opus"
    assert (settings.downloads_dir / job.filename).read_bytes() == b"o" * 10
    assert list(settings.tmp_dir.iterdir()) == []


def test_locate_output_without_ext_takes_the_largest_file(settings):
    tmp = settings.tmp_dir
    (tmp / "j1-x.m4a").write_bytes(b"1234")
    (tmp / "j1-x.jpg").write_bytes(b"1")
    (tmp / "j1-x.opus.part").write_bytes(b"123456789")
    assert downloader._locate_output(None, tmp, "j1", ext=None).name == "j1-x.m4a"


# -- subtitles -----------------------------------------------------------------

def test_build_opts_mp4_without_subtitles_requests_none(settings):
    opts = downloader.build_opts(settings, "abc", _hook, format="mp4", quality="1080")
    assert not any(k in opts for k in ("writesubtitles", "writeautomaticsub", "subtitleslangs"))
    assert all(pp["key"] != "FFmpegEmbedSubtitle" for pp in opts.get("postprocessors", []))


def test_build_opts_mp4_site_subtitles(settings):
    opts = downloader.build_opts(settings, "abc", _hook, format="mp4", quality="1080", subtitles="en")
    assert opts["writesubtitles"] is True and opts["writeautomaticsub"] is False
    assert opts["subtitleslangs"] == ["en"]
    assert opts["subtitlesformat"] == "vtt/srt/best"
    assert opts["postprocessors"][0] == {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}
    assert opts["postprocessors"][1:] == downloader.THUMBNAIL_POSTPROCESSORS   # thumbnails still embedded


def test_build_opts_mp4_automatic_captions(settings):
    opts = downloader.build_opts(settings, "abc", _hook, format="mp4", quality="best", subtitles="auto:en-orig")
    assert opts["writesubtitles"] is False and opts["writeautomaticsub"] is True
    assert opts["subtitleslangs"] == ["en\\-orig"]      # re.escape'd: literal match, never a regex/alias


def test_build_opts_escapes_regex_characters_in_subtitle_language(settings):
    opts = downloader.build_opts(settings, "abc", _hook, format="mp4", quality="best", subtitles="zh.Hans")
    assert opts["subtitleslangs"] == [r"zh\.Hans"]


@pytest.mark.parametrize("fmt, quality", [("mp3", "192"), ("audio", "best")])
def test_build_opts_ignores_subtitles_for_audio_formats(settings, fmt, quality):
    opts = downloader.build_opts(settings, "abc", _hook, format=fmt, quality=quality, subtitles="en")
    assert "subtitleslangs" not in opts
    assert all(pp["key"] != "FFmpegEmbedSubtitle" for pp in opts["postprocessors"])


def test_download_passes_the_job_subtitles_to_yt_dlp(settings, store):
    job = store.add("https://example.com/v", format="mp4", quality="720", subtitles="auto:en")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.last_opts["subtitleslangs"] == ["en"] and FakeYDL.last_opts["writeautomaticsub"] is True
    assert store.get(job.id).status == DONE


# -- impersonation -----------------------------------------------------------------

def test_impersonation_available_reflects_curl_cffi(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "curl_cffi" else None)
    assert downloader.impersonation_available() is True
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert downloader.impersonation_available() is False


@pytest.mark.parametrize("fmt, quality", [("mp4", "best"), ("mp3", "192"), ("audio", "best")])
def test_build_opts_impersonates_when_configured_and_available(settings, monkeypatch, fmt, quality):
    from yt_dlp.networking.impersonate import ImpersonateTarget
    monkeypatch.setattr(downloader, "target_supported", lambda t: t is not None)
    opts = downloader.build_opts(settings, "abc", _hook, format=fmt, quality=quality)
    assert opts["impersonate"] == ImpersonateTarget.from_str("chrome")
    settings.impersonate = "safari-17:ios"
    assert downloader.build_opts(settings, "abc", _hook)["impersonate"] == ImpersonateTarget.from_str("safari-17:ios")


def test_build_opts_omits_impersonate_when_disabled_or_unavailable(settings, monkeypatch):
    monkeypatch.setattr(downloader, "target_supported", lambda t: False)
    assert "impersonate" not in downloader.build_opts(settings, "abc", _hook)
    monkeypatch.setattr(downloader, "target_supported", lambda t: t is not None)
    settings.impersonate = None
    assert "impersonate" not in downloader.build_opts(settings, "abc", _hook)


def test_target_supported_asks_yt_dlp_and_rejects_unknown_targets():
    downloader._target_supported.cache_clear()
    assert downloader.target_supported(None) is False
    if downloader.impersonation_available():
        assert downloader.target_supported("chrome") is True
        assert downloader.target_supported("chrome-999") is False        # grammar-valid, unsupported
        assert downloader.target_supported("netscape-4") is False
    else:
        assert downloader.target_supported("chrome") is False


def test_target_supported_is_false_without_curl_cffi(monkeypatch):
    monkeypatch.setattr(downloader, "impersonation_available", lambda: False)
    assert downloader.target_supported("chrome") is False


def test_real_yt_dlp_accepts_the_chrome_target_with_curl_cffi_installed(settings):
    pytest.importorskip("curl_cffi")
    opts = downloader.build_opts(settings, "abc", _hook)
    with yt_dlp.YoutubeDL({**opts, "quiet": True}) as ydl:     # raises YoutubeDLError if the target is unavailable
        assert ydl.params["impersonate"] == opts["impersonate"]


# -- login wall / cookies --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "ERROR: [instagram] abc: You need to log in to access this content. Use --cookies-from-browser or --cookies",
    "ERROR: [facebook] 123: This content is unreachable; it may be private or require a login",
    "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
    "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
    "This video is only available for registered users",
    "Requested content is not available, rate-limit reached or login required",
    "Join this channel to get access to members-only content",
    "Sign in to confirm your age. This video may be inappropriate for some users (age-restricted)",
])
def test_looks_login_walled_recognises_yt_dlp_phrasings(text):
    assert downloader.looks_login_walled(text) is True


@pytest.mark.parametrize("text", ["Unable to download webpage: HTTP Error 404", "Unsupported URL", "", None,
                                  "ffmpeg exited with code 1", "Video unavailable"])
def test_looks_login_walled_ignores_other_errors(text):
    assert downloader.looks_login_walled(text) is False


def test_failed_download_marks_needs_login_when_the_error_is_a_login_wall(settings, store):
    class LoginWallYDL(FakeYDL):
        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("ERROR: [instagram] abc: You need to log in to access this content.\n"
                                             "Use --cookies-from-browser or --cookies for the authentication")

    job = store.add("https://www.instagram.com/p/abc/")
    downloader.download(job.id, job.url, store, settings, ydl_class=LoginWallYDL)
    failed = store.get(job.id)
    assert failed.status == FAILED and failed.needs_login is True
    assert failed.error.startswith("[instagram] abc: You need to log in")

    plain = store.add("https://example.com/v")
    downloader.download(plain.id, plain.url, store, settings, ydl_class=FailingYDL)
    assert store.get(plain.id).needs_login is False


def test_use_once_cookies_are_passed_to_that_job_and_discarded_afterwards(settings, store):
    from app.cookies import CookieStore
    job = store.add("https://www.instagram.com/p/abc/")
    once = ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tS\n"
    CookieStore(settings).add(once, keep=False, job_id=job.id)
    CookieSpyYDL.seen = None
    downloader.download(job.id, job.url, store, settings, ydl_class=CookieSpyYDL)
    path, content = CookieSpyYDL.seen
    assert path == str(settings.tmp_dir / f"cookies-{job.id}.txt") and content == "# Netscape HTTP Cookie File\n" + once
    assert store.get(job.id).status == DONE
    assert list(settings.tmp_dir.glob("cookies-*")) == []       # merged copy and once-file both gone


def test_use_once_cookies_are_discarded_after_a_failure_and_after_cancel(settings, store):
    from app.cookies import CookieStore
    job = store.add("https://example.com/v")
    CookieStore(settings).add("a.com\tFALSE\t/\tTRUE\t0\tk\tv\n", keep=False, job_id=job.id)
    downloader.download(job.id, job.url, store, settings, ydl_class=FailingYDL)
    assert list(settings.tmp_dir.glob("cookies-*")) == []

    job = store.add("https://example.com/v2")
    CookieStore(settings).add("a.com\tFALSE\t/\tTRUE\t0\tk\tv\n", keep=False, job_id=job.id)
    store.remove(job.id)                                          # removed before the worker starts it
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert list(settings.tmp_dir.glob("cookies-*")) == []


# -- round 6: clips, proxy, fragments ---------------------------------------------------

@pytest.mark.parametrize("start, end, expected", [
    (10.0, 20.5, (10.0, 20.5)),
    (None, 30.0, (0, 30.0)),
    (15.0, None, (15.0, float("inf"))),
    (0.0, 5.0, (0, 5.0)),
])
@pytest.mark.parametrize("fmt", ["mp4", "mp3", "audio"])
def test_build_opts_clip_sets_download_ranges(settings, start, end, expected, fmt):
    opts = downloader.build_opts(settings, "abc12345", _hook, format=fmt, clip_start=start, clip_end=end)
    assert opts["download_ranges"] == download_range_func(None, [expected])
    assert list(opts["download_ranges"]({}, None)) == [{"start_time": expected[0], "end_time": expected[1]}]
    assert "force_keyframes_at_cuts" not in opts


def test_build_opts_without_clip_has_no_ranges(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook)
    assert "download_ranges" not in opts and "force_keyframes_at_cuts" not in opts


def test_build_opts_proxy_and_fragments_only_when_set(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook)
    assert "proxy" not in opts and "concurrent_fragment_downloads" not in opts
    settings.proxy = "socks5://user:pw@proxy.local:1080"
    settings.fragments = 4
    opts = downloader.build_opts(settings, "abc12345", _hook)
    assert opts["proxy"] == "socks5://user:pw@proxy.local:1080"
    assert opts["concurrent_fragment_downloads"] == 4


def test_build_opts_logger_only_when_given(settings):
    assert "logger" not in downloader.build_opts(settings, "abc12345", _hook)
    logger = downloader.JobLogger(settings.tmp_dir)
    opts = downloader.build_opts(settings, "abc12345", _hook, logger=logger)
    assert opts["logger"] is logger
    # With a logger yt-dlp would route every progress-bar redraw into it and flood the ring buffer.
    assert opts["noprogress"] is True


def test_download_passes_the_job_clip_to_yt_dlp(settings, store):
    job = store.add("https://example.com/v", clip_start=5.0, clip_end=None)
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.last_opts["download_ranges"] == download_range_func(None, [(5.0, float("inf"))])
    assert isinstance(FakeYDL.last_opts["logger"], downloader.JobLogger)


# -- JobLogger ------------------------------------------------------------------------

def test_job_logger_keeps_all_levels_and_drops_debug_prefixed(tmp_path):
    lg = downloader.JobLogger(tmp_path)
    lg.debug("[youtube] abc: Downloading webpage")
    lg.debug("[debug] Invoking http downloader")
    lg.info("info line")
    lg.warning("WARNING: something")
    lg.error("ERROR: broke")
    assert lg.text() == "[youtube] abc: Downloading webpage\ninfo line\nWARNING: something\nERROR: broke"


def test_job_logger_keeps_the_last_60_lines(tmp_path):
    lg = downloader.JobLogger(tmp_path)
    for i in range(100):
        lg.info(f"line {i}")
    lines = lg.text().split("\n")
    assert len(lines) == downloader.LOG_LINES == 60
    assert lines[0] == "line 40" and lines[-1] == "line 99"


def test_job_logger_cuts_long_lines(tmp_path):
    lg = downloader.JobLogger(tmp_path)
    lg.error("x" * 1000)
    assert lg.text() == "x" * downloader.LOG_LINE_CHARS


def test_job_logger_splits_multiline_messages(tmp_path):
    lg = downloader.JobLogger(tmp_path)
    lg.error("ERROR: first\n  second\n")
    assert lg.text() == "ERROR: first\n  second"


def test_job_logger_caps_utf8_bytes_by_dropping_oldest_lines(tmp_path):
    lg = downloader.JobLogger(tmp_path)
    for i in range(60):
        lg.info(f"{i:02d}" + "é" * 140)   # 2 + 280 bytes per line, 60 lines > 8 KB
    text = lg.text()
    assert len(text.encode("utf-8")) <= downloader.LOG_MAX_BYTES == 8192
    lines = text.split("\n")
    assert lines[-1].startswith("59") and not lines[0].startswith("00")
    # dropping one more line would be unnecessary: the cap is met with the fewest lines removed
    assert len(("\n".join([f"{int(lines[0][:2]) - 1:02d}" + "é" * 140] + lines)).encode()) > 8192


def test_job_logger_scrubs_tmp_dir_and_cookie_names(tmp_path):
    lg = downloader.JobLogger(tmp_path / "tmp")
    lg.info(f"[info] Writing {tmp_path / 'tmp'}/abc12345-Title.mp4")
    lg.warning(f"cookies at {tmp_path / 'tmp'}/cookies-probe-1a2b.txt and cookies-once-Abc_12-x.txt")
    text = lg.text()
    assert str(tmp_path) not in text
    assert text == "[info] Writing <tmp>/abc12345-Title.mp4\ncookies at <tmp>/<cookies> and <cookies>"


def test_job_logger_scrubs_before_cutting(tmp_path):
    long_dir = tmp_path / ("d" * 280)
    lg = downloader.JobLogger(long_dir)
    lg.info(f"{long_dir}/file")
    assert lg.text() == "<tmp>/file"


def test_job_logger_tolerates_non_string_messages(tmp_path):
    lg = downloader.JobLogger(tmp_path)
    lg.info(None)
    lg.error(ValueError("boom"))
    assert lg.text() == "None\nboom"


class LoggingFailingYDL(FakeYDL):
    def extract_info(self, url, download=True):
        lg = self.opts["logger"]
        lg.debug("[youtube] v: Downloading webpage")
        lg.debug(f"[debug] cookies {self.opts.get('cookiefile')}")
        lg.error("ERROR: [youtube] v: Video unavailable")
        raise yt_dlp.utils.DownloadError("ERROR: [youtube] v: Video unavailable")


class LoggingFakeYDL(FakeYDL):
    def extract_info(self, url, download=True):
        self.opts["logger"].debug("[youtube] v: Downloading webpage")
        return super().extract_info(url, download)


def test_failed_download_stores_the_scrubbed_log(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=LoggingFailingYDL)
    job = store.get(job.id)
    assert job.status == FAILED
    assert job.log == "[youtube] v: Downloading webpage\nERROR: [youtube] v: Video unavailable"


def test_failed_download_logs_non_yt_dlp_errors_too(settings, store):
    class NoOutputYDL(FakeYDL):
        def extract_info(self, url, download=True):
            return {"title": "T"}

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=NoOutputYDL)
    assert store.get(job.id).log == "ERROR: download finished but no output file was found"


def test_done_download_leaves_log_unset(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=LoggingFakeYDL)
    job = store.get(job.id)
    assert job.status == DONE and job.log is None


# -- thumbnail from the progress hook -------------------------------------------------------

class ThumbYDL(FakeYDL):
    thumbnail = "https://i.example/v/hq.jpg"

    def extract_info(self, url, download=True):
        hook = self.opts["progress_hooks"][0]
        info = {"title": "My Video", "thumbnail": type(self).thumbnail}
        for done in (50, 100, 150):
            hook({"status": "downloading", "downloaded_bytes": done, "total_bytes": 200, "info_dict": info})
        out = Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
        out.write_bytes(b"x" * 10)
        hook({"status": "finished", "info_dict": info})
        self.opts["postprocessor_hooks"][0]({"status": "started", "postprocessor": "Merger", "info_dict": info})
        return {**info, "requested_downloads": [{"filepath": str(out)}]}


def _spy_fetch(monkeypatch, result="ok"):
    calls = []

    def fake(url, dest_dir, key, settings, opener=None):
        calls.append({"url": url, "dest_dir": dest_dir, "key": key, "settings": settings})
        if isinstance(result, BaseException):
            raise result
        return f"{key}.jpg" if result == "ok" else None

    monkeypatch.setattr(thumbs, "fetch_thumbnail", fake)
    return calls


def test_hook_fetches_the_thumbnail_once_and_stores_the_key(settings, store, monkeypatch):
    calls = _spy_fetch(monkeypatch)
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL)
    assert len(calls) == 1
    assert calls[0]["url"] == ThumbYDL.thumbnail and calls[0]["dest_dir"] == settings.thumbs_dir
    assert calls[0]["settings"] is settings and thumbs.KEY_PATTERN.fullmatch(calls[0]["key"])
    job = store.get(job.id)
    assert job.status == DONE and job.thumb == calls[0]["key"]


def test_hook_skips_the_thumbnail_when_the_job_already_has_one(settings, store, monkeypatch):
    calls = _spy_fetch(monkeypatch)
    job = store.add("https://example.com/v", thumb="probekey1")
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL)
    assert calls == [] and store.get(job.id).thumb == "probekey1"


@pytest.mark.parametrize("thumbnail", [None, "", "file:///etc/passwd", "data:image/png;base64,AA", 7])
def test_hook_ignores_missing_or_non_http_thumbnails(settings, store, monkeypatch, thumbnail):
    calls = _spy_fetch(monkeypatch)
    monkeypatch.setattr(ThumbYDL, "thumbnail", thumbnail)
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL)
    assert calls == [] and store.get(job.id).thumb is None and store.get(job.id).status == DONE


def test_hook_tries_once_even_when_the_fetch_fails(settings, store, monkeypatch):
    calls = _spy_fetch(monkeypatch, result=None)
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL)
    assert len(calls) == 1 and store.get(job.id).thumb is None and store.get(job.id).status == DONE


def test_hook_swallows_thumbnail_exceptions(settings, store, monkeypatch):
    calls = _spy_fetch(monkeypatch, result=RuntimeError("boom"))
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL)
    assert len(calls) == 1 and store.get(job.id).status == DONE and store.get(job.id).thumb is None


def test_hook_thumbnail_keeps_progress_writes_unchanged(settings, store, monkeypatch):
    _spy_fetch(monkeypatch)
    job = store.add("https://example.com/v")
    calls = _record_updates(store, monkeypatch)
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL, clock=lambda: 0.0)
    assert [c["progress"] for c in calls if "progress" in c and "status" not in c] == [25, 50, 75, 99]
    assert sum("thumb" in c for c in calls) == 1


# -- clip_label --------------------------------------------------------------------------

@pytest.mark.parametrize("start, end, label", [
    (0, 90, "0:00–1:30"),
    (None, 65.9, "0:00–1:05"),
    (125.0, None, "2:05–end"),
    (3600, 3725, "1:00:00–1:02:05"),
    (59, 3600, "0:59–1:00:00"),
    (None, None, "0:00–end"),
])
def test_clip_label(start, end, label):
    assert downloader.clip_label(start, end) == label


# -- review fixes: log secrets, concurrent thumbnail hooks, job removed mid-fetch -------------

def test_job_logger_redacts_given_secrets(tmp_path):
    proxy = "socks5://user:pw@proxy.local:1080"
    lg = downloader.JobLogger(tmp_path, secrets=[proxy, ""])
    lg.error(f"ERROR: Unable to connect to proxy {proxy}: refused")
    assert lg.text() == "ERROR: Unable to connect to proxy <redacted>: refused"


def test_failed_download_log_never_stores_the_proxy_url(settings, store):
    settings.proxy = "http://user:s3cret@proxy.local:3128"

    class ProxyFailingYDL(FakeYDL):
        def extract_info(self, url, download=True):
            self.opts["logger"].error(f"ERROR: Unable to download: proxy {self.opts['proxy']} refused")
            raise yt_dlp.utils.DownloadError("ERROR: Unable to download")

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=ProxyFailingYDL)
    job = store.get(job.id)
    assert job.status == FAILED and "s3cret" not in job.log and "<redacted>" in job.log


def test_concurrent_hooks_fetch_the_thumbnail_once(settings, store, monkeypatch):
    import threading
    import time
    calls = []

    def slow_fetch(url, dest_dir, key, settings, opener=None):
        calls.append(key)
        time.sleep(0.05)
        return f"{key}.jpg"

    real_is_http = thumbs.is_http_url

    def slow_is_http(url):   # widen the check-then-set window the way a busy thread would
        time.sleep(0.05)
        return real_is_http(url)

    monkeypatch.setattr(thumbs, "fetch_thumbnail", slow_fetch)
    monkeypatch.setattr(thumbs, "is_http_url", slow_is_http)

    class FragmentYDL(FakeYDL):
        def extract_info(self, url, download=True):
            hook = self.opts["progress_hooks"][0]
            info = {"title": "T", "thumbnail": "https://i.example/t.jpg"}
            d = {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "info_dict": info}
            threads = [threading.Thread(target=hook, args=(d,)) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return super().extract_info(url, download)

    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FragmentYDL)
    assert len(calls) == 1 and store.get(job.id).thumb == calls[0]


def test_job_removed_during_thumbnail_fetch_deletes_the_fetched_file(settings, store, monkeypatch):
    job = store.add("https://example.com/v")
    written = []

    def fetch_then_remove(url, dest_dir, key, settings, opener=None):
        path = dest_dir / f"{key}.jpg"
        path.write_bytes(b"\xff\xd8\xff")
        written.append(path)
        store.remove(job.id)   # the user removed the row while the fetch ran
        return path.name

    monkeypatch.setattr(thumbs, "fetch_thumbnail", fetch_then_remove)
    downloader.download(job.id, job.url, store, settings, ydl_class=ThumbYDL)
    assert len(written) == 1 and not written[0].exists() and store.get(job.id) is None


# -- round 7: exact cut, transient errors, automatic retry ------------------------------

def test_build_opts_exact_cut_forces_keyframes_only_with_a_clip(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, clip_start=10.0, clip_end=20.0, clip_exact=True)
    assert opts["force_keyframes_at_cuts"] is True
    opts = downloader.build_opts(settings, "abc12345", _hook, clip_start=None, clip_end=20.0, clip_exact=True)
    assert opts["force_keyframes_at_cuts"] is True
    assert "force_keyframes_at_cuts" not in downloader.build_opts(settings, "abc12345", _hook, clip_exact=True)
    assert "force_keyframes_at_cuts" not in downloader.build_opts(settings, "abc12345", _hook, clip_start=10.0)


def test_download_passes_clip_exact_to_yt_dlp(settings, store):
    job = store.add("https://example.com/v", clip_start=5.0, clip_end=9.0, clip_exact=True)
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.last_opts["force_keyframes_at_cuts"] is True


@pytest.mark.parametrize("start, end, label", [
    (0, 90, "0:00–1:30 exact"),
    (125.0, None, "2:05–end exact"),
])
def test_clip_label_exact(start, end, label):
    assert downloader.clip_label(start, end, exact=True) == label
    assert downloader.clip_label(start, end, exact=False) == label.removesuffix(" exact")


@pytest.mark.parametrize("text", [
    "ERROR: [generic] Unable to download webpage: The read operation timed out",
    "Read timed out. (read timeout=20.0)",
    "socket timeout",
    "Connection reset by peer",
    "[Errno 111] Connection refused",
    "Connection aborted.",
    "Remote end closed connection without response",
    "[Errno -3] Temporary failure in name resolution",
    "[Errno -2] Name or service not known",
    "nodename nor servname provided, or not known",
    "[Errno -5] No address associated with hostname",
    "EOF occurred in violation of protocol (_ssl.c:2427)",
    "IncompleteRead(1024 bytes read)... incomplete read",
    "ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests",
    "HTTP Error 503: Service Unavailable",
    "HTTP Error 502: Bad Gateway",
    "ERROR: unable to download video data: read error",
    "[Errno 101] Network is unreachable",
    "ERROR: [generic] 503: Unable to download webpage: Failed to perform, curl: (56) Connection closed abruptly. "
    "(caused by TransportError('Failed to perform, curl: (56) Connection closed abruptly.'))",
])
def test_is_transient_recognises_network_hiccups(text):
    assert downloader.is_transient(text) is True


@pytest.mark.parametrize("text", [
    "ERROR: [tiktok] Unable to extract video",
    "HTTP Error 404: Not Found",
    "HTTP Error 403: Forbidden",
    "Unsupported URL",
    "",
    None,
    # Login-walled texts are never transient, even with a retryable status in them.
    "HTTP Error 429: Too Many Requests. Requested content is not available, rate-limit reached or login required",
    "Sign in to confirm you're not a bot. Connection timed out",
])
def test_is_transient_ignores_permanent_and_login_walled_errors(text):
    assert downloader.is_transient(text) is False


class TransientYDL(FakeYDL):
    calls = 0

    def extract_info(self, url, download=True):
        TransientYDL.calls += 1
        self.opts["logger"].warning("WARNING: retrying fragment 1")
        partial = Path(self.opts["outtmpl"].replace("%(title).80B", "x").replace("%(ext)s", "mp4.part"))
        partial.write_bytes(b"partial")
        raise yt_dlp.utils.DownloadError("ERROR: Unable to download webpage: HTTP Error 503: Service Unavailable")


def test_transient_failure_requeues_the_job_with_a_retry_time(settings, store):
    job = store.add("https://example.com/v")
    store.update(job.id, progress=40, speed=1000, eta=5)
    before = datetime.now(timezone.utc)
    downloader.download(job.id, job.url, store, settings, ydl_class=TransientYDL)
    after = datetime.now(timezone.utc)

    job = store.get(job.id)
    assert job.status == QUEUED
    assert job.attempts == 1
    assert job.error == "retrying: Unable to download webpage: HTTP Error 503: Service Unavailable"
    assert job.progress == 0 and job.speed is None and job.eta is None
    assert job.needs_login is False
    assert "retrying fragment 1" in job.log
    retry_at = datetime.fromisoformat(job.retry_at)
    assert retry_at.tzinfo is not None
    assert before.timestamp() + RETRY_DELAY - 1 <= retry_at.timestamp() <= after.timestamp() + RETRY_DELAY + 1
    assert job.filename is None
    assert list(settings.tmp_dir.iterdir()) == []      # partials removed as on the failed path
    assert list(settings.downloads_dir.iterdir()) == []


def test_second_transient_failure_marks_the_job_failed(settings, store):
    job = store.add("https://example.com/v")
    store.update(job.id, attempts=AUTO_RETRIES)
    downloader.download(job.id, job.url, store, settings, ydl_class=TransientYDL)
    job = store.get(job.id)
    assert job.status == FAILED
    assert job.error == "Unable to download webpage: HTTP Error 503: Service Unavailable"
    assert job.attempts == AUTO_RETRIES


def test_permanent_failure_is_not_retried(settings, store):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=FailingYDL)
    job = store.get(job.id)
    assert job.status == FAILED and job.attempts == 0 and job.retry_at is None


def test_transient_retry_discards_use_once_cookies(settings, store):
    from app.cookies import CookieStore
    job = store.add("https://example.com/v")
    CookieStore(settings).add("a.com\tFALSE\t/\tTRUE\t0\tk\tv\n", keep=False, job_id=job.id)
    downloader.download(job.id, job.url, store, settings, ydl_class=TransientYDL)
    assert store.get(job.id).status == QUEUED
    assert list(settings.tmp_dir.glob("cookies-*")) == []


# -- round 9: SponsorBlock, H.264 preference, separate subtitle files ------------------

YT = "https://www.youtube.com/watch?v=abc"


def _keys(opts):
    return [pp["key"] for pp in opts.get("postprocessors", [])]


def test_sponsorblock_constants():
    assert downloader.SPONSORBLOCK_CATEGORIES == ["sponsor", "selfpromo", "interaction", "intro", "outro"]
    assert downloader.MP4_COMPAT_FORMAT_SORT == ["res", "vcodec:h264", "acodec:aac"]


@pytest.mark.parametrize("url", [YT, "https://youtu.be/abc", "https://music.youtube.com/watch?v=abc"])
@pytest.mark.parametrize("fmt,quality", [("mp4", "best"), ("mp3", "320"), ("audio", "best")])
def test_build_opts_sponsorblock_on_youtube_goes_first(settings, url, fmt, quality):
    opts = downloader.build_opts(settings, "abc", _hook, format=fmt, quality=quality,
                                 sponsorblock=True, url=url)
    pps = opts["postprocessors"]
    assert pps[0] == {"key": "SponsorBlock", "categories": downloader.SPONSORBLOCK_CATEGORIES,
                      "when": "after_filter"}
    assert pps[1] == {"key": "ModifyChapters", "remove_sponsor_segments": downloader.SPONSORBLOCK_CATEGORIES,
                      "force_keyframes": False, "when": "post_process"}
    assert _keys(opts).count("SponsorBlock") == 1


def test_build_opts_sponsorblock_with_subtitles_keeps_embed_after_it(settings):
    opts = downloader.build_opts(settings, "abc", _hook, subtitles="en", sponsorblock=True, url=YT)
    assert _keys(opts)[:3] == ["SponsorBlock", "ModifyChapters", "FFmpegEmbedSubtitle"]


@pytest.mark.parametrize("url", ["https://www.tiktok.com/@u/video/1", None, "not a url"])
def test_build_opts_sponsorblock_ignored_off_youtube(settings, url):
    opts = downloader.build_opts(settings, "abc", _hook, sponsorblock=True, url=url)
    assert "SponsorBlock" not in _keys(opts) and "ModifyChapters" not in _keys(opts)


def test_build_opts_no_sponsorblock_by_default(settings):
    opts = downloader.build_opts(settings, "abc", _hook, url=YT)
    assert "SponsorBlock" not in _keys(opts)


def test_build_opts_compat_sort_for_mp4(settings):
    opts = downloader.build_opts(settings, "abc", _hook, quality="720", compat=True)
    assert opts["format_sort"] == ["res", "vcodec:h264", "acodec:aac"]
    assert opts["format_sort"] is not downloader.MP4_COMPAT_FORMAT_SORT   # a copy
    assert downloader.build_opts(settings, "abc", _hook)["format_sort"] == downloader.MP4_FORMAT_SORT


@pytest.mark.parametrize("fmt,quality", [("mp3", "192"), ("audio", "best")])
def test_build_opts_compat_ignored_for_audio(settings, fmt, quality):
    assert "format_sort" not in downloader.build_opts(settings, "abc", _hook, format=fmt, quality=quality,
                                                      compat=True)


@pytest.mark.parametrize("sub,auto", [("en", False), ("auto:de", True)])
def test_build_opts_subtitle_file_mode(settings, sub, auto):
    opts = downloader.build_opts(settings, "abc", _hook, subtitles=sub, subtitles_mode="file")
    assert opts["writesubtitles"] is (not auto) and opts["writeautomaticsub"] is auto
    assert opts["subtitleslangs"] == [sub.removeprefix("auto:")]
    assert opts["subtitlesformat"] == "srt/vtt/best"
    assert {"key": "FFmpegSubtitlesConvertor", "format": "srt"} in opts["postprocessors"]
    assert "FFmpegEmbedSubtitle" not in _keys(opts)


def test_build_opts_subtitle_embed_mode_unchanged(settings):
    opts = downloader.build_opts(settings, "abc", _hook, subtitles="en", subtitles_mode="embed")
    assert opts["subtitlesformat"] == "vtt/srt/best"
    assert "FFmpegEmbedSubtitle" in _keys(opts) and "FFmpegSubtitlesConvertor" not in _keys(opts)


def test_build_opts_file_mode_without_subtitles_fetches_none(settings):
    opts = downloader.build_opts(settings, "abc", _hook, subtitles_mode="file")
    assert "writesubtitles" not in opts and "FFmpegSubtitlesConvertor" not in _keys(opts)


def test_collect_sidecars_moves_and_names(settings):
    tmp = settings.tmp_dir
    dest = settings.downloads_dir / "j1-My_Video.mp4"
    dest.write_bytes(b"v")
    (tmp / "j1-My_Video.en.srt").write_text("1")
    (tmp / "j1-My_Video.pt-BR.srt").write_text("2")
    (tmp / "j2-Other.en.srt").write_text("other job")
    (tmp / "j1-My_Video.en.vtt").write_text("not srt")
    names = downloader._collect_sidecars(tmp, "j1", dest)
    assert sorted(names) == ["j1-My_Video.en.srt", "j1-My_Video.pt-BR.srt"]
    assert (settings.downloads_dir / "j1-My_Video.en.srt").read_text() == "1"
    assert not (tmp / "j1-My_Video.en.srt").exists()
    assert (tmp / "j2-Other.en.srt").exists()


def test_collect_sidecars_title_with_dots_and_collision(settings):
    tmp = settings.tmp_dir
    dest = settings.downloads_dir / "j1-v1.2_final.mp4"
    (tmp / "j1-v1.2_final.en.srt").write_text("new")
    (settings.downloads_dir / "j1-v1.2_final.en.srt").write_text("old")
    assert downloader._collect_sidecars(tmp, "j1", dest) == ["j1-v1.2_final.en (2).srt"]
    assert (settings.downloads_dir / "j1-v1.2_final.en.srt").read_text() == "old"


def test_collect_sidecars_none_is_fine(settings):
    assert downloader._collect_sidecars(settings.tmp_dir, "j1", settings.downloads_dir / "j1-x.mp4") == []


class SidecarYDL(FakeYDL):
    def extract_info(self, url, download=True):
        info = super().extract_info(url, download)
        Path(self.opts["outtmpl"].replace("%(title).80B", "My_Video").replace("%(ext)s", "en.srt")).write_text("s")
        return info


def test_download_file_mode_records_sidecars_and_passes_job_fields(store, settings):
    job = store.add(YT, subtitles="en", subtitles_mode="file", sponsorblock=True, compat=True)
    downloader.download(job.id, job.url, store, settings, ydl_class=SidecarYDL)
    done = store.get(job.id)
    assert done.status == DONE
    assert done.sidecars == [f"{job.id}-My_Video.en.srt"]
    assert (settings.downloads_dir / done.sidecars[0]).read_text() == "s"
    assert not list(settings.tmp_dir.glob(f"{job.id}-*"))
    opts = SidecarYDL.last_opts
    assert _keys(opts)[0] == "SponsorBlock"
    assert opts["format_sort"] == downloader.MP4_COMPAT_FORMAT_SORT
    assert "FFmpegSubtitlesConvertor" in _keys(opts)


def test_download_without_sidecar_is_not_an_error(store, settings):
    job = store.add(YT, subtitles="en", subtitles_mode="file")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert store.get(job.id).status == DONE and store.get(job.id).sidecars == []


def test_download_cleared_while_finishing_removes_sidecars(store, settings):
    job = store.add(YT, subtitles="en", subtitles_mode="file")

    class ClearYDL(SidecarYDL):
        def extract_info(self, url, download=True):
            info = super().extract_info(url, download)
            store.remove(job.id)
            return info

    downloader.download(job.id, job.url, store, settings, ydl_class=ClearYDL)
    assert list(settings.downloads_dir.iterdir()) == []


# -- fallbacks: subtitles / SponsorBlock refused ------------------------------------------

SUB_429 = "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"
SB_DOWN = "ERROR: Postprocessing: Unable to communicate with SponsorBlock API: <urlopen error timed out>"


def _flaky(*errors):
    """FakeYDL whose first len(errors) extract_info calls raise those DownloadErrors."""

    class Flaky(FakeYDL):
        opts_seen: list = []

        def extract_info(self, url, download=True):
            Flaky.opts_seen.append(self.opts)
            if len(Flaky.opts_seen) <= len(errors):
                Path(self.opts["outtmpl"].replace("%(title).80B", "x").replace("%(ext)s", "en.vtt")).write_text("v")
                raise yt_dlp.utils.DownloadError(errors[len(Flaky.opts_seen) - 1])
            return super().extract_info(url, download)

    Flaky.opts_seen = []
    return Flaky


@pytest.mark.parametrize("mode", ["embed", "file"])
def test_subtitle_failure_downloads_again_without_subtitles(store, settings, mode):
    job = store.add(YT, subtitles="en", subtitles_mode=mode, compat=True)
    ydl = _flaky(SUB_429)
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    done = store.get(job.id)
    assert done.status == DONE and done.error is None
    assert done.note == downloader.SUBTITLES_NOTE
    assert done.sidecars == []
    first, second = ydl.opts_seen
    assert first["writesubtitles"] is True
    assert "writesubtitles" not in second and "subtitleslangs" not in second
    assert not any(k.startswith("FFmpegEmbedSubtitle") or k == "FFmpegSubtitlesConvertor" for k in _keys(second))
    assert second["format_sort"] == downloader.MP4_COMPAT_FORMAT_SORT   # everything else kept
    assert not list(settings.tmp_dir.glob(f"{job.id}-*"))
    assert store.get(job.id).subtitles == "en"   # the job keeps what was asked for


def test_other_download_error_still_fails(store, settings):
    job = store.add(YT, subtitles="en")
    ydl = _flaky("ERROR: [youtube] abc: Video unavailable")
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    assert store.get(job.id).status == FAILED and store.get(job.id).note is None
    assert len(ydl.opts_seen) == 1


def test_subtitle_error_without_subtitles_requested_is_not_retried(store, settings):
    job = store.add(YT)
    ydl = _flaky(SUB_429)
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    assert len(ydl.opts_seen) == 1
    assert store.get(job.id).status != DONE   # 429 is transient: the automatic retry handles it


def test_sponsorblock_failure_downloads_again_uncut(store, settings):
    job = store.add(YT, sponsorblock=True)
    ydl = _flaky(SB_DOWN)
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    done = store.get(job.id)
    assert done.status == DONE and done.note == downloader.SPONSORBLOCK_NOTE
    first, second = ydl.opts_seen
    assert _keys(first)[0] == "SponsorBlock"
    assert "SponsorBlock" not in _keys(second) and "ModifyChapters" not in _keys(second)


def test_both_fallbacks_join_their_notes(store, settings):
    job = store.add(YT, sponsorblock=True, subtitles="en")
    ydl = _flaky(SB_DOWN, SUB_429)
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    done = store.get(job.id)
    assert done.status == DONE and len(ydl.opts_seen) == 3
    assert done.note == f"{downloader.SPONSORBLOCK_NOTE}; {downloader.SUBTITLES_NOTE}"


def test_sponsorblock_failure_matches_only_the_api_error():
    assert downloader.SPONSORBLOCK_FAILURE == "SponsorBlock API"
    assert downloader.SPONSORBLOCK_FAILURE in SB_DOWN


def test_other_error_naming_sponsorblock_still_fails(store, settings):
    job = store.add(YT, sponsorblock=True)
    ydl = _flaky("ERROR: Postprocessing: SponsorBlock: Conversion failed!")
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    assert store.get(job.id).status == FAILED and store.get(job.id).note is None
    assert len(ydl.opts_seen) == 1


def test_sponsorblock_error_without_sponsorblock_requested_still_fails(store, settings):
    job = store.add(YT)
    ydl = _flaky(SB_DOWN)
    downloader.download(job.id, job.url, store, settings, ydl_class=ydl)
    assert store.get(job.id).status != DONE and len(ydl.opts_seen) == 1   # "timed out": auto-retry


def test_collect_sidecars_skips_one_that_cannot_move(settings, monkeypatch):
    tmp = settings.tmp_dir
    dest = settings.downloads_dir / "j1-My_Video.mp4"
    (tmp / "j1-My_Video.de.srt").write_text("de")
    (tmp / "j1-My_Video.en.srt").write_text("en")
    real_move = downloader.shutil.move

    def move(src, dst):
        if src.endswith(".de.srt"):
            raise PermissionError("denied")
        return real_move(src, dst)

    monkeypatch.setattr(downloader.shutil, "move", move)
    assert downloader._collect_sidecars(tmp, "j1", dest) == ["j1-My_Video.en.srt"]


def test_download_survives_a_sidecar_that_cannot_move(store, settings, monkeypatch):
    job = store.add(YT, subtitles="en", subtitles_mode="file")
    real_move = downloader.shutil.move

    def move(src, dst):
        if src.endswith(".srt"):
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(downloader.shutil, "move", move)
    downloader.download(job.id, job.url, store, settings, ydl_class=SidecarYDL)
    done = store.get(job.id)
    assert done.status == DONE and done.sidecars == []
    assert (settings.downloads_dir / done.filename).exists()


# -- round 10: metadata, custom file name, chapter split ------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("My Video", "My_Video"),
    ("Café déjà vu", "Cafe_deja_vu"),
    ("a/b\\c", "a_b_c"),
    ("..hidden", "hidden"),
    ("--dash", "dash"),
    ("  padded  ", "padded"),
    ("", None),
    ("   ", None),
    ("...", None),
    (None, None),
])
def test_safe_stem(name, expected):
    assert downloader.safe_stem(name) == expected


def test_safe_stem_never_keeps_separators_and_is_cut():
    assert "/" not in downloader.safe_stem("../../etc/passwd")
    assert not downloader.safe_stem("../../etc/passwd").startswith(".")
    assert len(downloader.safe_stem("x" * 400)) == 150
    assert downloader.safe_stem("日本語") is not None and downloader.safe_stem("日本語").isascii()


def test_metadata_fields_from_info():
    info = {"duration": 61.2, "uploader": "Alice", "channel": "Chan", "upload_date": "20260105",
            "description": "d" * 2500}
    assert downloader.DESCRIPTION_CHARS == 2000
    assert downloader.metadata_fields(info) == {
        "duration": 62, "uploader": "Alice", "upload_date": "2026-01-05", "description": "d" * 2000}


def test_metadata_fields_fallbacks_and_unknowns():
    assert downloader.metadata_fields({"channel": "Chan", "upload_date": "bad"}) == {
        "duration": None, "uploader": "Chan", "upload_date": None, "description": None}
    assert downloader.metadata_fields(None)["duration"] is None
    assert downloader.metadata_fields({"duration": 30, "is_live": True})["duration"] is None
    assert downloader.metadata_fields({"duration": 30, "live_status": "is_live"})["duration"] is None
    assert downloader.metadata_fields({"duration": 30})["duration"] == 30


def test_build_opts_custom_name_template(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, custom_name="holiday_2026")
    assert opts["outtmpl"] == str(settings.tmp_dir / "abc12345-holiday_2026.%(ext)s")


def test_build_opts_custom_name_escapes_percent(settings):
    # yt-dlp's sanitizer keeps "%"; the template must treat it literally.
    opts = downloader.build_opts(settings, "abc12345", _hook, custom_name="100%")
    assert opts["outtmpl"] == str(settings.tmp_dir / "abc12345-100%%.%(ext)s")


def test_build_opts_split_templates_and_postprocessor_last(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, subtitles="en", split=True)
    assert opts["outtmpl"] == {
        "default": str(settings.tmp_dir / "abc12345-%(title).80B.%(ext)s"),
        "chapter": str(settings.tmp_dir / "abc12345-chapter%(section_number)03d-%(section_title).80B.%(ext)s"),
    }
    pps = opts["postprocessors"]
    assert pps[-1] == {"key": "FFmpegSplitChapters", "force_keyframes": False, "when": "post_process"}
    keys = [pp["key"] for pp in pps]
    assert keys.index("FFmpegEmbedSubtitle") < keys.index("EmbedThumbnail") < keys.index("FFmpegSplitChapters")


def test_build_opts_split_last_even_with_sponsorblock(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, split=True, sponsorblock=True,
                                 url="https://www.youtube.com/watch?v=x")
    assert opts["postprocessors"][0]["key"] == "SponsorBlock"
    assert opts["postprocessors"][-1]["key"] == "FFmpegSplitChapters"


def test_build_opts_split_with_custom_name(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, split=True, custom_name="trip")
    assert opts["outtmpl"] == {
        "default": str(settings.tmp_dir / "abc12345-trip.%(ext)s"),
        "chapter": str(settings.tmp_dir / "abc12345-chapter%(section_number)03d-trip-%(section_title).60B.%(ext)s"),
    }


def test_build_opts_without_split_has_no_split_step(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook)
    assert isinstance(opts["outtmpl"], str)
    assert all(pp["key"] != "FFmpegSplitChapters" for pp in opts["postprocessors"])


META_INFO = {"title": "My Video", "duration": 99.5, "uploader": "Alice", "upload_date": "20260102",
             "description": "about"}


class MetaYDL(FakeYDL):
    def extract_info(self, url, download=True):
        info = super().extract_info(url, download)
        return {**info, **META_INFO}


class SplitYDL(FakeYDL):
    """Writes the whole file plus `-chapter001-`/`-chapter002-` parts (and a stray other ext)."""

    chapters = [{"start_time": 0, "end_time": 10.2, "title": "Intro"},
                {"start_time": 10.2, "end_time": 30, "title": ""}]
    write_parts = True
    on_finished = staticmethod(lambda: None)

    def extract_info(self, url, download=True):
        tmpl = self.opts["outtmpl"]
        hook = self.opts["progress_hooks"][0]
        info = {**META_INFO, "chapters": SplitYDL.chapters}
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "info_dict": info})
        whole = Path(tmpl["default"].replace("%(title).80B", "My_Video").replace("%(ext)s", "mp4"))
        whole.write_bytes(b"w" * 100)
        if SplitYDL.write_parts:
            for n, (title, size) in enumerate([("Intro", 7), ("", 13)], start=1):
                part = (tmpl["chapter"].replace("%(section_number)03d", f"{n:03d}")
                        .replace("%(section_title).80B", title or "NA").replace("%(ext)s", "mp4"))
                Path(part).write_bytes(b"p" * size)
            Path(tmpl["chapter"].replace("%(section_number)03d", "003")
                 .replace("%(section_title).80B", "x").replace("%(ext)s", "jpg")).write_bytes(b"j")
        hook({"status": "finished", "info_dict": info})
        SplitYDL.on_finished()
        return {**info, "requested_downloads": [{"filepath": str(whole)}]}


def _reset_split_ydl():
    SplitYDL.write_parts = True
    SplitYDL.on_finished = staticmethod(lambda: None)


def test_download_done_sets_metadata_and_finished_at(settings, store):
    job = store.add("https://example.com/v")
    assert downloader.download(job.id, job.url, store, settings, ydl_class=MetaYDL) == [job.id]
    job = store.get(job.id)
    assert job.status == DONE
    assert (job.duration, job.uploader, job.upload_date, job.description) == (100, "Alice", "2026-01-02", "about")
    assert datetime.fromisoformat(job.finished_at).tzinfo is not None


def test_download_failed_sets_finished_at(settings, store):
    job = store.add("https://example.com/v")
    assert downloader.download(job.id, job.url, store, settings, ydl_class=FailingYDL) == [job.id]
    job = store.get(job.id)
    assert job.status == FAILED and job.finished_at is not None


def test_transient_requeue_does_not_set_finished_at(settings, store):
    job = store.add("https://example.com/v")
    assert downloader.download(job.id, job.url, store, settings, ydl_class=TransientYDL) == [job.id]
    job = store.get(job.id)
    assert job.status == QUEUED and job.finished_at is None


def test_download_with_custom_name_uses_it_as_the_stem(settings, store):
    job = store.add("https://example.com/v", custom_name="holiday")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.last_opts["outtmpl"] == str(settings.tmp_dir / f"{job.id}-holiday.%(ext)s")


def test_download_cancelled_returns_empty(settings, store):
    job = store.add("https://example.com/v")
    CancelMidwayYDL.on_first_hook = staticmethod(lambda: store.remove(job.id))
    try:
        assert downloader.download(job.id, job.url, store, settings, ydl_class=CancelMidwayYDL) == []
    finally:
        CancelMidwayYDL.on_first_hook = staticmethod(lambda: None)
    assert downloader.download("missing", "https://example.com/v", store, settings, ydl_class=FakeYDL) == []


def test_split_download_creates_one_done_job_per_chapter(settings, store):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True, sponsorblock=True, compat=True, thumb="t" * 16)
    ended = downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL)

    assert store.get(job.id) is None
    assert len(ended) == 2
    parts = [store.get(i) for i in ended]
    assert [p.playlist_index for p in parts] == [1, 2]
    assert [p.title for p in parts] == ["Intro", "Part 2"]
    assert [p.size for p in parts] == [7, 13]
    assert [p.duration for p in parts] == [11, 20]
    for p in parts:
        assert p.status == DONE and p.progress == 100 and p.split is True
        assert p.playlist_id == f"split-{job.id}" and p.playlist == "My Video"
        assert p.url == job.url and p.created_at == job.created_at
        assert p.sponsorblock and p.compat and p.thumb == "t" * 16
        assert (p.uploader, p.upload_date, p.description) == ("Alice", "2026-01-02", "about")
        assert p.finished_at is not None
        assert (settings.downloads_dir / p.filename).is_file()
        assert p.filename.startswith(f"{job.id}-chapter00{p.playlist_index}-")
    assert sorted(x.name for x in settings.downloads_dir.iterdir()) == sorted(p.filename for p in parts)
    assert list(settings.tmp_dir.iterdir()) == []


def test_split_part_without_chapter_info_has_no_duration(settings, store):
    _reset_split_ydl()
    saved = SplitYDL.chapters
    SplitYDL.chapters = []
    try:
        job = store.add("https://example.com/v", split=True)
        ended = downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL)
    finally:
        SplitYDL.chapters = saved
    parts = [store.get(i) for i in ended]
    assert [p.duration for p in parts] == [None, None]
    assert [p.title for p in parts] == ["Part 1", "Part 2"]


def test_split_without_chapters_downloads_whole_with_note(settings, store):
    _reset_split_ydl()
    SplitYDL.write_parts = False
    try:
        job = store.add("https://example.com/v", split=True)
        assert downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL) == [job.id]
    finally:
        _reset_split_ydl()
    job = store.get(job.id)
    assert job.status == DONE and job.note == "No chapters found; downloaded whole"
    assert job.filename == f"{job.id}-My_Video.mp4" and job.size == 100
    assert [x.name for x in settings.downloads_dir.iterdir()] == [job.filename]


def test_split_parent_removed_before_parts_are_created(settings, store):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True)
    SplitYDL.on_finished = staticmethod(lambda: store.remove(job.id))
    try:
        assert downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL) == []
    finally:
        _reset_split_ydl()
    assert store.list() == []
    assert list(settings.downloads_dir.iterdir()) == []
    assert list(settings.tmp_dir.iterdir()) == []


# -- round 10 fix wave: split cancel window, orphan cleanup, ordering, notes -----------------

class _RemoveOnceStore:
    """Wraps a JobStore; `remove(parent)` first drops the parent and returns None, as if a
    cancel had removed it between add_many and the finisher's own remove."""

    def __init__(self, inner, parent_id):
        self._inner, self._parent_id = inner, parent_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def remove(self, job_id):
        if job_id == self._parent_id:
            self._inner.remove(job_id)
            return None
        return self._inner.remove(job_id)


def test_split_parent_cancelled_after_parts_created_leaves_nothing(settings, store):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True)
    wrapped = _RemoveOnceStore(store, job.id)
    assert downloader.download(job.id, job.url, wrapped, settings, ydl_class=SplitYDL) == []
    assert store.list() == []
    assert list(settings.downloads_dir.iterdir()) == []
    assert list(settings.tmp_dir.iterdir()) == []


def test_split_parent_cancelled_while_parts_move_leaves_nothing(settings, store, monkeypatch):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True)
    real_move = shutil.move
    calls = []

    def move(src, dst):
        calls.append(src)
        result = real_move(src, dst)
        if len(calls) == 2:
            store.remove(job.id)   # cancel lands after the last move, before add_many
        return result

    monkeypatch.setattr(downloader.shutil, "move", move)
    assert downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL) == []
    assert store.list() == []
    assert list(settings.downloads_dir.iterdir()) == []
    assert list(settings.tmp_dir.iterdir()) == []


def test_split_failing_move_deletes_parts_already_moved(settings, store, monkeypatch):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True)
    real_move = shutil.move
    calls = []

    def move(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(downloader.shutil, "move", move)
    assert downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL) == [job.id]
    job = store.get(job.id)
    assert job.status == FAILED and "disk full" in job.error
    assert [j.id for j in store.list()] == [job.id]
    assert list(settings.downloads_dir.iterdir()) == []


def test_split_failing_add_many_deletes_moved_parts(settings, store, monkeypatch):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True)

    def boom(items):
        raise OSError("store write failed")

    monkeypatch.setattr(store, "add_many", boom)
    downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL)
    assert store.get(job.id).status == FAILED
    assert list(settings.downloads_dir.iterdir()) == []


class _ChapterNamedSplitYDL(SplitYDL):
    """SplitYDL for a custom name: also fills the `.60B` chapter title field."""

    def extract_info(self, url, download=True):
        tmpl = self.opts["outtmpl"]
        self.opts["outtmpl"] = {k: v.replace("%(section_title).60B", "%(section_title).80B")
                                for k, v in tmpl.items()}
        return super().extract_info(url, download)


def test_split_whole_file_named_like_a_part_is_not_a_part(settings, store):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True, custom_name="chapter001-x")
    ended = downloader.download(job.id, job.url, store, settings, ydl_class=_ChapterNamedSplitYDL)
    assert len(ended) == 2
    parts = [store.get(i) for i in ended]
    assert [p.size for p in parts] == [7, 13]
    assert all(p.filename.startswith(f"{job.id}-chapter00{p.playlist_index}-chapter001-x-")
               for p in parts)
    assert sorted(x.name for x in settings.downloads_dir.iterdir()) == sorted(p.filename for p in parts)


def test_build_opts_audio_split_leaves_the_split_step_to_download(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, format="audio", split=True)
    assert set(opts["outtmpl"]) == {"default", "chapter"}
    assert all(pp["key"] != "FFmpegSplitChapters" for pp in opts["postprocessors"])


def test_build_opts_mp4_split_step_is_last(settings):
    opts = downloader.build_opts(settings, "abc12345", _hook, format="mp4", split=True)
    assert opts["postprocessors"][-1] == downloader.SPLIT_POSTPROCESSOR


def test_download_audio_split_adds_split_after_thumbnail_embed(settings, store):
    job = store.add("https://example.com/v", format="audio", split=True)
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert FakeYDL.added_pps == [("TolerantEmbedThumbnailPP", "post_process"),
                                 ("FFmpegSplitChaptersPP", "post_process")]
    assert all(pp["key"] != "FFmpegSplitChapters" for pp in FakeYDL.last_opts["postprocessors"])


def test_download_mp4_split_adds_no_extra_postprocessor(settings, store):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True)
    downloader.download(job.id, job.url, store, settings, ydl_class=SplitYDL)
    assert FakeYDL.added_pps == []
    assert FakeYDL.last_opts["postprocessors"][-1] == downloader.SPLIT_POSTPROCESSOR


class _SubsFailingSplitYDL(SplitYDL):
    """First attempt fails on the subtitles; the retry (without subtitles) splits."""

    def extract_info(self, url, download=True):
        if self.opts.get("writesubtitles") or self.opts.get("writeautomaticsub"):
            raise yt_dlp.utils.DownloadError(f"ERROR: {downloader.SUBTITLES_FAILURE} en")
        return super().extract_info(url, download)


def test_split_parts_carry_the_parent_note(settings, store):
    _reset_split_ydl()
    job = store.add("https://example.com/v", split=True, subtitles="en")
    ended = downloader.download(job.id, job.url, store, settings, ydl_class=_SubsFailingSplitYDL)
    assert len(ended) == 2
    assert [store.get(i).note for i in ended] == [downloader.SUBTITLES_NOTE] * 2


# -- round 11: hand-over to gallery-dl for image posts ------------------------------

class NoVideoYDL(FakeYDL):
    calls = 0

    def extract_info(self, url, download=True):
        NoVideoYDL.calls += 1
        raise yt_dlp.utils.DownloadError("ERROR: [twitter] 123: No video could be found in this tweet")


class ErrorYDL(FakeYDL):
    message = ""

    def extract_info(self, url, download=True):
        raise yt_dlp.utils.DownloadError(ErrorYDL.message)


image_prior: list = []


@pytest.fixture
def image_handover(monkeypatch):
    image_prior.clear()
    from app import images
    calls = []

    def fake_download(job_id, url, store, settings, prior_error=None):
        job = store.get(job_id)
        cookie_copies = list(settings.tmp_dir.glob("cookies-*"))
        calls.append({"job_id": job_id, "url": url, "format": job.format, "quality": job.quality,
                      "cookie_copies": cookie_copies})
        image_prior.append(prior_error)
        return [job_id]

    monkeypatch.setattr(images, "supports", lambda url: "x.com" in url)
    monkeypatch.setattr(images, "download", fake_download)
    return calls


def test_download_hands_over_to_images_on_a_supported_site(settings, store, image_handover):
    settings.cookies_file.write_text("# Netscape\nexample.com\tFALSE\t/\tFALSE\t0\tk\tv\n")
    NoVideoYDL.calls = 0
    job = store.add("https://x.com/a/status/123", format="mp4", quality="720")
    assert downloader.download(job.id, job.url, store, settings, ydl_class=NoVideoYDL) == [job.id]
    assert NoVideoYDL.calls == 1
    assert image_handover == [{"job_id": job.id, "url": job.url, "format": "images", "quality": "best",
                               "cookie_copies": []}]   # yt-dlp's cookie copy closed first
    assert store.get(job.id).format == "images"


@pytest.mark.parametrize("message", [
    "ERROR: [twitter] 123: Read timed out",                  # transient
    "ERROR: [twitter] 123: You need to log in to view this",  # login-walled
])
def test_download_does_not_hand_over_transient_or_login_errors(settings, store, image_handover, message):
    ErrorYDL.message = message
    job = store.add("https://x.com/a/status/123")
    downloader.download(job.id, job.url, store, settings, ydl_class=ErrorYDL)
    assert image_handover == [] and store.get(job.id).format == "mp4"


def test_download_does_not_hand_over_for_unsupported_sites(settings, store, image_handover):
    job = store.add("https://example.com/v")
    downloader.download(job.id, job.url, store, settings, ydl_class=NoVideoYDL)
    assert image_handover == [] and store.get(job.id).status == FAILED


def test_download_does_not_hand_over_on_a_retry_attempt(settings, store, image_handover):
    job = store.add("https://x.com/a/status/123")
    store.update(job.id, attempts=1)
    downloader.download(job.id, job.url, store, settings, ydl_class=NoVideoYDL)
    assert image_handover == [] and store.get(job.id).status == FAILED


def test_download_does_not_hand_over_when_yt_dlp_succeeds(settings, store, image_handover):
    job = store.add("https://x.com/a/status/123")
    downloader.download(job.id, job.url, store, settings, ydl_class=FakeYDL)
    assert image_handover == [] and store.get(job.id).status == DONE


def test_download_of_an_images_job_goes_straight_to_images(settings, store, image_handover):
    job = store.add("https://x.com/a/status/123", format="images")
    assert downloader.download(job.id, job.url, store, settings, ydl_class=NeverCalledYDL) == [job.id]
    assert len(image_handover) == 1


def test_job_logger_scrub_is_public(settings):
    logger = downloader.JobLogger(settings.tmp_dir, secrets=["hunter2"])
    assert logger.scrub(f"pw hunter2 in {settings.tmp_dir}/cookies-ab.txt") == "pw <redacted> in <tmp>/<cookies>"


def test_not_video_is_a_module_constant():
    for text in ("There is no video in this post", "No video could be found in this tweet",
                 "No video formats found!", "Media #2 is not a video", "Unsupported URL: https://x",
                 "No media found", "This post does not contain any video"):
        assert downloader.NOT_VIDEO.search(text), text
    assert not downloader.NOT_VIDEO.search("Postprocessing: Conversion failed!")


@pytest.mark.parametrize("message", [
    # These also occur for real videos (a deleted post, a format gone): yt-dlp's error stays.
    "ERROR: [twitter] 123: Requested format is not available. Use --list-formats",
    "ERROR: [tiktok] 123: Unable to extract webpage video data",
    "ERROR: [twitter] 123: Unable to download webpage: HTTP Error 404: Not Found",
])
def test_not_video_excludes_errors_real_videos_also_have(settings, store, image_handover, message):
    assert not downloader.NOT_VIDEO.search(message)
    ErrorYDL.message = message
    job = store.add("https://x.com/a/status/123")
    downloader.download(job.id, job.url, store, settings, ydl_class=ErrorYDL)
    failed = store.get(job.id)
    assert image_handover == [] and failed.status == FAILED
    assert failed.format != "images" and failed.error.startswith(message.removeprefix("ERROR: ")[:20])


@pytest.mark.parametrize("message", [
    "ERROR: Postprocessing: Conversion failed! No video",
    "ERROR: ffmpeg exited with code 1: No video stream",
    "ERROR: [Errno 28] No space left on device: Unable to extract",
    "ERROR: [twitter] 123: Something odd happened",      # not a "no video" error
])
def test_download_does_not_hand_over_other_errors(settings, store, image_handover, message):
    ErrorYDL.message = message
    job = store.add("https://x.com/a/status/123")
    downloader.download(job.id, job.url, store, settings, ydl_class=ErrorYDL)
    assert image_handover == [] and store.get(job.id).status == FAILED


def test_download_hands_over_an_instagram_no_video_error(settings, store, image_handover, monkeypatch):
    from app import images
    monkeypatch.setattr(images, "supports", lambda url: "instagram.com" in url)
    ErrorYDL.message = "ERROR: [Instagram] ABC: There is no video in this post"
    job = store.add("https://www.instagram.com/p/ABC/")
    assert downloader.download(job.id, job.url, store, settings, ydl_class=ErrorYDL) == [job.id]
    assert len(image_handover) == 1
    assert image_prior == ["[Instagram] ABC: There is no video in this post"]


def test_download_hand_over_resets_video_only_options(settings, store, image_handover):
    job = store.add("https://x.com/a/status/123", format="mp4", quality="720", subtitles="en",
                    subtitles_mode="file", clip_start=1.0, clip_end=5.0, clip_exact=True,
                    sponsorblock=True, compat=True, split=True)
    downloader.download(job.id, job.url, store, settings, ydl_class=NoVideoYDL)
    got = store.get(job.id)
    assert len(image_handover) == 1
    assert (got.format, got.quality, got.subtitles, got.subtitles_mode, got.clip_start, got.clip_end,
            got.clip_exact, got.sponsorblock, got.compat, got.split) == (
        "images", "best", None, "embed", None, None, False, False, False, False)


def test_download_does_not_hand_over_after_a_dropped_extra(settings, store, image_handover):
    class SubsThenNoVideo(FakeYDL):
        calls = 0

        def extract_info(self, url, download=True):
            SubsThenNoVideo.calls += 1
            if SubsThenNoVideo.calls == 1:
                raise yt_dlp.utils.DownloadError("ERROR: Unable to download video subtitles for 'en': HTTP Error 429")
            raise yt_dlp.utils.DownloadError("ERROR: [twitter] 123: No video could be found in this tweet")

    job = store.add("https://x.com/a/status/123", subtitles="en")
    downloader.download(job.id, job.url, store, settings, ydl_class=SubsThenNoVideo)
    assert SubsThenNoVideo.calls == 2
    assert image_handover == [] and store.get(job.id).status == FAILED
