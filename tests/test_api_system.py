import json

import pytest
from fastapi.testclient import TestClient

from app import downloader as downloader_mod
from app import probe as probe_mod
from app import tls as tls_mod
from app import worker as worker_mod
from app.jobs import DONE, DOWNLOADING, FAILED, QUEUED, Job
from app.main import create_app
from tests.api_helpers import (CHANNEL, COOKIES, CSRF, LOW_DISK_DETAIL, V, _done_job, _done_with_sidecars,
                               _failed_job, _history, _make_disk_low, _thumb)


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    page = r.text + client.get("/app.js").text   # markup in index.html, script in app.js
    assert "SNS Downloader" in page
    assert 'id="options"' in page
    assert 'id="quality"' in page
    assert "Cancel" in page
    assert 'id="plbox"' in page
    assert 'id="plcheck"' in page
    assert 'id="pllimit"' in page
    assert "/api/playlists" in page
    assert 'class="jobs"' in page   # downloads table (playlist groups render inside it)
    assert "plcancel" in page
    assert "/api/playlists/" in page
    assert "plremove" in page
    assert "status=finished" in page
    for call in ("actionButton('retry', 'retry', 'Retry'", "actionButton('pause', 'pause', 'Pause'",
                 "actionButton('resume', 'play', 'Resume'", "actionButton('addcookies', 'cookie', 'Add cookies'",
                 "actionButton('learnhow', 'help', 'Learn how'", "j.keep ? 'unkeep' : 'keep'",
                 "actionButton('remove', finished ? 'trash' : 'x', finished ? 'Remove' : 'Cancel'"):
        assert call in page, call
    assert "plretry" in page and "plpause" in page and "plresume" in page and "plzip" in page
    # row and playlist action buttons post to /api/jobs/<id>/<action> and /api/playlists/<id>/<action>
    assert "/api/jobs/${encodeURIComponent(actBtn.dataset.id)}/${action}" in page
    assert "/api/playlists/${encodeURIComponent(plAct.dataset.id)}/${action}" in page
    assert 'id="status"' in page and 'class="progress-text"' in page and 'class="fmt"' in page
    assert 'id="queuetoggle"' in page and 'id="queuebanner"' in page and "/api/queue/" in page
    assert 'id="update"' in page and "/api/update" in page


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
    assert r.json() == {"title": "Vid", "heights": [1080, 720], "previous": None}
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


def test_probe_response_passes_playlist_through(client, monkeypatch):
    monkeypatch.setattr(probe_mod, "probe",
                        lambda url, s: {"title": "V", "heights": [720],
                                        "playlist": {"title": "PL", "count": 2, "total": 3}})
    r = client.post("/api/probe", json={"url": "https://x/p"}, headers=CSRF)
    assert r.json()["playlist"] == {"title": "PL", "count": 2, "total": 3}


# -- status --------------------------------------------------------------------

def test_status_reports_downloads_and_yt_dlp_version(client, settings):
    import yt_dlp
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    body.pop("disk"), body.pop("proxy")   # covered by test_status_reports_disk_and_proxy
    body.pop("pacing"), body.pop("keep_dir")   # covered by test_status_reports_pacing_and_keep_dir
    body.pop("https"), body.pop("cert_hosts")   # covered by test_status_https_fields_*
    for key in ("window", "webhook", "subscriptions", "defaults"):   # round 9 tests below
        body.pop(key)
    for key in ("retention_days", "live_max_hours", "recordings", "media_layout", "public_url",
                "history"):   # round 10 tests below
        body.pop(key)
    body.pop("images")   # round 11 test below
    body.pop("telegram")   # round 12 test below
    assert body == {"yt_dlp_version": yt_dlp.version.__version__, "downloads": {"files": 0, "bytes": 0},
                    "queue_paused": False, "rate_limit": None,
                    "impersonate": {"target": "chrome", "active": downloader_mod.target_supported("chrome")},
                    "update": {"installed": None, "restart_pending": False, "last_checked": None, "error": None,
                               "note": None}}
    (settings.downloads_dir / "a.mp4").write_bytes(b"x" * 10)
    (settings.downloads_dir / "b.mp3").write_bytes(b"y" * 5)
    (settings.downloads_dir / "sub").mkdir()
    assert client.get("/api/status").json()["downloads"] == {"files": 2, "bytes": 15}


def test_status_tolerates_files_vanishing_mid_scan(client, settings, monkeypatch):
    import os
    from app import api_system as api_system_mod
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

    monkeypatch.setattr(api_system_mod.os, "scandir", Scan)
    assert client.get("/api/status").json()["downloads"] == {"files": 1, "bytes": 10}
    assert api_system_mod.downloads_usage(settings.downloads_dir / "missing") == {"files": 0, "bytes": 0}


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
    assert body["update"] == {"installed": None, "restart_pending": False, "last_checked": None, "error": None,
                              "note": None}
    assert body["yt_dlp_version"] == yt_dlp.version.__version__


def test_status_reports_rate_limit(client, settings):
    assert client.get("/api/status").json()["rate_limit"] is None
    settings.rate_limit = 1024
    assert client.get("/api/status").json()["rate_limit"] == 1024


def test_cleanup_endpoint_spares_kept_jobs(client, settings):
    kid = _done_job(client, settings, "https://youtu.be/k1", "k1.mp4")
    _done_job(client, settings, "https://youtu.be/k2", "k2.mp4")
    client.post(f"/api/jobs/{kid}/keep", headers=CSRF)
    r = client.post("/api/cleanup", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"removed": 1}
    assert [j["id"] for j in client.get("/api/jobs").json()] == [kid]
    assert [p.name for p in settings.downloads_dir.iterdir()] == ["k1.mp4"]


def test_status_reports_impersonation(client, settings, monkeypatch):
    monkeypatch.setattr(downloader_mod, "target_supported", lambda t: False)
    assert client.get("/api/status").json()["impersonate"] == {"target": "chrome", "active": False}
    monkeypatch.setattr(downloader_mod, "target_supported", lambda t: t is not None)
    assert client.get("/api/status").json()["impersonate"] == {"target": "chrome", "active": True}
    settings.impersonate = None
    assert client.get("/api/status").json()["impersonate"] == {"target": None, "active": False}


# -- cookies ----------------------------------------------------------------------

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
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": "nope"}, headers=CSRF)
    assert r.status_code == 404

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


def test_human_sizes():
    from app.api import _human
    assert _human(600 * 1024 ** 2) == "600.0 MB"
    assert _human(1024 ** 3) == "1.0 GB"
    assert _human(512) == "512.0 B"
    assert _human(1536) == "1.5 KB"


def test_cookies_for_a_failed_job_507_when_disk_is_low_but_plain_cookies_are_stored(client, monkeypatch):
    jid = _failed_job(client)
    _make_disk_low(monkeypatch)
    r = client.post("/api/cookies", json={"text": COOKIES, "keep": False, "job_id": jid}, headers=CSRF)
    assert r.status_code == 507 and r.json() == {"detail": LOW_DISK_DETAIL}
    assert client.app.state.store.get(jid).status == FAILED
    assert client.get("/api/cookies", headers=CSRF).json() == {"entries": []}
    assert client.post("/api/cookies", json={"text": COOKIES, "keep": True}, headers=CSRF).status_code == 201


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


# --- round 8: HTTPS certificate download and status ----------------------------------------

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
    assert "sns-v2" in r.text
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
    assert '<link rel="stylesheet" href="/app.css">' in r.text
    assert '<script src="/app.js" defer></script>' in r.text
    assert "navigator.serviceWorker.register('/sw.js')" in client.get("/app.js").text


# -- round 9: search -----------------------------------------------------------------

def test_search_returns_results(client, monkeypatch):
    seen = []

    def fake(q, s):
        seen.append(q)
        return [{"url": "https://www.youtube.com/watch?v=a", "title": "A", "duration": 5, "uploader": "U",
                 "thumb": None}]

    monkeypatch.setattr(probe_mod, "search", fake)
    r = client.post("/api/search", headers=CSRF, json={"q": "  lofi beats  "})
    assert r.status_code == 200 and r.json()["results"][0]["title"] == "A"
    assert seen == ["lofi beats"]


def test_search_requires_header(client, monkeypatch):
    monkeypatch.setattr(probe_mod, "search", lambda q, s: [])
    assert client.post("/api/search", json={"q": "x"}).status_code == 403


@pytest.mark.parametrize("q", ["", "   ", "x" * 201, " " * 150 + "x" * 201, "x" * 401])
def test_search_rejects_bad_query(client, monkeypatch, q):
    monkeypatch.setattr(probe_mod, "search", lambda q, s: pytest.fail("must not search"))
    assert client.post("/api/search", headers=CSRF, json={"q": q}).status_code == 422


def test_search_strips_before_the_200_char_limit(client, monkeypatch):
    seen = []
    monkeypatch.setattr(probe_mod, "search", lambda q, s: seen.append(q) or [])
    r = client.post("/api/search", headers=CSRF, json={"q": " " * 150 + "x" * 200 + " " * 50})
    assert r.status_code == 200 and seen == ["x" * 200]


def test_search_accepts_200_chars(client, monkeypatch):
    monkeypatch.setattr(probe_mod, "search", lambda q, s: [])
    assert client.post("/api/search", headers=CSRF, json={"q": "x" * 200}).json() == {"results": []}


def test_search_probe_error_is_422(client, monkeypatch):
    def boom(q, s):
        raise probe_mod.ProbeError("YouTube said no")

    monkeypatch.setattr(probe_mod, "search", boom)
    r = client.post("/api/search", headers=CSRF, json={"q": "x"})
    assert r.status_code == 422 and r.json()["detail"] == "YouTube said no"


# -- round 9: status ------------------------------------------------------------------

def test_status_round9_defaults(client):
    body = client.get("/api/status").json()
    assert body["window"] is None and body["webhook"] is False
    assert body["subscriptions"] == {"count": 0, "cron": "0 */6 * * *"}
    assert body["defaults"] == {"sponsorblock": False, "prefer_h264": False}


def test_status_round9_configured(client, settings, monkeypatch):
    from datetime import datetime, timezone
    from app.window import parse_window
    settings.download_window = parse_window("01:00-07:00")
    settings.webhook_url = "https://hooks.example/x"
    settings.webhook_secret = "s3cret"
    settings.subscription_cron = ""
    settings.sponsorblock = settings.prefer_h264 = True
    client.app.state.subscriptions.add(CHANNEL, "mp4", "best")
    monkeypatch.setattr(worker_mod, "now_fn", lambda s: datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc))
    r = client.get("/api/status")
    body = r.json()
    assert body["window"] == {"from": "01:00", "to": "07:00", "active": False,
                              "next_start": "2026-11-07T01:00:00+00:00"}
    assert body["webhook"] is True
    assert body["subscriptions"] == {"count": 1, "cron": None}
    assert body["defaults"] == {"sponsorblock": True, "prefer_h264": True}
    assert "s3cret" not in r.text and "hooks.example" not in r.text
    monkeypatch.setattr(worker_mod, "now_fn", lambda s: datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc))
    assert client.get("/api/status").json()["window"]["active"] is True


def test_cleanup_keeps_kept_sidecars_and_removes_the_rest(client, settings):
    kept = _done_with_sidecars(client, settings, "https://youtu.be/k", "k")
    _done_with_sidecars(client, settings, "https://youtu.be/g", "g")
    client.app.state.store.update(kept, keep=True)
    assert client.post("/api/cleanup", headers=CSRF).status_code == 200
    assert sorted(p.name for p in settings.downloads_dir.iterdir()) == ["k.en.srt", "k.mp4"]


# -- round 10: history and pending-webhook wiring ------------------------------------------

def test_create_app_wires_history_and_pending_into_the_pool(settings):
    from app import webhook
    from app.history import HistoryStore
    app = create_app(settings, run_background=False)
    pool = app.state.pool
    assert isinstance(app.state.history, HistoryStore) and pool.history is app.state.history
    assert pool.subs is app.state.subscriptions
    assert isinstance(pool.pending, webhook.PendingStore)


def test_startup_sweep_keeps_thumbs_used_by_history(settings):
    import os
    import time
    from app.history import HistoryStore
    history = HistoryStore(settings.history_file)
    history.record([Job(id="dn000002", url="https://youtu.be/h", status=DONE, thumb="histKEY123",
                        finished_at="2026-11-06T00:00:00+00:00")])
    hist, orphan = _thumb(settings, "histKEY123"), _thumb(settings, "orphKEY124")
    old = time.time() - 7200
    for p in (hist, orphan):
        os.utime(p, (old, old))
    with TestClient(create_app(settings, run_background=False)) as c:
        assert c.app.state.history.thumbs_in_use() == {"histKEY123"}
    assert hist.exists() and not orphan.exists()


def test_probe_reports_the_previous_download(client, monkeypatch):
    monkeypatch.setattr(probe_mod, "probe", lambda url, s: {"title": "Vid"})
    _history(client, ("https://youtu.be/abc", "Old title"))
    r = client.post("/api/probe", json={"url": "https://youtu.be/abc"}, headers=CSRF)
    assert r.json()["previous"] == {"finished_at": "2026-11-01T10:00:00+00:00", "format": "mp4",
                                    "quality": "best", "title": "Old title"}
    r = client.post("/api/probe", json={"url": "https://youtu.be/other"}, headers=CSRF)
    assert r.json()["previous"] is None


# -- round 10: status and events --------------------------------------------------------

def test_status_round10_fields(client):
    body = client.get("/api/status").json()
    assert body["retention_days"] is None and body["live_max_hours"] == 12
    assert body["recordings"] == {"active": 0, "max": 3}
    assert body["media_layout"] is False and body["public_url"] is None
    assert body["history"] == {"count": 0}
    _history(client, ("https://youtu.be/a", "A"))
    assert client.get("/api/status").json()["history"] == {"count": 1}


def test_events_route_streams_jobs_and_status_with_headers(settings, monkeypatch):
    """Drive the ASGI app directly: TestClient waits for a streaming body to end."""
    import asyncio
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: False)
    app = create_app(settings, run_background=False)
    app.state.store.add(V)

    async def run():
        messages, gone = [], asyncio.Event()
        state = {"sent": False}

        async def receive():
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            if body.count(b"\n\n") >= 2:
                gone.set()

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/api/events", "raw_path": b"/api/events", "query_string": b"",
                 "root_path": "", "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 1),
                 "server": ("testserver", 80)}
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
        return messages

    messages = asyncio.run(run())
    start = messages[0]
    assert start["status"] == 200
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store" and headers["x-accel-buffering"] == "no"
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    first, second = body.split(b"\n\n")[:2]
    assert first.startswith(b"event: jobs\ndata: [") and V.encode() in first
    assert second.startswith(b"event: status\ndata: {") and b'"history":{"count":0}' in second


# -- round 11: photo posts ---------------------------------------------------------------

def test_status_lists_the_photo_post_sites(client):
    from app import images
    sites = client.get("/api/status").json()["images"]["sites"]
    assert sites == sorted(images.SITES) and "pinterest" in sites


def test_telegram_test_route_404_when_unconfigured(client):
    assert client.post("/api/telegram/test", headers=CSRF).status_code == 404
    assert client.get("/api/status").json()["telegram"] is False


def test_telegram_test_route_requires_csrf(client, settings):
    settings.telegram_bot_token, settings.telegram_chat_id = "1:" + "a" * 30, "42"
    assert client.post("/api/telegram/test").status_code == 403


@pytest.mark.parametrize("ok", [True, False])
def test_telegram_test_route_sends_a_message(client, settings, monkeypatch, ok):
    from app import telegram as telegram_mod
    token = "1:" + "a" * 30
    settings.telegram_bot_token, settings.telegram_chat_id = token, "42"
    seen = []

    class _Resp:
        status = 200 if ok else 401

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=None):
        seen.append(req)
        return _Resp()

    monkeypatch.setattr(telegram_mod, "_OPENER", type("O", (), {"open": staticmethod(fake_open)})())
    r = client.post("/api/telegram/test", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"ok": ok}
    [req] = seen
    assert req.full_url == f"https://api.telegram.org/bot{token}/sendMessage"
    assert telegram_mod.TEST_TEXT.encode() in req.data and b"42" in req.data
    status = client.get("/api/status").json()
    assert status["telegram"] is True
    assert token not in json.dumps(status)
