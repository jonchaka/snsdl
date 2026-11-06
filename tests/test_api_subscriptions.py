import pytest

from app import subscriptions as subs_mod
from app import worker as worker_mod
from app.jobs import DONE
from tests.api_helpers import CHANNEL, CSRF, PL


# -- round 9: subscriptions -----------------------------------------------------------

@pytest.fixture
def checks(monkeypatch):
    """Record check_one calls instead of expanding anything."""
    import threading
    calls = []
    called = threading.Event()

    def fake(sub, subs, store, pool, settings, expander=None, blocking=True):
        calls.append({"id": sub.id, "blocking": blocking})
        called.set()
        return {"queued": 2, "error": None}

    monkeypatch.setattr(subs_mod, "check_one", fake)
    return calls, called


def test_subscriptions_add_list_remove(client, checks):
    calls, called = checks
    assert client.get("/api/subscriptions").json() == {"subscriptions": []}
    r = client.post("/api/subscriptions", headers=CSRF,
                    json={"url": CHANNEL, "format": "mp4", "quality": "720", "limit": 5})
    assert r.status_code == 201
    entry = r.json()
    assert entry["url"] == CHANNEL and entry["limit"] == 5 and entry["enabled"] is True
    assert entry["include_existing"] is False and entry["seen_count"] == 0 and "seen" not in entry
    assert called.wait(5)   # the first check ran in the background
    assert calls == [{"id": entry["id"], "blocking": True}]
    listed = client.get("/api/subscriptions").json()["subscriptions"]
    assert [s["id"] for s in listed] == [entry["id"]] and "seen" not in listed[0]
    assert client.delete(f"/api/subscriptions/{entry['id']}").status_code == 403
    assert client.delete(f"/api/subscriptions/{entry['id']}", headers=CSRF).status_code == 204
    assert client.delete(f"/api/subscriptions/{entry['id']}", headers=CSRF).status_code == 404
    assert client.get("/api/subscriptions").json() == {"subscriptions": []}


def test_subscription_defaults(client, checks):
    entry = client.post("/api/subscriptions", headers=CSRF, json={"url": CHANNEL, "format": "mp3",
                                                                  "quality": "320"}).json()
    assert entry["limit"] == 10 and entry["subtitles"] is None and entry["title"] is None


@pytest.mark.parametrize("body", [
    {"url": "not a url"}, {"url": "ftp://x/y"}, {"url": "https://"},
    {"url": CHANNEL, "format": "mkv"}, {"url": CHANNEL, "format": "mp3", "quality": "1080"},
    {"url": CHANNEL, "limit": 0}, {"url": CHANNEL, "limit": 101},
    {"url": CHANNEL, "format": "mp3", "quality": "320", "subtitles": "en"},
    {"url": CHANNEL, "subtitles": "-x"}, {"url": CHANNEL, "subtitles": "all"},
])
def test_subscription_add_rejects_invalid(client, checks, body):
    assert client.post("/api/subscriptions", headers=CSRF, json=body).status_code == 422
    assert client.get("/api/subscriptions").json() == {"subscriptions": []}


def test_subscription_add_requires_header_and_disk(client, checks, monkeypatch):
    assert client.post("/api/subscriptions", json={"url": CHANNEL}).status_code == 403
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: True)
    assert client.post("/api/subscriptions", headers=CSRF, json={"url": CHANNEL}).status_code == 507
    assert client.get("/api/subscriptions").json() == {"subscriptions": []}


def test_subscription_check_now(client, checks, monkeypatch):
    calls, called = checks
    sub = client.app.state.subscriptions.add(CHANNEL, "mp4", "best")
    assert client.post(f"/api/subscriptions/{sub.id}/check").status_code == 403
    assert client.post("/api/subscriptions/nope/check", headers=CSRF).status_code == 404
    r = client.post(f"/api/subscriptions/{sub.id}/check", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"queued": 2, "error": None}
    assert calls == [{"id": sub.id, "blocking": False}]
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: True)
    assert client.post(f"/api/subscriptions/{sub.id}/check", headers=CSRF).status_code == 507


def test_subscription_check_while_running_is_409(client, monkeypatch):
    def busy(*a, **kw):
        raise subs_mod.CheckRunning()

    monkeypatch.setattr(subs_mod, "check_one", busy)
    sub = client.app.state.subscriptions.add(CHANNEL, "mp4", "best")
    r = client.post(f"/api/subscriptions/{sub.id}/check", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"] == "A check is already running"


def test_subscription_check_real_lock_is_409(client, monkeypatch):
    sub = client.app.state.subscriptions.add(CHANNEL, "mp4", "best")
    assert subs_mod.CHECK_LOCK.acquire(blocking=False)
    try:
        assert client.post(f"/api/subscriptions/{sub.id}/check", headers=CSRF).status_code == 409
    finally:
        subs_mod.CHECK_LOCK.release()


def test_subscription_check_queues_jobs_end_to_end(client, monkeypatch):
    monkeypatch.setattr(subs_mod, "expander", lambda url, s: PL)
    sub = client.app.state.subscriptions.add(CHANNEL, "mp4", "best", include_existing=True, limit=2)
    r = client.post(f"/api/subscriptions/{sub.id}/check", headers=CSRF)
    assert r.json() == {"queued": 2, "error": None}
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 2 and all(j["subscription_id"] == sub.id for j in jobs)


def test_subscription_pause_resume(client):
    sub = client.app.state.subscriptions.add(CHANNEL, "mp4", "best")
    assert client.post(f"/api/subscriptions/{sub.id}/pause").status_code == 403
    for action in ("pause", "resume"):
        assert client.post(f"/api/subscriptions/nope/{action}", headers=CSRF).status_code == 404
    r = client.post(f"/api/subscriptions/{sub.id}/pause", headers=CSRF)
    assert r.status_code == 200 and r.json()["enabled"] is False and "seen" not in r.json()
    r = client.post(f"/api/subscriptions/{sub.id}/resume", headers=CSRF)
    assert r.status_code == 200 and r.json()["enabled"] is True


def test_subscription_auto_keep_needs_keep_dir(client, checks):
    r = client.post("/api/subscriptions", headers=CSRF, json={"url": CHANNEL, "auto_keep": True})
    assert r.status_code == 422 and r.json() == {"detail": "keep automatically needs KEEP_DIR"}
    assert client.get("/api/subscriptions").json() == {"subscriptions": []}


def test_subscription_auto_keep_with_keep_dir(client, checks, keep_dir):
    r = client.post("/api/subscriptions", headers=CSRF, json={"url": CHANNEL, "auto_keep": True})
    assert r.status_code == 201 and r.json()["auto_keep"] is True
    plain = client.post("/api/subscriptions", headers=CSRF, json={"url": CHANNEL + "2"}).json()
    assert plain["auto_keep"] is False


# -- round 10: podcast feed ------------------------------------------------------------

def _feed_setup(client, settings):
    from app import auth, feed
    subs = client.app.state.subscriptions
    sub = subs.add(CHANNEL, "mp3", "best")
    other = subs.add("https://www.youtube.com/@other", "mp3", "best")
    store = client.app.state.store
    mine = store.add("https://youtu.be/m1", format="mp3", subscription_id=sub.id, thumb="thumbkey1")
    (settings.downloads_dir / "mine.mp3").write_bytes(b"ID3data")
    store.update(mine.id, status=DONE, filename="mine.mp3", title="Mine", finished_at="2026-11-05T10:00:00+00:00")
    theirs = store.add("https://youtu.be/t1", format="mp3", subscription_id=other.id, thumb="thumbkey2")
    (settings.downloads_dir / "theirs.mp3").write_bytes(b"ID3other")
    store.update(theirs.id, status=DONE, filename="theirs.mp3", title="Theirs")
    (settings.thumbs_dir / "thumbkey1.jpg").write_bytes(b"\xff\xd8jpg")
    (settings.thumbs_dir / "thumbkey2.jpg").write_bytes(b"\xff\xd8jpg2")
    key = feed.feed_key(auth.secret_for(settings), sub.id)
    return sub, other, key


def test_subscriptions_carry_their_feed_path(client, checks, settings):
    from app import auth, feed
    entry = client.post("/api/subscriptions", headers=CSRF, json={"url": CHANNEL}).json()
    key = feed.feed_key(auth.secret_for(settings), entry["id"])
    assert entry["feed"] == f"/feed/{entry['id']}/{key}.xml" and entry["auto_keep"] is False
    listed = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert listed["feed"] == entry["feed"]
    r = client.post(f"/api/subscriptions/{entry['id']}/pause", headers=CSRF)
    assert r.json()["feed"] == entry["feed"]


def test_feed_document_with_the_right_key(client, settings):
    sub, _, key = _feed_setup(client, settings)
    r = client.get(f"/feed/{sub.id}/{key}.xml", headers={"host": "box.local:8180"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/rss+xml; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert f"http://box.local:8180/feed/{sub.id}/{key}/mine.mp3".encode() in r.content
    assert f"/feed/{sub.id}/{key}/thumbs/thumbkey1".encode() in r.content
    assert b"theirs.mp3" not in r.content


def test_feed_uses_public_url_as_base(client, settings):
    sub, _, key = _feed_setup(client, settings)
    settings.public_url = "https://dl.example"
    assert b"https://dl.example/feed/" in client.get(f"/feed/{sub.id}/{key}.xml").content


def test_feed_wrong_key_or_unknown_subscription_is_404(client, settings):
    sub, other, key = _feed_setup(client, settings)
    assert client.get(f"/feed/{sub.id}/{'0' * 32}.xml").status_code == 404
    assert client.get(f"/feed/{other.id}/{key}.xml").status_code == 404   # key of another feed
    assert client.get(f"/feed/nope/{key}.xml").status_code == 404
    assert client.get(f"/feed/{sub.id}/{'0' * 32}/mine.mp3").status_code == 404
    assert client.get(f"/feed/{sub.id}/{'0' * 32}/thumbs/thumbkey1").status_code == 404
    assert client.get(f"/feed/{sub.id}/{key}é.xml").status_code == 404


def test_feed_serves_only_its_own_files(client, settings):
    sub, _, key = _feed_setup(client, settings)
    r = client.get(f"/feed/{sub.id}/{key}/mine.mp3")
    assert r.status_code == 200 and r.content == b"ID3data"
    assert r.headers["content-type"] == "audio/mpeg" and r.headers["cache-control"] == "no-store"
    assert client.get(f"/feed/{sub.id}/{key}/theirs.mp3").status_code == 404
    assert client.get(f"/feed/{sub.id}/{key}/missing.mp3").status_code == 404


def test_feed_thumbs_only_of_its_own_jobs(client, settings):
    sub, _, key = _feed_setup(client, settings)
    r = client.get(f"/feed/{sub.id}/{key}/thumbs/thumbkey1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert client.get(f"/feed/{sub.id}/{key}/thumbs/thumbkey2").status_code == 404
    assert client.get(f"/feed/{sub.id}/{key}/thumbs/bad!key").status_code == 404
