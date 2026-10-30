import json

import pytest
from fastapi.testclient import TestClient

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


@pytest.mark.parametrize("name", ["missing.mp4", "..%2F..%2Fetc%2Fpasswd", "a%2Fb", "a%5Cb", ".hidden"])
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
