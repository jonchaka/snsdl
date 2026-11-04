import json

import pytest
from fastapi.testclient import TestClient

from app import downloader as downloader_mod
from app import probe as probe_mod
from app.jobs import DONE, DOWNLOADING, FAILED, PAUSED, QUEUED, Job
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
    assert 'id="plbox"' in r.text
    assert 'id="plcheck"' in r.text
    assert 'id="pllimit"' in r.text
    assert "/api/playlists" in r.text
    assert 'id="playlists"' in r.text
    assert "plcancel" in r.text
    assert "/api/playlists/" in r.text
    assert "plremove" in r.text
    assert "status=finished" in r.text
    assert 'small retry"' in r.text
    assert "plretry" in r.text
    assert 'small pause"' in r.text and 'small resume"' in r.text
    assert "plpause" in r.text and "plresume" in r.text
    # row and playlist action buttons post to /api/jobs/<id>/<action> and /api/playlists/<id>/<action>
    assert "/api/jobs/${encodeURIComponent(actBtn.dataset.id)}/${action}" in r.text
    assert "/api/playlists/${encodeURIComponent(actBtn.dataset.id)}/${action}" in r.text
    assert 'id="status"' in r.text and "<th>Size</th>" in r.text
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
    assert r.json() == {"detail": "URL is not a playlist"}
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
    assert r.json() == {"yt_dlp_version": yt_dlp.version.__version__, "downloads": {"files": 0, "bytes": 0}, "queue_paused": False, "rate_limit": None,
                        "impersonate": {"target": "chrome", "active": downloader_mod.impersonation_available()},
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
    monkeypatch.setattr(downloader_mod, "impersonation_available", lambda: False)
    assert client.get("/api/status").json()["impersonate"] == {"target": "chrome", "active": False}
    monkeypatch.setattr(downloader_mod, "impersonation_available", lambda: True)
    assert client.get("/api/status").json()["impersonate"] == {"target": "chrome", "active": True}
    settings.impersonate = None
    assert client.get("/api/status").json()["impersonate"] == {"target": None, "active": False}
