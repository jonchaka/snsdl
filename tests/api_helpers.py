"""Helpers and constants shared by the tests/test_api_*.py files."""
from app import worker as worker_mod
from app.jobs import DONE, FAILED, Job


CSRF = {"X-Requested-With": "sns-downloader"}


def _done_job(client, settings, url, filename):
    job = client.post("/api/jobs", json={"url": url}).json()
    (settings.downloads_dir / filename).write_bytes(b"data")
    client.app.state.store.update(job["id"], status=DONE, progress=100, filename=filename)
    return job["id"]


PL = {"title": "PL", "total": 3, "entries": [
    {"url": "https://www.youtube.com/watch?v=a1", "title": "One"},
    {"url": "https://www.youtube.com/watch?v=b2", "title": "Two"},
    {"url": "https://www.youtube.com/watch?v=c3", "title": "Three"},
]}


def _failed_job(client, url="https://youtu.be/bad"):
    job = client.post("/api/jobs", json={"url": url}).json()
    client.app.state.store.update(job["id"], status=FAILED, error="boom", progress=12)
    return job["id"]


COOKIES = (".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tSECRET\n"
           "www.facebook.com\tFALSE\t/\tTRUE\t0\tc_user\tSECRET2\n")


LOW_DISK_DETAIL = "Only 600.0 MB free on the download disk; downloads need at least 1.0 GB"


def _make_disk_low(monkeypatch):
    import shutil
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10 * 1024 ** 3, 0, 600 * 1024 ** 2))
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: True)


def _thumb(settings, key):
    path = settings.thumbs_dir / f"{key}.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    return path


CHANNEL = "https://www.youtube.com/@chan/videos"


def _done_with_sidecars(client, settings, url="https://youtu.be/s1", stem="s1"):
    jid = client.post("/api/jobs", json={"url": url}).json()["id"]
    (settings.downloads_dir / f"{stem}.mp4").write_bytes(b"video")
    (settings.downloads_dir / f"{stem}.en.srt").write_bytes(b"en subs")
    client.app.state.store.update(jid, status=DONE, progress=100, filename=f"{stem}.mp4",
                                  sidecars=[f"{stem}.en.srt"])
    return jid


V = "https://youtu.be/r10"


def _history(client, *specs):
    jobs = [Job(id=f"h{i}", url=url, title=title, status=DONE, filename=f"h{i}.mp4",
                finished_at=f"2026-11-0{i + 1}T10:00:00+00:00")
            for i, (url, title) in enumerate(specs)]
    return client.app.state.history.record(jobs)
