import json

import pytest
from fastapi.testclient import TestClient

from app import downloader as downloader_mod
from app import probe as probe_mod
from app import thumbs as thumbs_mod
from app import worker as worker_mod
from app.jobs import DONE, DOWNLOADING, FAILED, PAUSED, QUEUED, Job
from app.main import create_app


CSRF = {"X-Requested-With": "sns-downloader"}


@pytest.fixture
def client(settings, monkeypatch):
    # The 507 guard reads the real disk; keep tests independent of the machine's free space.
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: False)
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
    assert 'id="plbox"' in r.text
    assert 'id="plcheck"' in r.text
    assert 'id="pllimit"' in r.text
    assert "/api/playlists" in r.text
    assert 'class="jobs"' in r.text   # downloads table (playlist groups render inside it)
    assert "plcancel" in r.text
    assert "/api/playlists/" in r.text
    assert "plremove" in r.text
    assert "status=finished" in r.text
    for call in ("actionButton('retry', 'retry', 'Retry'", "actionButton('pause', 'pause', 'Pause'",
                 "actionButton('resume', 'play', 'Resume'", "actionButton('addcookies', 'cookie', 'Add cookies'",
                 "actionButton('learnhow', 'help', 'Learn how'", "j.keep ? 'unkeep' : 'keep'",
                 "actionButton('remove', finished ? 'trash' : 'x', finished ? 'Remove' : 'Cancel'"):
        assert call in r.text, call
    assert "plretry" in r.text and "plpause" in r.text and "plresume" in r.text and "plzip" in r.text
    # row and playlist action buttons post to /api/jobs/<id>/<action> and /api/playlists/<id>/<action>
    assert "/api/jobs/${encodeURIComponent(actBtn.dataset.id)}/${action}" in r.text
    assert "/api/playlists/${encodeURIComponent(plAct.dataset.id)}/${action}" in r.text
    assert 'id="status"' in r.text and 'class="progress-text"' in r.text and 'class="fmt"' in r.text
    assert 'id="queuetoggle"' in r.text and 'id="queuebanner"' in r.text and "/api/queue/" in r.text
    assert 'id="update"' in r.text and "/api/update" in r.text


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
    assert r.json() == {"detail": "[youtube] abc: Private video", "needs_login": False}


def test_probe_login_wall_is_flagged_in_the_422_body(client, monkeypatch):
    def fake_probe(url, s):
        raise probe_mod.ProbeError("[instagram] abc: You need to log in to access this content", needs_login=True)

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    r = client.post("/api/probe", json={"url": "https://www.instagram.com/p/abc/"}, headers=CSRF)
    assert r.status_code == 422
    assert r.json() == {"detail": "[instagram] abc: You need to log in to access this content", "needs_login": True}


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


def test_delete_unlinks_file_recorded_at_removal_time(client, settings):
    """Simulates the download finishing between the handler's lookup and the remove:
    the file named on the removed record must be deleted, not left orphaned."""
    store = client.app.state.store
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    store.update(job["id"], status=DOWNLOADING)
    (settings.downloads_dir / "late.mp4").write_bytes(b"x")
    real_remove = store.remove

    def finish_then_remove(job_id):
        store.update(job_id, status=DONE, progress=100, filename="late.mp4")
        return real_remove(job_id)

    store.remove = finish_then_remove
    assert client.delete(f"/api/jobs/{job['id']}", headers=CSRF).status_code == 204
    assert not (settings.downloads_dir / "late.mp4").exists()
    assert store.get(job["id"]) is None


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


# -- playlists -------------------------------------------------------------

PL = {"title": "PL", "total": 3, "entries": [
    {"url": "https://www.youtube.com/watch?v=a1", "title": "One"},
    {"url": "https://www.youtube.com/watch?v=b2", "title": "Two"},
    {"url": "https://www.youtube.com/watch?v=c3", "title": "Three"},
]}


def _fake_expand(monkeypatch, result=PL, calls=None):
    def fake(url, s):
        if calls is not None:
            calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(probe_mod, "expand_playlist", fake)


def test_playlists_requires_header(client, monkeypatch):
    _fake_expand(monkeypatch)
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 3})
    assert r.status_code == 403
    assert client.get("/api/jobs").json() == []


def test_playlists_creates_one_job_per_entry(client, monkeypatch):
    calls = []
    _fake_expand(monkeypatch, calls=calls)
    r = client.post("/api/playlists", json={"url": " https://x/p ", "format": "mp3", "quality": "320", "limit": 10}, headers=CSRF)
    assert r.status_code == 201
    body = r.json()
    assert calls == ["https://x/p"]
    assert (body["playlist"], body["total"], body["queued"], body["reused"]) == ("PL", 3, 3, 0)
    assert [j["url"] for j in body["jobs"]] == [e["url"] for e in PL["entries"]]
    assert [j["playlist_index"] for j in body["jobs"]] == [1, 2, 3]
    assert all(j["playlist"] == "PL" and j["format"] == "mp3" and j["quality"] == "320" for j in body["jobs"])
    assert [j["title"] for j in body["jobs"]] == ["One", "Two", "Three"]
    assert client.app.state.pool.queue.qsize() == 3
    assert len(client.get("/api/jobs").json()) == 3


def test_playlists_limit_truncates(client, monkeypatch):
    _fake_expand(monkeypatch)
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 2}, headers=CSRF)
    assert r.status_code == 201
    assert r.json()["queued"] == 2
    assert [j["playlist_index"] for j in r.json()["jobs"]] == [1, 2]
    assert client.app.state.pool.queue.qsize() == 2


def test_playlists_limit_capped_by_settings(client, monkeypatch, settings):
    _fake_expand(monkeypatch)
    settings.max_playlist_items = 1
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 3}, headers=CSRF)
    assert r.json()["queued"] == 1


def test_playlists_reuses_active_duplicate(client, monkeypatch):
    _fake_expand(monkeypatch)
    existing = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=b2"}).json()
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 3}, headers=CSRF)
    body = r.json()
    assert (body["queued"], body["reused"]) == (2, 1)
    assert body["jobs"][1]["id"] == existing["id"]
    assert body["jobs"][1]["playlist"] is None          # reused job keeps its own record
    assert client.app.state.pool.queue.qsize() == 3     # 1 from the single post + 2 new


def test_playlists_not_a_playlist_is_422(client, monkeypatch):
    _fake_expand(monkeypatch, result=probe_mod.ProbeError("URL is not a playlist"))
    r = client.post("/api/playlists", json={"url": "https://x/v", "limit": 5}, headers=CSRF)
    assert r.status_code == 422
    assert r.json() == {"detail": "URL is not a playlist", "needs_login": False}
    assert client.get("/api/jobs").json() == []


@pytest.mark.parametrize("body", [
    {"url": "https://x/p"},                                  # limit missing
    {"url": "https://x/p", "limit": 0},
    {"url": "https://x/p", "limit": 2, "format": "mp3", "quality": "1080"},
    {"url": "not a url", "limit": 2},
])
def test_playlists_rejects_bad_body(client, monkeypatch, body):
    _fake_expand(monkeypatch)
    assert client.post("/api/playlists", json=body, headers=CSRF).status_code == 422


def test_probe_response_passes_playlist_through(client, monkeypatch):
    monkeypatch.setattr(probe_mod, "probe", lambda url, s: {"title": "V", "heights": [720], "playlist": {"title": "PL", "count": 2, "total": 3}})
    r = client.post("/api/probe", json={"url": "https://x/p"}, headers=CSRF)
    assert r.json()["playlist"] == {"title": "PL", "count": 2, "total": 3}


def test_playlists_skips_duplicate_url_within_playlist(client, monkeypatch):
    dup = {"title": "PL", "total": 2, "entries": [
        {"url": "https://www.youtube.com/watch?v=a1", "title": "One"},
        {"url": "https://www.youtube.com/watch?v=a1", "title": "One again"},
    ]}
    _fake_expand(monkeypatch, result=dup)
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 5}, headers=CSRF)
    body = r.json()
    assert (body["queued"], body["reused"]) == (1, 0)
    assert len(body["jobs"]) == 1
    assert client.app.state.pool.queue.qsize() == 1
    assert len(client.get("/api/jobs").json()) == 1


# -- cancel remaining playlist jobs ------------------------------------------

def _expand_playlist(client, monkeypatch, result, limit=10):
    _fake_expand(monkeypatch, result=result)
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": limit}, headers=CSRF)
    assert r.status_code == 201
    return r.json()


def test_playlists_sets_one_playlist_id_on_new_jobs_only(client, monkeypatch):
    reused = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=b2"}).json()
    body = _expand_playlist(client, monkeypatch, PL)
    ids = {j["playlist_id"] for j in body["jobs"] if j["id"] != reused["id"]}
    assert len(ids) == 1
    pid = ids.pop()
    assert isinstance(pid, str) and len(pid) == 8
    assert [j for j in body["jobs"] if j["id"] == reused["id"]][0]["playlist_id"] is None
    assert all(j["playlist_id"] == pid for j in client.get("/api/jobs").json() if j["id"] != reused["id"])
    other = _expand_playlist(client, monkeypatch, {"title": "Q", "total": 1, "entries": [
        {"url": "https://www.youtube.com/watch?v=z9", "title": "Z"}]})
    assert other["jobs"][0]["playlist_id"] != pid


def test_cancel_playlist_requires_header(client, monkeypatch):
    body = _expand_playlist(client, monkeypatch, PL)
    pid = body["jobs"][0]["playlist_id"]
    assert client.delete(f"/api/playlists/{pid}").status_code == 403
    assert len(client.get("/api/jobs").json()) == 3


def test_cancel_playlist_unknown_id_is_404(client):
    assert client.delete("/api/playlists/nope1234", headers=CSRF).status_code == 404


def test_cancel_playlist_removes_only_active_jobs_of_that_playlist(client, monkeypatch, settings):
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    j1, j2, j3 = body["jobs"]
    pid = j1["playlist_id"]
    (settings.downloads_dir / "one.mp4").write_bytes(b"x")
    store.update(j1["id"], status=DONE, progress=100, filename="one.mp4")
    store.update(j2["id"], status=DOWNLOADING, progress=30)
    # j3 stays queued
    other = _expand_playlist(client, monkeypatch, {"title": "Q", "total": 1, "entries": [
        {"url": "https://www.youtube.com/watch?v=z9", "title": "Z"}]})
    single = client.post("/api/jobs", json={"url": "https://youtu.be/solo"}).json()

    r = client.delete(f"/api/playlists/{pid}", headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"cancelled": 2}
    remaining = {j["id"] for j in client.get("/api/jobs").json()}
    assert remaining == {j1["id"], other["jobs"][0]["id"], single["id"]}
    assert (settings.downloads_dir / "one.mp4").exists()

    again = client.delete(f"/api/playlists/{pid}", headers=CSRF)
    assert again.status_code == 200
    assert again.json() == {"cancelled": 0}


def test_cancel_playlist_keeps_failed_rows(client, monkeypatch):
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    pid = body["jobs"][0]["playlist_id"]
    store.update(body["jobs"][0]["id"], status=FAILED, error="boom")
    r = client.delete(f"/api/playlists/{pid}", headers=CSRF)
    assert r.json() == {"cancelled": 2}
    assert [j["id"] for j in client.get("/api/jobs").json()] == [body["jobs"][0]["id"]]


def test_cancel_playlist_filters_through_remove_where(client, monkeypatch):
    """The active-status check must run inside the store lock, i.e. via remove_where's predicate,
    not on a snapshot taken before the removal."""
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    pid = body["jobs"][0]["playlist_id"]
    done = store.get(body["jobs"][0]["id"])
    active = store.get(body["jobs"][1]["id"])
    store.update(done.id, status=DONE)
    other = store.add("https://youtu.be/other")
    calls = []

    def spy(predicate):
        calls.append(predicate)
        return []

    monkeypatch.setattr(store, "remove_where", spy)
    assert client.delete(f"/api/playlists/{pid}", headers=CSRF).json() == {"cancelled": 0}
    assert len(calls) == 1
    pred = calls[0]
    assert pred(active) is True
    assert pred(done) is False
    assert pred(other) is False


def test_cancel_playlist_keeps_a_job_that_finished_after_listing(client, monkeypatch, settings):
    """A job that finishes between the handler's listing and the removal must keep its row."""
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    pid = body["jobs"][0]["playlist_id"]
    late = body["jobs"][2]["id"]
    real_list = store.list

    def list_then_finish():
        snapshot = real_list()
        (settings.downloads_dir / "late.mp4").write_bytes(b"x")
        store.update(late, status=DONE, progress=100, filename="late.mp4")
        return snapshot

    store.list = list_then_finish
    r = client.delete(f"/api/playlists/{pid}", headers=CSRF)
    assert r.json() == {"cancelled": 2}
    assert [j.id for j in real_list()] == [late]
    assert (settings.downloads_dir / "late.mp4").exists()


# -- remove finished playlist rows -------------------------------------------

def _mixed_playlist(client, monkeypatch, settings):
    """Expand PL and leave: j1 done (with file), j2 failed, j3 queued. Returns (pid, j1, j2, j3)."""
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    j1, j2, j3 = body["jobs"]
    (settings.downloads_dir / "one.mp4").write_bytes(b"x")
    store.update(j1["id"], status=DONE, progress=100, filename="one.mp4")
    store.update(j2["id"], status=FAILED, error="boom")
    return j1["playlist_id"], j1, j2, j3


def test_remove_finished_removes_done_and_failed_rows_and_files(client, monkeypatch, settings):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)
    other = _expand_playlist(client, monkeypatch, {"title": "Q", "total": 1, "entries": [
        {"url": "https://www.youtube.com/watch?v=z9", "title": "Z"}]})
    (settings.downloads_dir / "other.mp4").write_bytes(b"y")
    client.app.state.store.update(other["jobs"][0]["id"], status=DONE, filename="other.mp4")
    single = client.post("/api/jobs", json={"url": "https://youtu.be/solo"}).json()

    r = client.delete(f"/api/playlists/{pid}?status=finished", headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"removed": 2}
    remaining = {j["id"] for j in client.get("/api/jobs").json()}
    assert remaining == {j3["id"], other["jobs"][0]["id"], single["id"]}
    assert not (settings.downloads_dir / "one.mp4").exists()
    assert (settings.downloads_dir / "other.mp4").exists()

    again = client.delete(f"/api/playlists/{pid}?status=finished", headers=CSRF)
    assert again.json() == {"removed": 0}
    assert client.app.state.store.get(j3["id"]).status == QUEUED


@pytest.mark.parametrize("suffix", ["", "?status=active"])
def test_cancel_default_still_only_cancels_active(client, monkeypatch, settings, suffix):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)
    r = client.delete(f"/api/playlists/{pid}{suffix}", headers=CSRF)
    assert r.json() == {"cancelled": 1}
    assert {j["id"] for j in client.get("/api/jobs").json()} == {j1["id"], j2["id"]}
    assert (settings.downloads_dir / "one.mp4").exists()


def test_remove_finished_rejects_unknown_status(client, monkeypatch, settings):
    pid, *_ = _mixed_playlist(client, monkeypatch, settings)
    assert client.delete(f"/api/playlists/{pid}?status=bogus", headers=CSRF).status_code == 422
    assert len(client.get("/api/jobs").json()) == 3


def test_remove_finished_requires_header_and_known_id(client, monkeypatch, settings):
    pid, *_ = _mixed_playlist(client, monkeypatch, settings)
    assert client.delete(f"/api/playlists/{pid}?status=finished").status_code == 403
    assert len(client.get("/api/jobs").json()) == 3
    assert client.delete("/api/playlists/nope1234?status=finished", headers=CSRF).status_code == 404


# -- retry -------------------------------------------------------------------

def _failed_job(client, url="https://youtu.be/bad"):
    job = client.post("/api/jobs", json={"url": url}).json()
    client.app.state.store.update(job["id"], status=FAILED, error="boom", progress=12)
    return job["id"]


def test_retry_requires_header(client):
    jid = _failed_job(client)
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 403
    assert client.app.state.store.get(jid).status == FAILED


def test_retry_unknown_job_is_404(client):
    assert client.post("/api/jobs/nope1234/retry", headers=CSRF).status_code == 404


@pytest.mark.parametrize("status", [QUEUED, DOWNLOADING, DONE])
def test_retry_non_failed_job_is_409(client, status):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], status=status)
    before = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/jobs/{job['id']}/retry", headers=CSRF)
    assert r.status_code == 409
    assert r.json() == {"detail": "Only failed jobs can be retried"}
    assert client.app.state.pool.queue.qsize() == before


def test_retry_failed_job_resets_and_enqueues(client):
    jid = _failed_job(client)
    before = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/jobs/{jid}/retry", headers=CSRF)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == jid
    assert (body["status"], body["progress"], body["error"], body["filename"]) == (QUEUED, 0, None, None)
    assert client.app.state.pool.queue.qsize() == before + 1
    assert client.app.state.store.get(jid).status == QUEUED


def _playlist_with_failures(client, monkeypatch, settings):
    """PL: j1 failed, j2 done (file), j3 failed. Other playlist Q: one failed. Returns (pid, j1, j2, j3, other_id)."""
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    j1, j2, j3 = body["jobs"]
    (settings.downloads_dir / "two.mp4").write_bytes(b"x")
    store.update(j1["id"], status=FAILED, error="e1")
    store.update(j2["id"], status=DONE, progress=100, filename="two.mp4")
    store.update(j3["id"], status=FAILED, error="e3")
    other = _expand_playlist(client, monkeypatch, {"title": "Q", "total": 1, "entries": [
        {"url": "https://www.youtube.com/watch?v=z9", "title": "Z"}]})
    store.update(other["jobs"][0]["id"], status=FAILED, error="eo")
    return j1["playlist_id"], j1, j2, j3, other["jobs"][0]["id"]


def test_retry_playlist_requires_header_and_known_id(client, monkeypatch, settings):
    pid, *_ = _playlist_with_failures(client, monkeypatch, settings)
    assert client.post(f"/api/playlists/{pid}/retry").status_code == 403
    assert client.post("/api/playlists/nope1234/retry", headers=CSRF).status_code == 404


def test_retry_playlist_requeues_only_its_failed_rows(client, monkeypatch, settings):
    store = client.app.state.store
    pid, j1, j2, j3, other_id = _playlist_with_failures(client, monkeypatch, settings)
    before = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/playlists/{pid}/retry", headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"retried": 2}
    assert client.app.state.pool.queue.qsize() == before + 2
    assert store.get(j1["id"]).status == QUEUED and store.get(j1["id"]).error is None
    assert store.get(j3["id"]).status == QUEUED
    assert store.get(j1["id"]).playlist_index == 1 and store.get(j3["id"]).playlist_index == 3
    assert store.get(j2["id"]).status == DONE and (settings.downloads_dir / "two.mp4").exists()
    assert store.get(other_id).status == FAILED
    again = client.post(f"/api/playlists/{pid}/retry", headers=CSRF)
    assert again.json() == {"retried": 0}
    assert client.app.state.pool.queue.qsize() == before + 2


# -- pause / resume ------------------------------------------------------------

def test_pause_and_resume_require_header(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    assert client.post(f"/api/jobs/{job['id']}/pause").status_code == 403
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 403
    assert client.app.state.store.get(job["id"]).status == QUEUED


def test_pause_and_resume_unknown_job_is_404(client):
    assert client.post("/api/jobs/nope1234/pause", headers=CSRF).status_code == 404
    assert client.post("/api/jobs/nope1234/resume", headers=CSRF).status_code == 404


def test_pause_queued_job_then_resume_requeues_it(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    q0 = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/jobs/{job['id']}/pause", headers=CSRF)
    assert r.status_code == 200 and r.json()["status"] == PAUSED
    assert client.app.state.pool.queue.qsize() == q0          # pause does not touch the queue
    r = client.post(f"/api/jobs/{job['id']}/resume", headers=CSRF)
    assert r.status_code == 200 and r.json()["status"] == QUEUED
    assert client.app.state.pool.queue.qsize() == q0 + 1      # resume enqueues again


def test_pause_clears_priority_and_now(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], priority=True, now=True)
    r = client.post(f"/api/jobs/{job['id']}/pause", headers=CSRF)
    assert r.status_code == 200 and r.json()["priority"] is False and r.json()["now"] is False


@pytest.mark.parametrize("status", [PAUSED, DOWNLOADING, DONE, FAILED])
def test_pause_non_queued_job_is_409(client, status):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], status=status)
    r = client.post(f"/api/jobs/{job['id']}/pause", headers=CSRF)
    assert r.status_code == 409 and r.json() == {"detail": "Only queued jobs can be paused"}
    assert client.app.state.store.get(job["id"]).status == status


@pytest.mark.parametrize("status", [QUEUED, DOWNLOADING, DONE, FAILED])
def test_resume_non_paused_job_is_409(client, status):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], status=status)
    q0 = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/jobs/{job['id']}/resume", headers=CSRF)
    assert r.status_code == 409 and r.json() == {"detail": "Only paused jobs can be resumed"}
    assert client.app.state.pool.queue.qsize() == q0
    assert client.app.state.store.get(job["id"]).status == status


def test_duplicate_submit_of_paused_url_reuses_it(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.post(f"/api/jobs/{job['id']}/pause", headers=CSRF)
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a"})
    assert r.status_code == 200 and r.json()["id"] == job["id"] and r.json()["status"] == PAUSED


def test_playlist_pause_and_resume(client, monkeypatch, settings):
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    j1, j2, j3 = body["jobs"]
    pid = j1["playlist_id"]
    store.update(j2["id"], status=DOWNLOADING)
    other = _expand_playlist(client, monkeypatch, {"title": "Q", "total": 1, "entries": [
        {"url": "https://www.youtube.com/watch?v=z9", "title": "Z"}]})
    assert client.post(f"/api/playlists/{pid}/pause").status_code == 403
    assert client.post("/api/playlists/nope1234/pause", headers=CSRF).status_code == 404
    assert client.post("/api/playlists/nope1234/resume", headers=CSRF).status_code == 404

    q0 = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/playlists/{pid}/pause", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"paused": 2}
    assert store.get(j1["id"]).status == PAUSED and store.get(j3["id"]).status == PAUSED
    assert store.get(j2["id"]).status == DOWNLOADING
    assert store.get(other["jobs"][0]["id"]).status == QUEUED
    assert client.app.state.pool.queue.qsize() == q0

    r = client.post(f"/api/playlists/{pid}/resume", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"resumed": 2}
    assert store.get(j1["id"]).status == QUEUED and store.get(j3["id"]).status == QUEUED
    assert client.app.state.pool.queue.qsize() == q0 + 2
    assert client.post(f"/api/playlists/{pid}/resume", headers=CSRF).json() == {"resumed": 0}


def test_cancel_remaining_also_cancels_paused_rows(client, monkeypatch, settings):
    store = client.app.state.store
    body = _expand_playlist(client, monkeypatch, PL)
    pid = body["jobs"][0]["playlist_id"]
    store.update(body["jobs"][0]["id"], status=PAUSED)
    store.update(body["jobs"][1]["id"], status=DONE, filename=None)
    r = client.delete(f"/api/playlists/{pid}", headers=CSRF)
    assert r.json() == {"cancelled": 2}
    assert [j["id"] for j in client.get("/api/jobs").json()] == [body["jobs"][1]["id"]]


# -- status --------------------------------------------------------------------

def test_status_reports_downloads_and_yt_dlp_version(client, settings):
    import yt_dlp
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    body.pop("disk"), body.pop("proxy")   # covered by test_status_reports_disk_and_proxy
    body.pop("pacing"), body.pop("keep_dir")   # covered by test_status_reports_pacing_and_keep_dir
    body.pop("https"), body.pop("cert_hosts")   # covered by test_status_https_fields_*
    assert body == {"yt_dlp_version": yt_dlp.version.__version__, "downloads": {"files": 0, "bytes": 0}, "queue_paused": False, "rate_limit": None,
                        "impersonate": {"target": "chrome", "active": downloader_mod.target_supported("chrome")},
                        "update": {"installed": None, "restart_pending": False, "last_checked": None, "error": None, "note": None}}
    (settings.downloads_dir / "a.mp4").write_bytes(b"x" * 10)
    (settings.downloads_dir / "b.mp3").write_bytes(b"y" * 5)
    (settings.downloads_dir / "sub").mkdir()
    assert client.get("/api/status").json()["downloads"] == {"files": 2, "bytes": 15}


def test_status_tolerates_files_vanishing_mid_scan(client, settings, monkeypatch):
    import os
    from app import api as api_mod
    (settings.downloads_dir / "a.mp4").write_bytes(b"x" * 10)
    (settings.downloads_dir / "b.mp4").write_bytes(b"y" * 5)
    real_scandir = os.scandir

    class VanishingEntry:
        def __init__(self, e): self._e = e
        def is_file(self, follow_symlinks=False): return True
        def stat(self, follow_symlinks=False):
            if self._e.name == "b.mp4":
                raise FileNotFoundError
            return self._e.stat(follow_symlinks=follow_symlinks)

    class Scan:
        def __init__(self, path): self._it = real_scandir(path)
        def __enter__(self): return (VanishingEntry(e) for e in self._it)
        def __exit__(self, *a): self._it.close()

    monkeypatch.setattr(api_mod.os, "scandir", Scan)
    assert client.get("/api/status").json()["downloads"] == {"files": 1, "bytes": 10}
    assert api_mod.downloads_usage(settings.downloads_dir / "missing") == {"files": 0, "bytes": 0}


# -- global queue pause --------------------------------------------------------

def test_queue_pause_requires_header(client):
    assert client.post("/api/queue/pause").status_code == 403
    assert client.post("/api/queue/resume").status_code == 403
    assert client.get("/api/status").json()["queue_paused"] is False


def test_queue_pause_and_resume_toggle_gate_and_persist(client, settings):
    pool = client.app.state.pool
    assert pool.paused is False
    r = client.post("/api/queue/pause", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"queue_paused": True}
    assert pool.paused is True
    assert client.get("/api/status").json()["queue_paused"] is True
    assert json.loads(settings.state_file.read_text())["queue_paused"] is True
    assert client.post("/api/queue/pause", headers=CSRF).json() == {"queue_paused": True}   # idempotent
    r = client.post("/api/queue/resume", headers=CSRF)
    assert r.json() == {"queue_paused": False}
    assert pool.paused is False
    assert json.loads(settings.state_file.read_text())["queue_paused"] is False


def test_persisted_pause_is_honoured_on_startup(settings):
    settings.state_file.write_text(json.dumps({"queue_paused": True}))
    app = create_app(settings, run_background=False)
    with TestClient(app) as c:
        assert app.state.pool.paused is True
        assert c.get("/api/status").json()["queue_paused"] is True


# -- yt-dlp update -------------------------------------------------------------

class FakeUpdater:
    def __init__(self, pending=False):
        from app.updater import UpdateStatus
        self.status = UpdateStatus(running="1.0", installed=None, restart_pending=False,
                                   last_checked=None, error=None, note=None)
        self.pending = pending
        self.update_calls = 0
        self.restart_calls = 0

    def update(self):
        self.update_calls += 1
        self.status.installed = "2.0" if self.pending else "1.0"
        self.status.restart_pending = self.pending
        self.status.last_checked = "2026-11-04T04:00:00+00:00"
        return self.status

    def schedule_restart(self):
        if self.status.restart_pending:
            self.restart_calls += 1


def test_update_requires_header(client):
    client.app.state.updater = FakeUpdater()
    assert client.post("/api/update").status_code == 403
    assert client.app.state.updater.update_calls == 0


def test_update_runs_updater_and_reports(client):
    fake = FakeUpdater(pending=True)
    client.app.state.updater = fake
    r = client.post("/api/update", headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"running": "1.0", "installed": "2.0", "restart_pending": True,
                        "last_checked": "2026-11-04T04:00:00+00:00", "error": None, "note": None}
    assert fake.update_calls == 1 and fake.restart_calls == 1


def test_update_not_pending_does_not_schedule_restart(client):
    fake = FakeUpdater(pending=False)
    client.app.state.updater = fake
    assert client.post("/api/update", headers=CSRF).json()["restart_pending"] is False
    assert fake.restart_calls == 0


def test_status_includes_update_block(client):
    body = client.get("/api/status").json()
    import yt_dlp
    assert body["update"] == {"installed": None, "restart_pending": False, "last_checked": None, "error": None, "note": None}
    assert body["yt_dlp_version"] == yt_dlp.version.__version__


def test_status_reports_rate_limit(client, settings):
    assert client.get("/api/status").json()["rate_limit"] is None
    settings.rate_limit = 1024
    assert client.get("/api/status").json()["rate_limit"] == 1024


# -- keep -------------------------------------------------------------------------

def test_keep_and_unkeep_toggle_flag_on_done_job(client, settings):
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    r = client.post(f"/api/jobs/{jid}/keep", headers=CSRF)
    assert r.status_code == 200 and r.json()["keep"] is True
    assert client.get("/api/jobs").json()[0]["keep"] is True
    r = client.post(f"/api/jobs/{jid}/unkeep", headers=CSRF)
    assert r.status_code == 200 and r.json()["keep"] is False


@pytest.mark.parametrize("action", ["keep", "unkeep"])
def test_keep_requires_csrf_header_and_known_done_job(client, settings, action):
    jid = client.post("/api/jobs", json={"url": "https://youtu.be/q"}).json()["id"]   # queued
    assert client.post(f"/api/jobs/{jid}/{action}").status_code == 403
    assert client.post(f"/api/jobs/nope/{action}", headers=CSRF).status_code == 404
    assert client.post(f"/api/jobs/{jid}/{action}", headers=CSRF).status_code == 409
    client.app.state.store.update(jid, status=FAILED, error="x")
    assert client.post(f"/api/jobs/{jid}/{action}", headers=CSRF).status_code == 409


def test_cleanup_endpoint_spares_kept_jobs(client, settings):
    kid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    _done_job(client, settings, "https://youtu.be/k2", "k2.mp4")
    client.post(f"/api/jobs/{kid}/keep", headers=CSRF)
    r = client.post("/api/cleanup", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"removed": 1}
    assert [j["id"] for j in client.get("/api/jobs").json()] == [kid]
    assert [p.name for p in settings.downloads_dir.iterdir()] == ["k1.mp4"]


def test_remove_finished_skips_kept_rows(client, monkeypatch, settings):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)
    client.post(f"/api/jobs/{j1['id']}/keep", headers=CSRF)
    r = client.delete(f"/api/playlists/{pid}?status=finished", headers=CSRF)
    assert r.json() == {"removed": 1}                           # only the failed row
    assert {j["id"] for j in client.get("/api/jobs").json()} == {j1["id"], j3["id"]}
    assert (settings.downloads_dir / "one.mp4").exists()


def test_delete_job_removes_a_kept_job_and_its_file(client, settings):
    kid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    client.post(f"/api/jobs/{kid}/keep", headers=CSRF)
    assert client.delete(f"/api/jobs/{kid}", headers=CSRF).status_code == 204
    assert client.get("/api/jobs").json() == []
    assert not (settings.downloads_dir / "k1.mp4").exists()


def test_keep_returns_404_if_job_vanishes_between_lookup_and_update(client, settings, monkeypatch):
    kid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    store = client.app.state.store
    real_get = store.get

    def get_then_clear(job_id):
        job = real_get(job_id)
        store.clear()
        return job

    monkeypatch.setattr(store, "get", get_then_clear)
    assert client.post(f"/api/jobs/{kid}/keep", headers=CSRF).status_code == 404


# -- audio (original) ---------------------------------------------------------------

def test_create_audio_job(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "format": "audio", "quality": "best"})
    assert r.status_code == 201
    assert r.json()["format"] == "audio" and r.json()["quality"] == "best"


@pytest.mark.parametrize("quality", ["320", "1080", "original"])
def test_create_audio_job_rejects_other_qualities(client, quality):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "format": "audio", "quality": quality})
    assert r.status_code == 422


# -- subtitles -----------------------------------------------------------------

def test_create_job_with_subtitles(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "format": "mp4", "quality": "1080", "subtitles": "auto:en-orig"})
    assert r.status_code == 201 and r.json()["subtitles"] == "auto:en-orig"
    assert client.app.state.store.get(r.json()["id"]).subtitles == "auto:en-orig"


def test_same_video_with_and_without_subtitles_are_separate_jobs(client):
    a = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "subtitles": "en"})
    b = client.post("/api/jobs", json={"url": "https://youtu.be/abc"})
    c = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "subtitles": "en"})
    assert (a.status_code, b.status_code, c.status_code) == (201, 201, 200)
    assert c.json()["id"] == a.json()["id"]


@pytest.mark.parametrize("fmt, quality", [("mp3", "192"), ("audio", "best")])
def test_subtitles_rejected_for_audio_formats(client, fmt, quality):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "format": fmt, "quality": quality, "subtitles": "en"})
    assert r.status_code == 422 and "mp4" in r.text


@pytest.mark.parametrize("bad", ["", "en us", "auto:", "a" * 21, "en;rm", "AUTO:en", "../x",
                                 "all", "auto:all", "ALL", "-en", "auto:-en", ".en"])
def test_subtitles_rejects_malformed_values(client, bad):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/abc", "subtitles": bad})
    assert r.status_code == 422, bad


def test_playlist_jobs_carry_the_subtitles_choice(client, monkeypatch):
    _fake_expand(monkeypatch, result=PL)
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 10, "subtitles": "en"}, headers=CSRF)
    assert r.status_code == 201
    assert {j["subtitles"] for j in r.json()["jobs"]} == {"en"}
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 10, "format": "mp3", "quality": "192", "subtitles": "en"}, headers=CSRF)
    assert r.status_code == 422


def test_subtitles_null_or_absent_is_accepted(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/n1", "subtitles": None})
    assert r.status_code == 201 and r.json()["subtitles"] is None


def test_playlist_reuses_only_active_jobs_with_the_same_subtitles(client, monkeypatch):
    plain = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=a1"}).json()
    subbed = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=c3", "subtitles": "en"}).json()
    _fake_expand(monkeypatch, result=PL)
    body = client.post("/api/playlists", json={"url": "https://x/p", "limit": 10, "subtitles": "en"}, headers=CSRF).json()
    ids = {j["url"]: j["id"] for j in body["jobs"]}
    assert ids["https://www.youtube.com/watch?v=c3"] == subbed["id"]      # same subtitles: reused
    assert ids["https://www.youtube.com/watch?v=a1"] != plain["id"]       # different subtitles: new job
    assert body["reused"] == 1


# -- playlist zip ----------------------------------------------------------------

def _zip_of(r):
    import io, zipfile
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None
    return zf


def test_zip_playlist_streams_finished_files_named_by_position(client, monkeypatch, settings):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)   # j1 done (one.mp4), j2 failed, j3 queued
    store = client.app.state.store
    store.update(j3["id"], status=DONE, filename=f"{j3['id']}-three.mp4")
    (settings.downloads_dir / f"{j3['id']}-three.mp4").write_bytes(b"333")

    r = client.get(f"/api/playlists/{pid}/zip")
    zf = _zip_of(r)
    assert r.headers["content-disposition"] == 'attachment; filename="PL.zip"'
    assert zf.namelist() == ["001-one.mp4", "003-three.mp4"]
    assert zf.read("003-three.mp4") == b"333"


def test_zip_playlist_404s_for_unknown_id_or_nothing_finished(client, monkeypatch, settings):
    assert client.get("/api/playlists/nope/zip").status_code == 404
    body = _expand_playlist(client, monkeypatch, PL)
    pid = body["jobs"][0]["playlist_id"]
    r = client.get(f"/api/playlists/{pid}/zip")
    assert r.status_code == 404 and "finished" in r.json()["detail"]


def test_zip_playlist_skips_done_rows_whose_file_is_missing(client, monkeypatch, settings):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)
    (settings.downloads_dir / "one.mp4").unlink()
    assert client.get(f"/api/playlists/{pid}/zip").status_code == 404


def test_zip_filename_is_sanitised_and_falls_back_to_id():
    from app.api import _zip_filename
    assert _zip_filename("My Mix: Best / Hits", "abc") == "My Mix_ Best _ Hits.zip"
    assert _zip_filename("", "abc") == "playlist-abc.zip"
    assert _zip_filename(None, "abc") == "playlist-abc.zip"
    assert len(_zip_filename("x" * 200, "abc")) == 84
    assert _zip_filename("x" * 79 + ". trailing", "abc") == "x" * 79 + ".zip"


def test_zip_content_disposition_keeps_non_ascii_title_in_filename_star():
    from app.api import _content_disposition
    assert _content_disposition("PL", "abc") == 'attachment; filename="PL.zip"'
    h = _content_disposition('日本の歌 "best"\r\n', "abc")
    assert h.startswith('attachment; filename="best.zip"; filename*=UTF-8\'\'')
    assert "%E6%97%A5%E6%9C%AC" in h and "%0D" not in h and '"' not in h.split("filename*=")[1]


def test_zip_playlist_includes_kept_rows_and_skips_rows_whose_file_vanished(client, monkeypatch, settings):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)
    store = client.app.state.store
    client.post(f"/api/jobs/{j1['id']}/keep", headers=CSRF)
    store.update(j3["id"], status=DONE, filename="gone.mp4")     # done, but no file on disk
    zf = _zip_of(client.get(f"/api/playlists/{pid}/zip"))
    assert zf.namelist() == ["001-one.mp4"]


def test_status_reports_impersonation(client, settings, monkeypatch):
    monkeypatch.setattr(downloader_mod, "target_supported", lambda t: False)
    assert client.get("/api/status").json()["impersonate"] == {"target": "chrome", "active": False}
    monkeypatch.setattr(downloader_mod, "target_supported", lambda t: t is not None)
    assert client.get("/api/status").json()["impersonate"] == {"target": "chrome", "active": True}
    settings.impersonate = None
    assert client.get("/api/status").json()["impersonate"] == {"target": None, "active": False}


# -- cookies ----------------------------------------------------------------------

COOKIES = (".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tSECRET\n"
           "www.facebook.com\tFALSE\t/\tTRUE\t0\tc_user\tSECRET2\n")


def test_cookies_routes_require_csrf_header(client):
    assert client.get("/api/cookies").status_code == 403
    assert client.post("/api/cookies", json={"text": COOKIES, "keep": True}).status_code == 403
    assert client.delete("/api/cookies/abc").status_code == 403


def test_add_kept_cookies_lists_metadata_only_and_deletes(client, settings):
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": True}, headers=CSRF)
    assert r.status_code == 201
    entry = r.json()["entry"]
    assert r.json()["retried"] is False
    assert entry["source"] == "kept" and entry["sites"] == ["facebook.com", "instagram.com"] and entry["count"] == 2
    assert "SECRET" not in r.text and "sessionid" not in r.text
    listing = client.get("/api/cookies", headers=CSRF).json()
    assert [e["id"] for e in listing["entries"]] == [entry["id"]]
    assert "SECRET" not in client.get("/api/cookies", headers=CSRF).text
    assert client.delete(f"/api/cookies/{entry['id']}", headers=CSRF).status_code == 204
    assert client.get("/api/cookies", headers=CSRF).json() == {"entries": []}
    assert client.delete(f"/api/cookies/{entry['id']}", headers=CSRF).status_code == 404
    assert client.delete("/api/cookies/legacy", headers=CSRF).status_code == 404


def test_add_cookies_rejects_bad_format_with_reason(client):
    r = client.post("/api/cookies", json={"text": "<html>login page</html>", "keep": True}, headers=CSRF)
    assert r.status_code == 422 and "7 tab-separated" in r.json()["detail"]
    r = client.post("/api/cookies", json={"text": "x" * (1024 * 1024 + 1), "keep": True}, headers=CSRF)
    assert r.status_code == 422


def test_use_once_cookies_need_a_known_job_and_retry_it(client, settings):
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": False}, headers=CSRF)
    assert r.status_code == 422 and "job" in r.json()["detail"]
    assert client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": "nope"}, headers=CSRF).status_code == 404

    job = client.post("/api/jobs", json={"url": "https://www.instagram.com/p/abc/"}).json()
    client.app.state.store.update(job["id"], status=FAILED, error="You need to log in", needs_login=True)
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": job["id"]}, headers=CSRF)
    assert r.status_code == 201
    assert r.json()["entry"]["source"] == "once" and r.json()["retried"] is True
    again = client.app.state.store.get(job["id"])
    assert again.status == QUEUED and again.needs_login is False and again.error is None
    assert (settings.tmp_dir / f"cookies-once-{job['id']}.txt").is_file()
    assert client.get("/api/cookies", headers=CSRF).json() == {"entries": []}      # once-files are not listed


def test_kept_cookies_with_job_id_also_retry_the_failed_job(client, settings):
    job = client.post("/api/jobs", json={"url": "https://www.instagram.com/p/abc/"}).json()
    client.app.state.store.update(job["id"], status=FAILED, error="login", needs_login=True)
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": True, "job_id": job["id"]}, headers=CSRF)
    assert r.status_code == 201 and r.json()["retried"] is True and r.json()["entry"]["source"] == "kept"
    assert client.app.state.store.get(job["id"]).status == QUEUED


def test_cookies_for_a_job_that_is_not_failed_are_stored_without_retry(client, settings):
    job = client.post("/api/jobs", json={"url": "https://www.instagram.com/p/abc/"}).json()   # queued
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": job["id"]}, headers=CSRF)
    assert r.status_code == 201 and r.json()["retried"] is False
    assert client.app.state.store.get(job["id"]).status == QUEUED


def test_delete_job_discards_its_use_once_cookies(client, settings):
    job = client.post("/api/jobs", json={"url": "https://www.instagram.com/p/abc/"}).json()
    client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": job["id"]}, headers=CSRF)
    once = settings.tmp_dir / f"cookies-once-{job['id']}.txt"
    assert once.is_file()
    assert client.delete(f"/api/jobs/{job['id']}", headers=CSRF).status_code == 204
    assert not once.exists()


def test_failed_jobs_expose_needs_login(client):
    job = client.post("/api/jobs", json={"url": "https://www.instagram.com/p/abc/"}).json()
    assert job["needs_login"] is False
    client.app.state.store.update(job["id"], status=FAILED, error="login", needs_login=True)
    assert client.get("/api/jobs").json()[0]["needs_login"] is True


def test_oversized_cookie_text_422_does_not_echo_the_text(client):
    r = client.post("/api/cookies", json={"text": "S" * (1024 * 1024 + 1), "keep": True}, headers=CSRF)
    assert r.status_code == 422 and "1 MiB" in r.json()["detail"] and len(r.text) < 200


def test_use_once_cookies_for_a_running_or_done_job_are_refused(client, settings):
    job = client.post("/api/jobs", json={"url": "https://www.instagram.com/p/abc/"}).json()
    for status in (DOWNLOADING, DONE):
        client.app.state.store.update(job["id"], status=status)
        r = client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": job["id"]}, headers=CSRF)
        assert r.status_code == 409, status
    assert not (settings.tmp_dir / f"cookies-once-{job['id']}.txt").exists()


def test_playlist_login_wall_is_flagged_in_the_422_body(client, monkeypatch):
    def fake(url, s):
        raise probe_mod.ProbeError("[instagram] abc: You need to log in to access this content", needs_login=True)

    monkeypatch.setattr(probe_mod, "expand_playlist", fake)
    r = client.post("/api/playlists", json={"url": "https://x/p", "limit": 3}, headers=CSRF)
    assert r.status_code == 422 and r.json()["needs_login"] is True


# -- round 6: public job dicts and the log route --------------------------------

def test_job_list_and_routes_never_carry_the_log(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    assert "log" not in job
    client.app.state.store.update(job["id"], status=FAILED, log="ERROR: boom")
    assert all("log" not in j for j in client.get("/api/jobs").json())
    assert "log" not in client.post(f"/api/jobs/{job['id']}/retry", headers=CSRF).json()


def test_log_route_returns_the_stored_log_or_empty(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    assert client.get(f"/api/jobs/{job['id']}/log").json() == {"log": ""}
    client.app.state.store.update(job["id"], status=FAILED, log="line 1\nERROR: boom")
    r = client.get(f"/api/jobs/{job['id']}/log")
    assert r.status_code == 200 and r.json() == {"log": "line 1\nERROR: boom"}
    assert client.get("/api/jobs/nope1234/log").status_code == 404


# -- round 6: status disk and proxy ----------------------------------------------

def test_status_reports_disk_and_proxy(client, settings, monkeypatch):
    import shutil
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10, 5, 600 * 1024 ** 2))
    body = client.get("/api/status").json()
    assert body["disk"] == {"free": 600 * 1024 ** 2, "min_free": 1024 ** 3, "low": False}   # disk_low patched
    assert body["proxy"] is None
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: True)
    settings.proxy = "http://user:secret@proxy.lan:3128"
    body = client.get("/api/status").json()
    assert body["disk"]["low"] is True
    assert body["proxy"] == "http://proxy.lan:3128"
    assert "secret" not in json.dumps(body)


# -- round 6: thumbnails ----------------------------------------------------------

def test_thumbs_route_serves_a_stored_file(client, settings):
    (settings.thumbs_dir / "abcdEFGH12.webp").write_bytes(b"RIFF....WEBP")
    r = client.get("/api/thumbs/abcdEFGH12")
    assert r.status_code == 200
    assert r.content == b"RIFF....WEBP"
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.parametrize("key", ["missing123", "short", "a" * 17, "bad.key12", "..%2Fjobs"])
def test_thumbs_route_404s_unknown_or_invalid_keys(client, settings, key):
    (settings.thumbs_dir / "abcdEFGH12.jpg").write_bytes(b"\xff\xd8\xff")
    assert client.get(f"/api/thumbs/{key}").status_code == 404


def test_create_job_with_a_known_thumb(client, settings):
    (settings.thumbs_dir / "abcdEFGH12.jpg").write_bytes(b"\xff\xd8\xff")
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a", "thumb": "abcdEFGH12"})
    assert r.status_code == 201 and r.json()["thumb"] == "abcdEFGH12"


@pytest.mark.parametrize("thumb", ["missing123", "bad key!", "x"])
def test_create_job_rejects_unknown_or_malformed_thumb(client, thumb):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a", "thumb": thumb})
    assert r.status_code == 422
    assert client.app.state.store.list() == []


# -- round 6: clips ---------------------------------------------------------------

def test_create_job_with_a_clip(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 10, "clip_end": 70.5})
    assert r.status_code == 201
    assert (r.json()["clip_start"], r.json()["clip_end"]) == (10, 70.5)
    open_end = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 10}).json()
    assert (open_end["clip_start"], open_end["clip_end"]) == (10, None)


def test_different_clips_of_one_video_are_separate_jobs_same_clip_reuses(client):
    a = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 0, "clip_end": 30}).json()
    b = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 30, "clip_end": 60})
    whole = client.post("/api/jobs", json={"url": "https://youtu.be/a"})
    again = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 0, "clip_end": 30})
    assert b.status_code == 201 and whole.status_code == 201
    assert again.status_code == 200 and again.json()["id"] == a["id"]


@pytest.mark.parametrize("clip", [{"clip_start": 30, "clip_end": 30}, {"clip_start": 30, "clip_end": 10},
                                  {"clip_start": -1}, {"clip_end": 10_000_000}, {"clip_start": "abc"}])
def test_create_job_rejects_bad_clips(client, clip):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a", **clip})
    assert r.status_code == 422


@pytest.mark.parametrize("clip", [{"clip_start": 5}, {"clip_end": 5}])
def test_playlists_reject_clips(client, monkeypatch, clip):
    _fake_expand(monkeypatch)
    r = client.post("/api/playlists", json={"url": "https://www.youtube.com/playlist?list=x", "limit": 5, **clip},
                    headers=CSRF)
    assert r.status_code == 422
    assert "clips apply to single videos" in r.text


# -- round 6: free-space guard ----------------------------------------------------

LOW_DISK_DETAIL = "Only 600.0 MB free on the download disk; downloads need at least 1.0 GB"


def _make_disk_low(monkeypatch):
    import shutil
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10 * 1024 ** 3, 0, 600 * 1024 ** 2))
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: True)


@pytest.fixture
def low_disk(monkeypatch):
    _make_disk_low(monkeypatch)


def test_human_sizes():
    from app.api import _human
    assert _human(600 * 1024 ** 2) == "600.0 MB"
    assert _human(1024 ** 3) == "1.0 GB"
    assert _human(512) == "512.0 B"
    assert _human(1536) == "1.5 KB"


def test_create_job_507_when_disk_is_low(client, low_disk):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a"})
    assert r.status_code == 507
    assert r.json() == {"detail": LOW_DISK_DETAIL}
    assert client.app.state.store.list() == [] and client.app.state.pool.queue.qsize() == 0


def test_playlists_507_when_disk_is_low(client, monkeypatch, low_disk):
    calls = []
    _fake_expand(monkeypatch, calls=calls)
    r = client.post("/api/playlists", json={"url": "https://www.youtube.com/playlist?list=x", "limit": 5},
                    headers=CSRF)
    assert r.status_code == 507 and r.json() == {"detail": LOW_DISK_DETAIL}
    assert calls == [] and client.app.state.store.list() == []


def test_retries_507_when_disk_is_low(client, monkeypatch):
    jid = _failed_job(client)
    client.app.state.store.update(jid, playlist_id="pl000001")
    before = client.app.state.pool.queue.qsize()
    _make_disk_low(monkeypatch)
    for path in (f"/api/jobs/{jid}/retry", "/api/playlists/pl000001/retry"):
        r = client.post(path, headers=CSRF)
        assert r.status_code == 507 and r.json() == {"detail": LOW_DISK_DETAIL}, path
    assert client.app.state.store.get(jid).status == FAILED
    assert client.app.state.pool.queue.qsize() == before


def test_cookies_for_a_failed_job_507_when_disk_is_low_but_plain_cookies_are_stored(client, monkeypatch):
    jid = _failed_job(client)
    _make_disk_low(monkeypatch)
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": jid}, headers=CSRF)
    assert r.status_code == 507 and r.json() == {"detail": LOW_DISK_DETAIL}
    assert client.app.state.store.get(jid).status == FAILED
    assert client.get("/api/cookies", headers=CSRF).json() == {"entries": []}
    assert client.post("/api/cookies", json={"text": COOKIES, "keep": True}, headers=CSRF).status_code == 201


def test_guard_is_off_when_disk_is_fine(client):
    assert client.post("/api/jobs", json={"url": "https://youtu.be/a"}).status_code == 201


# -- round 6: several links at once ----------------------------------------------

def test_batch_507_when_disk_is_low(client, low_disk):
    r = client.post("/api/jobs/batch", json={"urls": ["https://youtu.be/a"]}, headers=CSRF)
    assert r.status_code == 507 and r.json() == {"detail": LOW_DISK_DETAIL}
    assert client.app.state.store.list() == []


def test_batch_requires_header(client):
    r = client.post("/api/jobs/batch", json={"urls": ["https://youtu.be/a"]})
    assert r.status_code == 403
    assert client.app.state.store.list() == []


def test_batch_queues_deduped_urls_in_order_and_lists_rejected(client):
    urls = [" https://youtu.be/a ", "not a url", "https://youtu.be/b", "https://youtu.be/a", "ftp://x/y",
            "https://youtu.be/c"]
    r = client.post("/api/jobs/batch", json={"urls": urls, "format": "mp3", "quality": "192"}, headers=CSRF)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"queued", "reused", "rejected", "jobs"}
    assert (body["queued"], body["reused"]) == (3, 0)
    assert body["rejected"] == ["not a url", "ftp://x/y"]
    assert [j["url"] for j in body["jobs"]] == ["https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/c"]
    assert all(j["format"] == "mp3" and j["quality"] == "192" and j["status"] == QUEUED for j in body["jobs"])
    assert all("log" not in j for j in body["jobs"])
    assert client.app.state.pool.queue.qsize() == 3
    assert len(client.app.state.store.list()) == 3


def test_batch_saves_all_new_jobs_at_once(client, monkeypatch):
    store = client.app.state.store
    calls = []
    real = store.add_many
    monkeypatch.setattr(store, "add_many", lambda items: calls.append(len(items)) or real(items))
    client.post("/api/jobs/batch", json={"urls": ["https://youtu.be/a", "https://youtu.be/b"]}, headers=CSRF)
    assert calls == [2]


def test_batch_reuses_active_duplicates(client):
    existing = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    other_quality = client.post("/api/jobs", json={"url": "https://youtu.be/b", "quality": "720"}).json()
    before = client.app.state.pool.queue.qsize()
    r = client.post("/api/jobs/batch", json={"urls": ["https://youtu.be/a", "https://youtu.be/b"]}, headers=CSRF)
    body = r.json()
    assert (body["queued"], body["reused"], body["rejected"]) == (1, 1, [])
    assert body["jobs"][0]["id"] == existing["id"]
    assert body["jobs"][1]["id"] != other_quality["id"]
    assert client.app.state.pool.queue.qsize() == before + 1


def test_batch_with_no_valid_url_is_422(client):
    r = client.post("/api/jobs/batch", json={"urls": ["nope", "ftp://x"]}, headers=CSRF)
    assert r.status_code == 422
    assert client.app.state.store.list() == []


def test_batch_over_the_cap_is_422_naming_it(client, settings):
    settings.max_playlist_items = 2
    urls = ["https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/a", "bad"]
    assert client.post("/api/jobs/batch", json={"urls": urls}, headers=CSRF).status_code == 201
    r = client.post("/api/jobs/batch", json={"urls": urls + ["https://youtu.be/c"]}, headers=CSRF)
    assert r.status_code == 422
    assert "2" in r.json()["detail"]
    assert len(client.app.state.store.list()) == 2


@pytest.mark.parametrize("body", [{"urls": []}, {"urls": ["https://youtu.be/a"] * 2001}, {},
                                  {"urls": ["https://youtu.be/a"], "format": "mp3", "quality": "1080"},
                                  {"urls": "https://youtu.be/a"}])
def test_batch_rejects_bad_bodies(client, body):
    assert client.post("/api/jobs/batch", json=body, headers=CSRF).status_code == 422


# -- round 6: download next -------------------------------------------------------

def test_next_requires_header_and_known_job(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    assert client.post(f"/api/jobs/{job['id']}/next").status_code == 403
    assert client.app.state.store.get(job["id"]).priority is False
    assert client.post("/api/jobs/nope1234/next", headers=CSRF).status_code == 404


def test_next_moves_a_queued_job_to_the_front_and_flags_it(client):
    a = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    b = client.post("/api/jobs", json={"url": "https://youtu.be/b"}).json()
    r = client.post(f"/api/jobs/{b['id']}/next", headers=CSRF)
    assert r.status_code == 200
    assert r.json()["id"] == b["id"] and r.json()["priority"] is True and "log" not in r.json()
    assert client.app.state.store.get(b["id"]).priority is True
    assert list(client.app.state.pool.queue._items)[0] == b["id"]
    assert client.app.state.pool.queue.qsize() == 3   # the stale FIFO entry is skipped by the worker
    assert client.app.state.store.get(a["id"]).priority is False


@pytest.mark.parametrize("status", [PAUSED, DOWNLOADING, DONE, FAILED])
def test_next_non_queued_job_is_409(client, status):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.app.state.store.update(job["id"], status=status)
    before = client.app.state.pool.queue.qsize()
    r = client.post(f"/api/jobs/{job['id']}/next", headers=CSRF)
    assert r.status_code == 409
    assert client.app.state.store.get(job["id"]).priority is False
    assert client.app.state.pool.queue.qsize() == before


def test_pause_clears_priority(client):
    job = client.post("/api/jobs", json={"url": "https://youtu.be/a"}).json()
    client.post(f"/api/jobs/{job['id']}/next", headers=CSRF)
    r = client.post(f"/api/jobs/{job['id']}/pause", headers=CSRF)
    assert r.status_code == 200 and r.json()["priority"] is False
    assert client.app.state.store.get(job["id"]).priority is False


# -- round 6: thumbnails go with their jobs ------------------------------------------

def _thumb(settings, key):
    path = settings.thumbs_dir / f"{key}.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    return path


def test_delete_job_deletes_its_thumb_only(client, settings):
    mine, other = _thumb(settings, "mineKEY123"), _thumb(settings, "otherKEY12")
    jid = client.post("/api/jobs", json={"url": "https://youtu.be/a", "thumb": "mineKEY123"}).json()["id"]
    client.post("/api/jobs", json={"url": "https://youtu.be/b", "thumb": "otherKEY12"})
    assert client.delete(f"/api/jobs/{jid}", headers=CSRF).status_code == 204
    assert not mine.exists() and other.exists()


def test_remove_finished_playlist_rows_deletes_their_thumbs(client, monkeypatch, settings):
    body = _expand_playlist(client, monkeypatch, PL)
    store = client.app.state.store
    done, active = body["jobs"][0]["id"], body["jobs"][1]["id"]
    store.update(done, status=DONE, thumb="doneKEY123")
    store.update(active, thumb="liveKEY123")
    done_thumb, live_thumb = _thumb(settings, "doneKEY123"), _thumb(settings, "liveKEY123")
    pid = body["jobs"][0]["playlist_id"]
    assert client.delete(f"/api/playlists/{pid}?status=finished", headers=CSRF).json() == {"removed": 1}
    assert not done_thumb.exists() and live_thumb.exists()
    client.delete(f"/api/playlists/{pid}", headers=CSRF)   # cancel remaining
    assert not live_thumb.exists()


def test_cleanup_endpoint_deletes_thumbs_of_removed_jobs_and_keeps_kept_ones(client, settings):
    store = client.app.state.store
    kept = store.add("https://youtu.be/k", thumb="keptKEY123")
    store.update(kept.id, status=DONE, keep=True)
    store.add("https://youtu.be/p", thumb="goneKEY123")
    kept_thumb, gone_thumb = _thumb(settings, "keptKEY123"), _thumb(settings, "goneKEY123")
    assert client.post("/api/cleanup", headers=CSRF).status_code == 200
    assert kept_thumb.exists() and not gone_thumb.exists()


def test_startup_sweeps_old_orphan_thumbs(settings):
    import os
    import time
    settings.jobs_file.write_text(json.dumps([Job(id="dn000001", url="https://youtu.be/d", status=DONE,
                                                  thumb="usedKEY123").to_dict()]))
    old = time.time() - 7200
    used, orphan, young = (_thumb(settings, k) for k in ("usedKEY123", "orphKEY123", "younKEY123"))
    for p in (used, orphan):
        os.utime(p, (old, old))
    with TestClient(create_app(settings, run_background=False)):
        pass
    assert used.exists() and young.exists() and not orphan.exists()


# -- review fix: a thumbnail shared by two jobs (one Check) survives until the last goes -------

def test_delete_job_keeps_a_thumb_another_job_still_uses(client, settings):
    shared = _thumb(settings, "sharKEY123")
    a = client.post("/api/jobs", json={"url": "https://youtu.be/a", "thumb": "sharKEY123"}).json()["id"]
    b = client.post("/api/jobs", json={"url": "https://youtu.be/a", "format": "mp3",
                                       "thumb": "sharKEY123"}).json()["id"]
    assert client.delete(f"/api/jobs/{a}", headers=CSRF).status_code == 204
    assert shared.exists()
    assert client.delete(f"/api/jobs/{b}", headers=CSRF).status_code == 204
    assert not shared.exists()


@pytest.mark.parametrize("mode", ["active", "finished"])
def test_remove_playlist_rows_keeps_a_thumb_another_job_still_uses(client, monkeypatch, settings, mode):
    body = _expand_playlist(client, monkeypatch, PL)
    store = client.app.state.store
    member = body["jobs"][0]["id"]
    store.update(member, thumb="sharKEY123", **({"status": DONE} if mode == "finished" else {}))
    other = store.add("https://youtu.be/elsewhere", thumb="sharKEY123")
    shared = _thumb(settings, "sharKEY123")
    pid = body["jobs"][0]["playlist_id"]
    assert client.delete(f"/api/playlists/{pid}?status={mode}", headers=CSRF).status_code == 200
    assert store.get(member) is None and shared.exists()
    assert client.delete(f"/api/jobs/{other.id}", headers=CSRF).status_code == 204
    assert not shared.exists()


def test_batch_rejected_list_is_capped_and_truncated(client):
    junk = [f"bad{i} " + "x" * 300 for i in range(30)]
    r = client.post("/api/jobs/batch", json={"urls": junk + ["https://youtu.be/a"]}, headers=CSRF)
    assert r.status_code == 201
    rejected = r.json()["rejected"]
    assert len(rejected) == 20 and rejected[0] == junk[0][:200] and rejected[19].startswith("bad19 ")
    assert all(len(x) == 200 for x in rejected)


# -- round 7: exact cut ---------------------------------------------------------------

def test_create_job_with_an_exact_clip(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 10, "clip_end": 20,
                                       "clip_exact": True})
    assert r.status_code == 201 and r.json()["clip_exact"] is True
    assert client.app.state.store.get(r.json()["id"]).clip_exact is True
    plain = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 10, "clip_end": 20})
    assert plain.status_code == 201 and plain.json()["clip_exact"] is False   # a different job
    again = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_start": 10, "clip_end": 20,
                                           "clip_exact": True})
    assert again.status_code == 200 and again.json()["id"] == r.json()["id"]


def test_exact_cut_without_a_clip_is_422(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_exact": True})
    assert r.status_code == 422 and "exact cut needs a clip" in r.text
    assert client.post("/api/jobs", json={"url": "https://youtu.be/a", "clip_exact": False}).status_code == 201


def test_playlists_reject_exact_cut(client, monkeypatch):
    _fake_expand(monkeypatch)
    r = client.post("/api/playlists", json={"url": "https://www.youtube.com/playlist?list=x", "limit": 5,
                                            "clip_exact": True}, headers=CSRF)
    assert r.status_code == 422


# -- round 7: kept-files directory ------------------------------------------------------

@pytest.fixture
def keep_dir(settings):
    settings.keep_dir = settings.data_dir / "keep"
    settings.keep_dir.mkdir()
    return settings.keep_dir


def test_keep_moves_the_file_into_keep_dir_and_files_route_still_serves_it(client, settings, keep_dir):
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    r = client.post(f"/api/jobs/{jid}/keep", headers=CSRF)
    assert r.status_code == 200 and r.json()["keep"] is True and r.json()["filename"] == "k1.mp4"
    assert not (settings.downloads_dir / "k1.mp4").exists()
    assert (keep_dir / "k1.mp4").read_bytes() == b"data"
    served = client.get("/files/k1.mp4")
    assert served.status_code == 200 and served.content == b"data"


def test_keep_suffixes_the_name_on_a_collision_in_keep_dir(client, settings, keep_dir):
    (keep_dir / "k1.mp4").write_bytes(b"older")
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    r = client.post(f"/api/jobs/{jid}/keep", headers=CSRF)
    assert r.json()["filename"] == "k1 (2).mp4"
    assert client.app.state.store.get(jid).filename == "k1 (2).mp4"
    assert client.get("/files/k1 (2).mp4").content == b"data"
    assert (keep_dir / "k1.mp4").read_bytes() == b"older"


def test_unkeep_moves_the_file_back(client, settings, keep_dir):
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    client.post(f"/api/jobs/{jid}/keep", headers=CSRF)
    r = client.post(f"/api/jobs/{jid}/unkeep", headers=CSRF)
    assert r.status_code == 200 and r.json()["keep"] is False and r.json()["filename"] == "k1.mp4"
    assert (settings.downloads_dir / "k1.mp4").read_bytes() == b"data"
    assert list(keep_dir.iterdir()) == []


def test_keep_job_removed_during_the_move_leaves_no_file_in_keep_dir(client, settings, keep_dir, monkeypatch):
    from app import files as files_mod
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    real = files_mod.move_to_keep

    def move_then_clear(s, job):
        name = real(s, job)
        client.app.state.store.clear()
        return name

    monkeypatch.setattr(files_mod, "move_to_keep", move_then_clear)
    assert client.post(f"/api/jobs/{jid}/keep", headers=CSRF).status_code == 404
    assert list(keep_dir.iterdir()) == []


@pytest.mark.parametrize("exc, status, detail", [
    (PermissionError(13, "Permission denied"), 500, "Could not move the file: Permission denied"),
    (OSError(28, "No space left on device"), 507, "Could not move the file: No space left on device"),
])
def test_keep_move_failure_answers_an_error_and_leaves_keep_unchanged(client, settings, keep_dir, monkeypatch,
                                                                      exc, status, detail):
    from app import files as files_mod
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")

    def boom(s, job):
        raise exc

    monkeypatch.setattr(files_mod, "move_to_keep", boom)
    r = client.post(f"/api/jobs/{jid}/keep", headers=CSRF)
    assert r.status_code == status and r.json() == {"detail": detail}
    job = client.app.state.store.get(jid)
    assert job.keep is False and job.filename == "k1.mp4"


def test_unkeep_move_failure_leaves_the_job_kept(client, settings, keep_dir, monkeypatch):
    from app import files as files_mod
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    client.post(f"/api/jobs/{jid}/keep", headers=CSRF)

    def boom(s, job):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(files_mod, "move_back", boom)
    assert client.post(f"/api/jobs/{jid}/unkeep", headers=CSRF).status_code == 500
    assert client.app.state.store.get(jid).keep is True


def test_keep_file_vanishing_mid_move_is_404(client, settings, keep_dir, monkeypatch):
    from app import files as files_mod
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")

    def gone(s, job):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(files_mod, "move_to_keep", gone)
    assert client.post(f"/api/jobs/{jid}/keep", headers=CSRF).status_code == 404
    assert client.app.state.store.get(jid).keep is False


def test_bulk_keep_counts_a_failed_move_as_skipped_and_goes_on(client, settings, keep_dir, monkeypatch):
    import errno
    from app import files as files_mod
    a = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    b = _done_job(client, settings, "https://youtu.be/b", "b.mp4")
    c = _done_job(client, settings, "https://youtu.be/c", "c.mp4")
    real = files_mod.move_to_keep

    def flaky(s, job):
        if job.id == a:
            raise PermissionError(errno.EACCES, "Permission denied")
        if job.id == b:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return real(s, job)

    monkeypatch.setattr(files_mod, "move_to_keep", flaky)
    r = client.post("/api/jobs/bulk", json={"ids": [a, b, c], "action": "keep"}, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"done": 1, "skipped": 2}
    store = client.app.state.store
    assert not store.get(a).keep and not store.get(b).keep and store.get(c).keep


def test_delete_kept_job_removes_its_file_from_keep_dir(client, settings, keep_dir):
    jid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    client.post(f"/api/jobs/{jid}/keep", headers=CSRF)
    assert client.delete(f"/api/jobs/{jid}", headers=CSRF).status_code == 204
    assert list(keep_dir.iterdir()) == [] and client.get("/api/jobs").json() == []


def test_zip_playlist_includes_files_in_keep_dir(client, monkeypatch, settings, keep_dir):
    pid, j1, j2, j3 = _mixed_playlist(client, monkeypatch, settings)
    client.post(f"/api/jobs/{j1['id']}/keep", headers=CSRF)
    assert (keep_dir / "one.mp4").exists()
    zf = _zip_of(client.get(f"/api/playlists/{pid}/zip"))
    assert zf.namelist() == ["001-one.mp4"] and zf.read("001-one.mp4") == b"x"


def test_cleanup_leaves_keep_dir_alone(client, settings, keep_dir):
    kid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    _done_job(client, settings, "https://youtu.be/k2", "k2.mp4")
    client.post(f"/api/jobs/{kid}/keep", headers=CSRF)
    (keep_dir / "by-hand.txt").write_text("mine")           # a file no job knows about
    assert client.post("/api/cleanup", headers=CSRF).status_code == 200
    assert sorted(p.name for p in keep_dir.iterdir()) == ["by-hand.txt", "k1.mp4"]
    assert list(settings.downloads_dir.iterdir()) == []
    assert [j["id"] for j in client.get("/api/jobs").json()] == [kid]


def test_status_reports_pacing_and_keep_dir(client, settings):
    body = client.get("/api/status").json()
    assert body["pacing"] == {"max_per_site": 1, "site_gap": 3.0}
    assert body["keep_dir"] is False
    settings.max_per_site, settings.site_gap = 0, 0.5
    settings.keep_dir = settings.data_dir / "keep"
    body = client.get("/api/status").json()
    assert body["pacing"] == {"max_per_site": 0, "site_gap": 0.5} and body["keep_dir"] is True


def test_status_downloads_usage_counts_keep_dir_too(client, settings, keep_dir):
    (settings.downloads_dir / "a.mp4").write_bytes(b"x" * 10)
    (keep_dir / "b.mp4").write_bytes(b"y" * 5)
    assert client.get("/api/status").json()["downloads"] == {"files": 2, "bytes": 15}


# -- round 7: bulk actions --------------------------------------------------------------

def test_bulk_keep_and_unkeep_count_done_jobs_and_skip_the_rest(client, settings, keep_dir):
    a = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    b = _done_job(client, settings, "https://youtu.be/b", "b.mp4")
    queued = client.post("/api/jobs", json={"url": "https://youtu.be/q"}).json()["id"]
    r = client.post("/api/jobs/bulk", json={"ids": [a, b, queued, "nope", a], "action": "keep"}, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"done": 2, "skipped": 3}   # the repeat counts as skipped
    store = client.app.state.store
    assert store.get(a).keep and store.get(b).keep and not store.get(queued).keep
    assert sorted(p.name for p in keep_dir.iterdir()) == ["a.mp4", "b.mp4"]
    r = client.post("/api/jobs/bulk", json={"ids": [a, queued], "action": "unkeep"}, headers=CSRF)
    assert r.json() == {"done": 1, "skipped": 1}
    assert not store.get(a).keep and (settings.downloads_dir / "a.mp4").exists()
    assert [p.name for p in keep_dir.iterdir()] == ["b.mp4"]


def test_bulk_remove_behaves_like_delete_per_id(client, settings, keep_dir):
    a = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    kept = _done_job(client, settings, "https://youtu.be/k", "k.mp4")
    client.post(f"/api/jobs/{kept}/keep", headers=CSRF)
    queued = client.post("/api/jobs", json={"url": "https://youtu.be/q"}).json()["id"]
    keepme = _done_job(client, settings, "https://youtu.be/z", "z.mp4")
    r = client.post("/api/jobs/bulk", json={"ids": [a, kept, queued, "nope"], "action": "remove"}, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"done": 3, "skipped": 1}
    assert [j["id"] for j in client.get("/api/jobs").json()] == [keepme]
    assert [p.name for p in settings.downloads_dir.iterdir()] == ["z.mp4"]
    assert list(keep_dir.iterdir()) == []


def test_bulk_remove_cleans_thumbnails_once_for_the_whole_batch(client, settings, monkeypatch):
    from app import cleanup as cleanup_mod
    calls = []
    real = cleanup_mod.delete_unused_thumbs
    monkeypatch.setattr(cleanup_mod, "delete_unused_thumbs",
                        lambda store, s, removed: (calls.append([j.id for j in removed]), real(store, s, removed)))
    ids = [_done_job(client, settings, f"https://youtu.be/{i}", f"{i}.mp4") for i in range(3)]
    r = client.post("/api/jobs/bulk", json={"ids": ids + ["nope"], "action": "remove"}, headers=CSRF)
    assert r.json() == {"done": 3, "skipped": 1}
    assert calls == [ids]


def test_bulk_requires_csrf(client, settings):
    a = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    assert client.post("/api/jobs/bulk", json={"ids": [a], "action": "remove"}).status_code == 403
    assert client.app.state.store.get(a) is not None


@pytest.mark.parametrize("body", [
    {"ids": [], "action": "keep"},
    {"ids": ["x"] * 501, "action": "keep"},
    {"ids": ["x"], "action": "explode"},
    {"ids": ["x"]},
    {"action": "remove"},
])
def test_bulk_rejects_bad_bodies(client, body):
    assert client.post("/api/jobs/bulk", json=body, headers=CSRF).status_code == 422


def test_bulk_accepts_500_ids(client):
    r = client.post("/api/jobs/bulk", json={"ids": [f"id{i}" for i in range(500)], "action": "remove"},
                    headers=CSRF)
    assert r.status_code == 200 and r.json() == {"done": 0, "skipped": 500}


# -- round 7: zip of selected downloads -------------------------------------------------

def _done_job_named(client, settings, url, suffix):
    """A done job whose file is named like the downloader names it: '<job id>-<suffix>'."""
    jid = client.post("/api/jobs", json={"url": url}).json()["id"]
    (settings.downloads_dir / f"{jid}-{suffix}").write_bytes(jid.encode())
    client.app.state.store.update(jid, status=DONE, progress=100, filename=f"{jid}-{suffix}")
    return jid


def test_zip_ids_streams_the_finished_files_among_them(client, settings, keep_dir):
    a = _done_job_named(client, settings, "https://youtu.be/a", "first.mp4")
    b = _done_job_named(client, settings, "https://youtu.be/b", "same.mp4")
    c = _done_job_named(client, settings, "https://youtu.be/c", "same.mp4")
    client.post(f"/api/jobs/{c}/keep", headers=CSRF)             # its file now lives in KEEP_DIR
    assert (keep_dir / f"{c}-same.mp4").exists()
    queued = client.post("/api/jobs", json={"url": "https://youtu.be/q"}).json()["id"]
    r = client.get(f"/api/zip?ids= {a}, {b},,{queued},nope,{c},{a} ")
    zf = _zip_of(r)
    assert r.headers["content-disposition"] == 'attachment; filename="downloads.zip"'
    assert zf.namelist() == ["first.mp4", "same.mp4", "same (2).mp4"]   # id prefix dropped, deduped
    assert zf.read("same.mp4") == b.encode() and zf.read("same (2).mp4") == c.encode()


def test_zip_ids_404_when_none_finished(client, settings):
    queued = client.post("/api/jobs", json={"url": "https://youtu.be/q"}).json()["id"]
    gone = _done_job(client, settings, "https://youtu.be/g", "g.mp4")
    (settings.downloads_dir / "g.mp4").unlink()
    r = client.get(f"/api/zip?ids={queued},{gone},nope")
    assert r.status_code == 404 and "finished" in r.json()["detail"]


@pytest.mark.parametrize("ids", ["", ",, ,", ",".join(f"id{i}" for i in range(501))])
def test_zip_ids_422_when_empty_or_too_many(client, ids):
    assert client.get(f"/api/zip?ids={ids}").status_code == 422
    assert client.get("/api/zip").status_code == 422


# -- round 7: QR code --------------------------------------------------------------------

def _request(scheme="http", host="box.local:8180"):
    from starlette.requests import Request as StarletteRequest
    headers = [(b"host", host.encode())] if host else []
    return StarletteRequest({"type": "http", "method": "GET", "scheme": scheme, "path": "/api/x",
                             "query_string": b"", "headers": headers, "server": ("10.0.0.2", 8180)})


def test_file_url_uses_scheme_host_header_and_quoted_name():
    from app.api import _file_url
    job = Job(id="j1", url="https://youtu.be/a", filename="j1-My Video #1?.mp4")
    assert _file_url(_request(), job) == "http://box.local:8180/files/j1-My%20Video%20%231%3F.mp4"
    assert _file_url(_request("https", "dl.example"), job).startswith("https://dl.example/files/")
    job.filename = "j1-日本.mp4"
    assert _file_url(_request(), job) == "http://box.local:8180/files/j1-%E6%97%A5%E6%9C%AC.mp4"


def test_qr_svg_refuses_an_oversized_url(client, settings):
    jid = _done_job(client, settings, "https://youtu.be/a", "a.mp4")
    r = client.get(f"/api/jobs/{jid}/qr.svg", headers={"Host": "h" * 3000})
    assert r.status_code == 422 and r.json() == {"detail": "URL too long for a QR code"}


def test_qr_svg_for_a_done_job(client, settings, monkeypatch):
    import segno
    from app import api as api_mod
    seen = []
    real_make = segno.make

    def spy(content, **kw):
        seen.append((content, kw))
        return real_make(content, **kw)

    monkeypatch.setattr(api_mod.segno, "make", spy)
    jid = _done_job(client, settings, "https://youtu.be/a", "a b.mp4")
    r = client.get(f"/api/jobs/{jid}/qr.svg", headers={"Host": "phone-sees.me:8180"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-store"
    assert r.text.lstrip().startswith(("<svg", "<?xml")) and "<svg" in r.text
    assert 'xmlns="http://www.w3.org/2000/svg"' in r.text      # standalone: loads in an <img>
    assert seen == [("http://phone-sees.me:8180/files/a%20b.mp4", {"error": "m", "micro": False})]


def test_qr_svg_404_unknown_409_not_done(client, settings):
    assert client.get("/api/jobs/nope/qr.svg").status_code == 404
    queued = client.post("/api/jobs", json={"url": "https://youtu.be/q"}).json()["id"]
    assert client.get(f"/api/jobs/{queued}/qr.svg").status_code == 409
    client.app.state.store.update(queued, status=FAILED, error="x")
    assert client.get(f"/api/jobs/{queued}/qr.svg").status_code == 409
    gone = _done_job(client, settings, "https://youtu.be/g", "g.mp4")
    (settings.downloads_dir / "g.mp4").unlink()
    assert client.get(f"/api/jobs/{gone}/qr.svg").status_code == 404


# --- round 8: HTTPS certificate download and status ----------------------------------------

from app import tls as tls_mod


@pytest.fixture
def https_settings(settings):
    settings.https = True
    tls_mod.ensure_certs(settings.tls_dir, ["localhost", "127.0.0.1"])
    return settings


@pytest.fixture
def https_client(https_settings, monkeypatch):
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: False)
    with TestClient(create_app(https_settings, run_background=False)) as c:
        yield c


@pytest.mark.parametrize("path", ["/cert.pem", "/cert.crt"])
def test_cert_404_when_https_off(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.json() == {"detail": "HTTPS is off"}


@pytest.mark.parametrize("path", ["/cert.pem", "/cert.crt"])
def test_cert_served_when_https_on(https_client, https_settings, path):
    r = https_client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-x509-ca-cert"
    assert r.headers["content-disposition"] == 'attachment; filename="sns-downloader-ca.crt"'
    assert r.headers["cache-control"] == "no-store"
    assert r.content == (https_settings.tls_dir / tls_mod.CA_CERT).read_bytes()
    assert b"PRIVATE KEY" not in r.content
    assert r.content != (https_settings.tls_dir / tls_mod.SERVER_CERT).read_bytes()


def test_cert_404_when_file_missing(https_client, https_settings):
    (https_settings.tls_dir / tls_mod.CA_CERT).unlink()
    r = https_client.get("/cert.pem")
    assert r.status_code == 404
    assert r.json() == {"detail": "Certificate not found"}


def test_status_https_fields_off(client):
    body = client.get("/api/status").json()
    assert body["https"] is False
    assert body["cert_hosts"] == []


def test_status_https_fields_on(https_client):
    body = https_client.get("/api/status").json()
    assert body["https"] is True
    assert body["cert_hosts"] == ["localhost", "127.0.0.1"]


def test_webmanifest_and_svg_mime_types_registered(client):
    import mimetypes
    assert mimetypes.guess_type("x.webmanifest")[0] == "application/manifest+json"
    assert mimetypes.guess_type("x.svg")[0] == "image/svg+xml"


def test_manifest_served_with_share_target(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.content)
    assert m["start_url"] == "/" and m["scope"] == "/" and m["display"] == "standalone"
    assert m["share_target"] == {"action": "/", "method": "GET",
                                 "params": {"url": "url", "text": "text", "title": "title"}}
    purposes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    assert purposes == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_service_worker_and_icons_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "sns-v1" in r.text
    r = client.get("/icons/icon-192.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/icons/apple-touch-icon.png").status_code == 200
    r = client.get("/icons/icon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


def test_index_links_manifest_and_registers_service_worker(client):
    r = client.get("/")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in r.text
    assert '<meta name="theme-color" content="#4f46e5">' in r.text
    assert '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">' in r.text
    assert "navigator.serviceWorker.register('/sw.js')" in r.text
