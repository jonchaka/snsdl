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


def test_build_opts_uses_cookies_when_present(settings):
    settings.cookies_file.write_text("# Netscape HTTP Cookie File\n")
    opts = downloader.build_opts(settings, "abc12345", _hook)
    assert opts["cookiefile"] == str(settings.cookies_file)


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
