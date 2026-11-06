import io
import json
import stat
import zipfile

import pytest

from app import backup
from app.history import HistoryStore
from app.state import AppState
from app.subscriptions import SubscriptionStore

COOKIES = ".example.com\tTRUE\t/\tFALSE\t0\tsid\tsecret-value\n"
SUB = {"id": "s1", "url": "https://www.youtube.com/@c", "format": "mp4", "quality": "best"}
ENTRY = {"id": "h1", "url": "https://youtu.be/a", "title": "A"}


class FakePool:
    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


@pytest.fixture
def stores(settings):
    subs, history, state = (SubscriptionStore(settings.subscriptions_file), HistoryStore(settings.history_file),
                            AppState(settings.state_file))
    for s in (subs, history, state):
        s.load()
    return subs, history, state


def _zip(members: dict, manifest=None) -> bytes:
    if manifest is None:
        manifest = {"app": "sns-downloader", "version": 1, "created_at": "2026-11-06T00:00:00+00:00",
                    "files": list(members)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if manifest is not False:
            zf.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _good(**extra) -> dict:
    members = {"subscriptions.json": json.dumps([SUB]), "history.json": json.dumps([ENTRY, {**ENTRY, "id": "h2"}]),
               "state.json": json.dumps({"queue_paused": True}), "config/cookies/k1.txt": COOKIES}
    members.update(extra)
    return members


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _restore(settings, stores, data, pool=None):
    subs, history, state = stores
    return backup.restore_backup(settings, data, subs, history, state, pool)


# -- make_backup -----------------------------------------------------------------------------

def test_backup_holds_the_store_files_cookies_and_a_manifest(settings):
    settings.subscriptions_file.write_text(json.dumps([SUB]))
    settings.history_file.write_text("[]")
    settings.cookies_file.write_text(COOKIES)
    kept = settings.data_dir / "config" / "cookies"
    kept.mkdir(parents=True)
    (kept / "abc.txt").write_text("# Netscape HTTP Cookie File\n" + COOKIES)
    (kept / "not an id.txt").write_text(COOKIES)   # not a kept entry: left out
    zf = zipfile.ZipFile(io.BytesIO(backup.make_backup(settings)))
    names = zf.namelist()
    assert names == ["manifest.json", "subscriptions.json", "history.json", "state.json",
                     "config/cookies.txt", "config/cookies/abc.txt"]
    assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["app"] == "sns-downloader" and manifest["version"] == 1 and manifest["created_at"]
    assert manifest["files"] == names[1:]
    assert json.loads(zf.read("subscriptions.json")) == [SUB]
    assert json.loads(zf.read("state.json")) == {"queue_paused": False}   # missing file: the default
    assert zf.read("config/cookies.txt").decode() == COOKIES
    assert "secret-value" in zf.read("config/cookies/abc.txt").decode()


def test_backup_too_large(settings, monkeypatch):
    monkeypatch.setattr(backup, "MAX_BACKUP", 100)
    settings.history_file.write_text("[" + " " * 200 + "]")
    with pytest.raises(backup.BackupTooLarge):
        backup.make_backup(settings)


def test_backup_round_trip(settings, stores):
    settings.subscriptions_file.write_text(json.dumps([SUB]))
    settings.history_file.write_text(json.dumps([ENTRY]))
    settings.cookies_file.write_text(COOKIES)
    data = backup.make_backup(settings)
    for p in (settings.subscriptions_file, settings.history_file, settings.cookies_file):
        p.unlink()
    assert _restore(settings, stores, data) == {"subscriptions": 1, "history": 1, "cookies": 1}
    assert stores[0].get("s1").url == SUB["url"] and settings.cookies_file.is_file()


# -- restore_backup --------------------------------------------------------------------------

def test_restore_replaces_and_reloads(settings, stores):
    subs, history, state = stores
    subs.add("https://www.youtube.com/@old", "mp4", "best")
    kept = settings.data_dir / "config" / "cookies"
    kept.mkdir(parents=True)
    (kept / "old.txt").write_text(COOKIES)
    pool = FakePool()
    counts = _restore(settings, stores, _zip(_good()), pool)
    assert counts == {"subscriptions": 1, "history": 2, "cookies": 1}
    assert [s.id for s in subs.list()] == ["s1"]
    assert [e.id for e in history.list()] == ["h1", "h2"]
    assert state.queue_paused is True and pool.paused is True
    assert sorted(p.name for p in kept.iterdir()) == ["k1.txt"]   # old kept cookies gone
    restored = kept / "k1.txt"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert restored.read_text().startswith("# Netscape HTTP Cookie File\n") and "secret-value" in restored.read_text()
    assert not list(settings.data_dir.rglob("*.restore.tmp"))


def test_restore_resumes_the_pool_when_the_backup_is_not_paused(settings, stores):
    pool = FakePool()
    pool.pause()
    _restore(settings, stores, _zip(_good(**{"state.json": "{}"})), pool)
    assert pool.paused is False and stores[2].queue_paused is False


def test_restore_legacy_cookies_file_is_private(settings, stores):
    _restore(settings, stores, _zip(_good(**{"config/cookies.txt": COOKIES})))
    assert stat.S_IMODE(settings.cookies_file.stat().st_mode) == 0o600


def _hostile_zip(name, data=b"x"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        members = _good()
        manifest = {"app": "sns-downloader", "version": 1, "files": [*members, name]}
        zf.writestr("manifest.json", json.dumps(manifest))
        for n, d in members.items():
            zf.writestr(n, d)
        zf.writestr(name, data)
    return buf.getvalue()


def _too_many():
    members = _good()
    members.update({f"config/cookies/c{i}.txt": COOKIES for i in range(backup.MAX_MEMBERS)})
    return _zip(members)


def _oversized():
    return _zip(_good(**{"history.json": "[" + " " * backup.MAX_MEMBER + "]"}))


@pytest.mark.parametrize("data, reason", [
    (b"not a zip at all", "not a zip file"),
    (b"", "not a zip file"),
    (_hostile_zip("../subscriptions.json"), "unexpected file"),
    (_hostile_zip("config/cookies/../../x.txt"), "unexpected file"),
    (_hostile_zip("/etc/passwd"), "unexpected file"),
    (_hostile_zip("jobs.json"), "unexpected file"),
    (_hostile_zip("config/cookies/"), "unexpected file"),
    (_hostile_zip("config/cookies/a.b.txt"), "unexpected file"),
    (_zip(_good(), manifest=False), "no manifest.json"),
    (_zip(_good(), manifest={"app": "sns-downloader", "version": 2, "files": list(_good())}), "version"),
    (_zip(_good(), manifest={"app": "other", "version": 1, "files": list(_good())}), "not an sns-downloader"),
    (_zip(_good(), manifest={"app": "sns-downloader", "version": 1, "files": []}), "does not list"),
    (_zip({k: v for k, v in _good().items() if k != "history.json"}), "no history.json"),
    (_zip(_good(**{"subscriptions.json": "{"})), "not valid JSON"),
    (_zip(_good(**{"subscriptions.json": "{}"})), "must be a list"),
    (_zip(_good(**{"subscriptions.json": json.dumps([{"id": "s1"}])})), "missing required fields"),
    (_zip(_good(**{"subscriptions.json": json.dumps([{**SUB, "seen": "x"}])})), "bad seen"),
    (_zip(_good(**{"subscriptions.json": json.dumps([SUB, SUB])})), "repeats id"),
    (_zip(_good(**{"history.json": json.dumps([1])})), "not an object"),
    (_zip(_good(**{"history.json": json.dumps([{"id": 5, "url": "u"}])})), "bad id or url"),
    (_zip(_good(**{"state.json": json.dumps({"queue_paused": "yes"})})), "queue_paused"),
    (_zip(_good(**{"state.json": json.dumps({"other": 1})})), "state.json must be"),
    (_zip(_good(**{"config/cookies/k1.txt": "garbage\n"})), "config/cookies/k1.txt: line 1"),
    (_zip(_good(**{"config/cookies.txt": b"\xff\xfe"})), "not a text file"),
    (_too_many(), "more than 200 files"),
    (_oversized(), "larger than 5 MB"),
])
def test_restore_rejects_and_writes_nothing(settings, stores, data, reason):
    settings.subscriptions_file.write_text(json.dumps([{**SUB, "id": "keep-me"}]))
    kept = settings.data_dir / "config" / "cookies"
    kept.mkdir(parents=True, exist_ok=True)
    (kept / "old.txt").write_text(COOKIES)
    before = _snapshot(settings.data_dir)
    with pytest.raises(backup.BackupError) as info:
        _restore(settings, stores, data, FakePool())
    assert reason in str(info.value)
    assert _snapshot(settings.data_dir) == before
    assert not (settings.data_dir.parent / "x.txt").exists()


def test_restore_rejects_a_body_over_the_cap(settings, stores, monkeypatch):
    monkeypatch.setattr(backup, "MAX_BACKUP", 10)
    with pytest.raises(backup.BackupError, match="larger than"):
        _restore(settings, stores, _zip(_good()))


def test_backup_error_is_a_value_error():
    assert issubclass(backup.BackupError, ValueError)
