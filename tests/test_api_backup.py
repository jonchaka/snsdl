import io
import json
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import backup as backup_mod
from app import subscriptions as subs_mod
from app.config import Settings
from app.main import create_app
from tests.api_helpers import CSRF

COOKIES = ".example.com\tTRUE\t/\tFALSE\t0\tsid\tsecret-value\n"
SUB = {"id": "s1", "url": "https://www.youtube.com/@c", "format": "mp4", "quality": "best"}


@pytest.fixture(autouse=True)
def no_checks(monkeypatch):
    """Adding a subscription starts a first check in the background; it would hold CHECK_LOCK."""
    monkeypatch.setattr(subs_mod, "check_one", lambda *a, **k: {"queued": 0, "error": None})


def _backup(subs=(SUB,), history=({"id": "h1", "url": "https://youtu.be/a", "title": "A"},),
            paused=True, cookies=("k1",)) -> bytes:
    members = {"subscriptions.json": json.dumps(list(subs)), "history.json": json.dumps(list(history)),
               "state.json": json.dumps({"queue_paused": paused})}
    members.update({f"config/cookies/{c}.txt": COOKIES for c in cookies})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"app": "sns-downloader", "version": 1, "files": list(members)}))
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _post(client, data: bytes, headers=CSRF):
    return client.post("/api/restore", headers=headers, files={"file": ("b.zip", data, "application/zip")})


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_backup_download(client, settings):
    client.post("/api/subscriptions", headers=CSRF, json={"url": "https://www.youtube.com/@x"})
    r = client.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["cache-control"] == "no-store"
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="sns-backup-') and cd.endswith('.zip"')
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "manifest.json" in zf.namelist()
    assert json.loads(zf.read("subscriptions.json"))[0]["url"] == "https://www.youtube.com/@x"


def test_backup_too_large_is_507(client, monkeypatch):
    monkeypatch.setattr(backup_mod, "MAX_BACKUP", 5)
    r = client.get("/api/backup")
    assert r.status_code == 507 and "larger than" in r.json()["detail"]


def test_restore_replaces_and_reloads(client, settings):
    client.post("/api/subscriptions", headers=CSRF, json={"url": "https://www.youtube.com/@old"})
    kept = settings.data_dir / "config" / "cookies"
    kept.mkdir(parents=True, exist_ok=True)
    (kept / "old.txt").write_text(COOKIES)
    r = _post(client, _backup())
    assert r.status_code == 200 and r.json() == {"subscriptions": 1, "history": 1, "cookies": 1}
    assert [s["id"] for s in client.get("/api/subscriptions").json()["subscriptions"]] == ["s1"]
    history = client.get("/api/history").json()
    assert "h1" in json.dumps(history)
    assert client.app.state.pool.paused is True and client.app.state.app_state.queue_paused is True
    assert sorted(p.name for p in kept.iterdir()) == ["k1.txt"]
    assert stat.S_IMODE((kept / "k1.txt").stat().st_mode) == 0o600
    r = _post(client, _backup(paused=False))
    assert r.status_code == 200 and client.app.state.pool.paused is False


def test_restore_round_trip_through_the_api(client):
    client.post("/api/subscriptions", headers=CSRF, json={"url": "https://www.youtube.com/@x"})
    data = client.get("/api/backup").content
    sub_id = client.get("/api/subscriptions").json()["subscriptions"][0]["id"]
    client.delete(f"/api/subscriptions/{sub_id}", headers=CSRF)
    assert client.get("/api/subscriptions").json()["subscriptions"] == []
    assert _post(client, data).json()["subscriptions"] == 1
    assert client.get("/api/subscriptions").json()["subscriptions"][0]["id"] == sub_id


def test_restore_accepts_a_raw_zip_body(client):
    r = client.post("/api/restore", headers={**CSRF, "Content-Type": "application/zip"}, content=_backup())
    assert r.status_code == 200 and r.json()["subscriptions"] == 1


def test_restore_needs_the_csrf_header(client, settings):
    before = _snapshot(settings.data_dir)
    assert _post(client, _backup(), headers={}).status_code == 403
    assert _snapshot(settings.data_dir) == before


@pytest.mark.parametrize("data, reason", [
    (b"not a zip", "not a zip file"),
    (_backup(cookies=("../../evil",)), "unexpected file"),
    (_backup(subs=({"id": "x"},)), "missing required fields"),
])
def test_restore_rejects_with_422_and_changes_nothing(client, settings, data, reason):
    client.post("/api/subscriptions", headers=CSRF, json={"url": "https://www.youtube.com/@old"})
    before = _snapshot(settings.data_dir)
    r = _post(client, data)
    assert r.status_code == 422 and reason in r.json()["detail"]
    assert _snapshot(settings.data_dir) == before
    assert len(client.get("/api/subscriptions").json()["subscriptions"]) == 1


def test_restore_bad_cookie_file_is_422(client, settings):
    members = _backup()
    zf_in = zipfile.ZipFile(io.BytesIO(members))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in zf_in.namelist():
            zf.writestr(name, b"bad\n" if name.endswith(".txt") else zf_in.read(name))
    before = _snapshot(settings.data_dir)
    r = _post(client, buf.getvalue())
    assert r.status_code == 422 and "config/cookies/k1.txt" in r.json()["detail"]
    assert _snapshot(settings.data_dir) == before


def test_restore_without_a_file_field_is_422(client):
    r = client.post("/api/restore", headers=CSRF, files={"other": ("b.zip", _backup(), "application/zip")})
    assert r.status_code == 422 and "file" in r.json()["detail"]


def test_restore_over_the_upload_cap_is_422(client, monkeypatch):
    monkeypatch.setattr(backup_mod, "MAX_BACKUP", 100)
    monkeypatch.setattr("app.api_backup.MULTIPART_SLACK", 0)
    r = _post(client, _backup())
    assert r.status_code == 422 and "larger than" in r.json()["detail"]


def test_restore_is_409_during_a_subscription_check(client, settings):
    before = _snapshot(settings.data_dir)
    assert subs_mod.CHECK_LOCK.acquire(blocking=False)
    try:
        r = _post(client, _backup())
    finally:
        subs_mod.CHECK_LOCK.release()
    assert r.status_code == 409
    assert _snapshot(settings.data_dir) == before
    assert subs_mod.CHECK_LOCK.acquire(blocking=False)   # the route released nothing it did not take
    subs_mod.CHECK_LOCK.release()


def test_restore_releases_the_check_lock(client):
    _post(client, b"junk")
    _post(client, _backup())
    assert subs_mod.CHECK_LOCK.acquire(blocking=False)
    subs_mod.CHECK_LOCK.release()


def test_backup_and_restore_need_the_pin(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path), "ACCESS_PIN": "4321"})
    s.ensure_dirs()
    with TestClient(create_app(s, run_background=False)) as c:
        assert c.get("/api/backup").status_code == 401
        assert _post(c, _backup()).status_code == 401
