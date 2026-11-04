import json

import pytest
from fastapi.testclient import TestClient

from app import probe as probe_mod
from app.jobs import DONE, DOWNLOADING, FAILED, QUEUED, Job
from app.main import create_app


CSRF = {"X-Requested-With": "sns-downloader"}


@pytest.fixture
def client(settings):
    app = create_app(settings, run_background=False)
    with TestClient(app) as c:
        yield c


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "SNS Downloader" in r.text
    assert 'id="options"' in r.text
    assert 'id="quality"' in r.text
    assert "Cancel" in r.text


def test_create_job_returns_201_and_enqueues(client):
    r = client.post("/api/jobs", json={"url": "https://www.tiktok.com/@u/video/1"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == QUEUED
    assert body["url"] == "https://www.tiktok.com/@u/video/1"
    assert client.app.state.pool.queue.qsize() == 1


def test_create_job_strips_whitespace(client):
    r = client.post("/api/jobs", json={"url": "  https://youtu.be/abc  "})
    assert r.status_code == 201
    assert r.json()["url"] == "https://youtu.be/abc"


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://x/y", "javascript:alert(1)", "https://", "file:///etc/passwd", "http://:80"])
def test_create_job_rejects_invalid_url(client, bad):
    r = client.post("/api/jobs", json={"url": bad})
    assert r.status_code == 422


def test_duplicate_active_url_returns_existing_job(client):
    first = client.post("/api/jobs", json={"url": "https://youtu.be/abc"}).json()
    second = client.post("/api/jobs", json={"url": "https://youtu.be/abc"})
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert client.app.state.pool.queue.qsize() == 1


def test_finished_url_can_be_requeued(client):
    store = client.app.state.store
    first = client.post("/api/jobs", json={"url": "https://youtu.be/abc"}).json()
    store.update(first["id"], status=DONE)
    second = client.post("/api/jobs", json={"url": "https://youtu.be/abc"})
    assert second.status_code == 201
    assert second.json()["id"] != first["id"]


def test_list_jobs_newest_first(client):
    a = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    b = client.post("/api/jobs", json={"url": "https://youtu.be/b"}).json()
    ids = [j["id"] for j in client.get("/api/jobs").json()]
    assert ids == [b["id"], a["id"]]


def test_get_file_serves_attachment(client, settings):
    (settings.downloads_dir / "abc-vid.mp4").write_bytes(b"\x00\x01")
    r = client.get("/files/abc-vid.mp4")
    assert r.status_code == 200
    assert r.content == b"\x00\x01"
    assert 'attachment' in r.headers["content-disposition"]
    assert 'abc-vid.mp4' in r.headers["content-disposition"]


def test_get_file_serves_name_with_double_dots(client, settings):
    (settings.downloads_dir / "abc-Wait..._what.mp4").write_bytes(b"dots")
    r = client.get("/files/abc-Wait..._what.mp4")
    assert r.status_code == 200
    assert r.content == b"dots"


@pytest.mark.parametrize("name", ["missing.mp4", "..%2F..%2Fetc%2Fpasswd", "a%2Fb", "a%5Cb", ".hidden", "a%00b"])
def test_get_file_rejects_missing_or_unsafe(client, name):
    r = client.get(f"/files/{name}")
    assert r.status_code == 404


def test_cleanup_requires_header(client, settings):
    (settings.downloads_dir / "x.mp4").write_bytes(b"x")
    assert client.post("/api/cleanup").status_code == 403
    assert (settings.downloads_dir / "x.mp4").exists()


def test_cleanup_endpoint(client, settings):
    (settings.downloads_dir / "x.mp4").write_bytes(b"x")
    client.post("/api/jobs", json={"url": "https://youtu.be/a"})
    r = client.post("/api/cleanup", headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"removed": 1}
    assert client.get("/api/jobs").json() == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_startup_recovers_interrupted_jobs(settings):
    settings.jobs_file.write_text(json.dumps([
        Job(id="dl000001", url="https://youtu.be/dl", status=DOWNLOADING, progress=40).to_dict(),
        Job(id="qq000001", url="https://youtu.be/q", status=QUEUED).to_dict(),
    ]))
    (settings.tmp_dir / "dl000001-partial.mp4.part").write_bytes(b"p")

    app = create_app(settings, run_background=False)
    with TestClient(app) as c:
        jobs = {j["id"]: j for j in c.get("/api/jobs").json()}
        assert jobs["dl000001"]["status"] == FAILED
        assert jobs["dl000001"]["error"] == "interrupted by restart"
        assert jobs["qq000001"]["status"] == QUEUED
        assert app.state.pool.queue.qsize() == 1
        assert list(settings.tmp_dir.iterdir()) == []


# -- format / quality on create -------------------------------------------

def test_create_job_defaults_format_and_quality(client):
    body = client.post("/api/jobs", json={"url": "https://youtu.be/abc"}).json()
    assert body["format"] == "mp4"
    assert body["quality"] == "best"


def test_create_job_accepts_format_and_quality(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "format": "mp3", "quality": "320"})
    assert r.status_code == 201
    assert r.json()["format"] == "mp3"
    assert r.json()["quality"] == "320"


@pytest.mark.parametrize("fmt,quality", [("mp3", "1080"), ("mp4", "abc"), ("wav", "best"), ("mp4", "0")])
def test_create_job_rejects_bad_format_quality(client, fmt, quality):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "format": fmt, "quality": quality})
    assert r.status_code == 422
    assert "not valid for format" in r.text or "format must be one of" in r.text


def test_duplicate_detection_includes_format_and_quality(client):
    url = "https://youtu.be/abc"
    a = client.post("/api/jobs", json={"url": url, "format": "mp4", "quality": "1080"})
    b = client.post("/api/jobs", json={"url": url, "format": "mp4", "quality": "720"})
    c = client.post("/api/jobs", json={"url": url, "format": "mp3", "quality": "best"})
    d = client.post("/api/jobs", json={"url": url, "format": "mp4", "quality": "1080"})
    assert (a.status_code, b.status_code, c.status_code, d.status_code) == (201, 201, 201, 200)
    assert d.json()["id"] == a.json()["id"]
    assert client.app.state.pool.queue.qsize() == 3


# -- probe -----------------------------------------------------------------

def test_probe_requires_header(client):
    assert client.post("/api/probe", json={"url": "https://youtu.be/abc"}).status_code == 403


def test_probe_rejects_invalid_url(client):
    assert client.post("/api/probe", json={"url": "not a url"}, headers=CSRF).status_code == 422


def test_probe_returns_title_and_heights(client, monkeypatch, settings):
    calls = []

    def fake_probe(url, s):
        calls.append((url, s))
        return {"title": "Vid", "heights": [1080, 720]}

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    r = client.post("/api/probe", json={"url": "  https://youtu.be/abc "}, headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"title": "Vid", "heights": [1080, 720]}
    assert calls == [("https://youtu.be/abc", settings)]


def test_probe_error_is_422_with_message(client, monkeypatch):
    def fake_probe(url, s):
        raise probe_mod.ProbeError("[youtube] abc: Private video")

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    r = client.post("/api/probe", json={"url": "https://youtu.be/abc"}, headers=CSRF)
    assert r.status_code == 422
    assert r.json() == {"detail": "[youtube] abc: Private video"}


# -- delete ----------------------------------------------------------------

def _done_job(client, settings, url, filename):
    job = client.post("/api/jobs", json={"url": url}).json()
    (settings.downloads_dir / filename).write_bytes(b"data")
    client.app.state.store.update(job["id"], status=DONE, progress=100, filename=filename)
    return job["id"]


def test_delete_requires_header(client, settings):
    jid = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    assert client.delete(f"/api/jobs/{jid}").status_code == 403
    assert (settings.downloads_dir / "a.mp4").exists()
    assert client.app.state.store.get(jid) is not None


def test_delete_unknown_job_is_404(client):
    assert client.delete("/api/jobs/nope", headers=CSRF).status_code == 404


@pytest.mark.parametrize("status", [QUEUED, DOWNLOADING])
def test_delete_active_job_cancels_it(client, status):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], status=status, progress=40)
    r = client.delete(f"/api/jobs/{job['id']}", headers=CSRF)
    assert r.status_code == 204
    assert client.app.state.store.get(job["id"]) is None
    assert client.get("/api/jobs").json() == []


def test_cancelled_url_can_be_queued_again(client):
    first = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.delete(f"/api/jobs/{first['id']}", headers=CSRF)
    second = client.post("/api/jobs", json={"url": "https://youtu.be/a"})
    assert second.status_code == 201
    assert second.json()["id"] != first["id"]


def test_delete_done_job_removes_record_and_file_only_for_that_job(client, settings):
    gone = _done_job(client, settings, "https://youtu.be/gone", "gone.mp4")
    keep = _done_job(client, settings, "https://youtu.be/keep", "keep.mp4")
    r = client.delete(f"/api/jobs/{gone}", headers=CSRF)
    assert r.status_code == 204
    assert r.content == b""
    ids = [j["id"] for j in client.get("/api/jobs").json()]
    assert ids == [keep]
    assert not (settings.downloads_dir / "gone.mp4").exists()
    assert (settings.downloads_dir / "keep.mp4").exists()


def test_delete_failed_job_without_file(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], status=FAILED, error="boom")
    assert client.delete(f"/api/jobs/{job['id']}", headers=CSRF).status_code == 204
    assert client.get("/api/jobs").json() == []


def test_delete_done_job_whose_file_is_already_gone(client, settings):
    jid = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    (settings.downloads_dir / "a.mp4").unlink()
    assert client.delete(f"/api/jobs/{jid}", headers=CSRF).status_code == 204
