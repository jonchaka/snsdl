import json
import threading

import pytest

from app import subscriptions as subs_mod
from app import worker as worker_mod
from app.jobs import DONE, DOWNLOADING, FAILED, QUEUED
from app.probe import ProbeError
from app.subscriptions import (CHECK_LOCK, SEEN_CAP, CheckRunning, Subscription,
                               SubscriptionStore, check_all, check_one)


class FakePool:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, job_id):
        self.enqueued.append(job_id)


@pytest.fixture
def subs(settings):
    s = SubscriptionStore(settings.subscriptions_file)
    s.load()
    return s


@pytest.fixture
def pool():
    return FakePool()


@pytest.fixture(autouse=True)
def _disk_ok(monkeypatch):
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: False)


def _expander(urls, title="Chan"):
    calls = []

    def fn(url, settings):
        calls.append(url)
        return {"title": title, "total": len(urls),
                "entries": [{"url": u, "title": u.rsplit("/", 1)[-1]} for u in urls]}
    fn.calls = calls
    return fn


def _urls(*names):
    return [f"https://example.com/{n}" for n in names]


# -- store -----------------------------------------------------------------------

def test_add_defaults(subs):
    sub = subs.add("https://example.com/c", "mp4", "720", None, 10, False)
    assert isinstance(sub, Subscription)
    assert len(sub.id) == 8 and int(sub.id, 16) >= 0
    assert sub.title is None and sub.enabled is True and sub.include_existing is False
    assert sub.limit == 10 and sub.last_checked is None and sub.last_error is None
    assert sub.last_queued == 0 and sub.seen == [] and sub.added_at
    assert subs.get(sub.id) is sub and subs.list() == [sub]


def test_store_round_trip(settings, subs):
    a = subs.add("https://example.com/a", "mp3", "320", None, 5, True)
    b = subs.add("https://example.com/b", "mp4", "best", "en", 10, False)
    subs.update(b.id, seen=_urls("x", "y"), title="B", enabled=False)
    again = SubscriptionStore(settings.subscriptions_file)
    again.load()
    assert [s.id for s in again.list()] == [a.id, b.id]
    got = again.get(b.id)
    assert got.seen == _urls("x", "y") and got.title == "B" and got.enabled is False
    assert got.subtitles == "en"
    assert again.get(a.id).include_existing is True and again.get(a.id).limit == 5


def test_save_is_atomic(settings, subs, monkeypatch):
    subs.add("https://example.com/a", "mp4", "best", None, 10, False)
    before = settings.subscriptions_file.read_text()
    from pathlib import Path

    def boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        subs.add("https://example.com/b", "mp4", "best", None, 10, False)
    assert settings.subscriptions_file.read_text() == before   # old file intact


def test_load_missing_and_malformed(settings, subs, caplog):
    assert subs.list() == []
    settings.subscriptions_file.write_text("{not json")
    s = SubscriptionStore(settings.subscriptions_file)
    s.load()
    assert s.list() == []
    assert settings.subscriptions_file.with_suffix(".json.bad").exists()


def test_load_ignores_unknown_keys(settings):
    settings.subscriptions_file.write_text(json.dumps([{
        "id": "abcd1234", "url": "https://example.com/a", "format": "mp4", "quality": "best",
        "added_at": "2026-11-06T00:00:00+00:00", "future_field": 1}]))
    s = SubscriptionStore(settings.subscriptions_file)
    s.load()
    assert s.get("abcd1234").url == "https://example.com/a"


def test_update_and_remove(subs):
    sub = subs.add("https://example.com/a", "mp4", "best", None, 10, False)
    assert subs.update(sub.id, enabled=False).enabled is False
    assert subs.update("nope", enabled=False) is None
    with pytest.raises(AttributeError):
        subs.update(sub.id, bogus=1)
    assert subs.remove(sub.id) is True
    assert subs.remove(sub.id) is False and subs.get(sub.id) is None


def test_to_dict_public_drops_seen(subs):
    sub = subs.add("https://example.com/a", "mp4", "best", None, 10, False)
    subs.update(sub.id, seen=_urls("x", "y", "z"))
    d = sub.to_dict()
    assert "seen" not in d and d["seen_count"] == 3 and d["id"] == sub.id
    assert sub.to_dict(public=False)["seen"] == _urls("x", "y", "z")


# -- check_one ---------------------------------------------------------------------

def test_first_check_seeds_without_queueing(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "720", None, 10, False)
    exp = _expander(_urls("c3", "c2", "c1"))
    res = check_one(sub, subs, store, pool, settings, expander=exp)
    assert res == {"queued": 0, "error": None}
    assert store.list() == [] and pool.enqueued == []
    sub = subs.get(sub.id)
    assert set(sub.seen) == set(_urls("c1", "c2", "c3"))
    assert sub.title == "Chan" and sub.last_checked and sub.last_queued == 0
    assert exp.calls == ["https://example.com/c"]


def test_later_check_queues_only_new_entries(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "720", "en", 10, False)
    check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c2", "c1")))
    res = check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c4", "c3", "c2", "c1")))
    assert res == {"queued": 2, "error": None}
    jobs = sorted(store.list(), key=lambda j: j.playlist_index)
    assert [j.url for j in jobs] == _urls("c4", "c3")
    for j in jobs:
        assert j.format == "mp4" and j.quality == "720" and j.subtitles == "en"
        assert j.playlist == "Chan" and j.playlist_id == f"sub-{sub.id}"
        assert j.subscription_id == sub.id and j.status == QUEUED
    assert [j.playlist_index for j in jobs] == [1, 2]
    assert jobs[0].title == "c4"
    assert pool.enqueued == [j.id for j in jobs]
    sub = subs.get(sub.id)
    assert sub.last_queued == 2 and sub.last_error is None
    assert set(sub.seen) == set(_urls("c1", "c2", "c3", "c4"))
    assert sub.seen[-1] == "https://example.com/c4"     # newest last
    # A third check with nothing new queues nothing.
    assert check_one(sub, subs, store, pool, settings,
                     expander=_expander(_urls("c4", "c3", "c2", "c1")))["queued"] == 0
    assert subs.get(sub.id).last_queued == 0


def test_include_existing_queues_on_first_check_up_to_limit(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp3", "192", None, 2, True)
    res = check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c3", "c2", "c1")))
    assert res["queued"] == 2
    assert {j.url for j in store.list()} == set(_urls("c3", "c2"))
    # Every current url is seen, including the one beyond the limit.
    assert set(subs.get(sub.id).seen) == set(_urls("c1", "c2", "c3"))


def test_limit_applies_to_later_checks(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 1, False)
    check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")))
    res = check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c4", "c3", "c2", "c1")))
    assert res["queued"] == 1 and [j.url for j in store.list()] == _urls("c4")


@pytest.mark.parametrize("status", [QUEUED, DOWNLOADING, "paused"])
def test_dedupes_against_active_jobs(subs, store, pool, settings, status):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    existing = store.add("https://example.com/c2", "mp4", "best")
    store.update(existing.id, status=status)
    res = check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c2", "c1")))
    assert res["queued"] == 1
    assert sorted(j.url for j in store.list()) == _urls("c1", "c2")
    assert pool.enqueued != [] and existing.id not in pool.enqueued


def test_finished_job_of_same_url_does_not_block(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    old = store.add("https://example.com/c1", "mp4", "best")
    store.update(old.id, status=DONE)
    assert check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")))["queued"] == 1


def test_active_job_with_other_format_does_not_block(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp3", "best", None, 10, True)
    store.add("https://example.com/c1", "mp4", "best")
    assert check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")))["queued"] == 1


def test_duplicate_entries_queue_once(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    res = check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1", "c1", "c2")))
    assert res["queued"] == 2


def test_probe_error_sets_last_error(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)

    def bad(url, settings):
        raise ProbeError("URL is not a playlist")

    res = check_one(sub, subs, store, pool, settings, expander=bad)
    assert res == {"queued": 0, "error": "URL is not a playlist"}
    sub = subs.get(sub.id)
    assert sub.last_error == "URL is not a playlist" and sub.seen == []
    assert store.list() == [] and pool.enqueued == []
    # A later successful check clears the error.
    check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")))
    assert subs.get(sub.id).last_error is None


def test_low_disk_queues_nothing(subs, store, pool, settings, monkeypatch):
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: True)
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    exp = _expander(_urls("c1"))
    res = check_one(sub, subs, store, pool, settings, expander=exp)
    assert res == {"queued": 0, "error": "low disk space"}
    assert subs.get(sub.id).last_error == "low disk space"
    assert store.list() == [] and exp.calls == []


def test_busy_lock_raises_check_running_when_not_blocking(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    with CHECK_LOCK:
        with pytest.raises(CheckRunning):
            check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")), blocking=False)
    assert not CHECK_LOCK.locked()
    assert check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")),
                     blocking=False)["queued"] == 1


def test_blocking_check_waits_for_the_lock(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    out = []
    CHECK_LOCK.acquire()
    try:
        t = threading.Thread(target=lambda: out.append(
            check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")))))
        t.start()
        t.join(0.1)
        assert t.is_alive() and out == []
    finally:
        CHECK_LOCK.release()
    t.join(5)
    assert out == [{"queued": 1, "error": None}]


def test_lock_released_after_an_unexpected_error(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)

    def bad(url, settings):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        check_one(sub, subs, store, pool, settings, expander=bad)
    assert not CHECK_LOCK.locked()


def test_seen_is_capped(subs, store, pool, settings, monkeypatch):
    monkeypatch.setattr(subs_mod, "SEEN_CAP", 5)
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, False)
    subs.update(sub.id, seen=_urls("o1", "o2", "o3", "o4"), seeded=True,
                last_checked="2026-11-06T00:00:00+00:00")
    check_one(sub, subs, store, pool, settings, expander=_expander(_urls("n3", "n2", "n1")))
    assert subs.get(sub.id).seen == _urls("o3", "o4", "n1", "n2", "n3")


def test_seen_cap_value():
    assert SEEN_CAP == 5000


def test_uses_module_level_expander(subs, store, pool, settings, monkeypatch):
    exp = _expander(_urls("c1"))
    monkeypatch.setattr(subs_mod, "expander", exp)
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    assert check_one(sub, subs, store, pool, settings)["queued"] == 1 and exp.calls


def test_check_of_a_removed_subscription_queues_nothing(subs, store, pool, settings):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    subs.remove(sub.id)
    exp = _expander(_urls("c1"))
    assert check_one(sub, subs, store, pool, settings, expander=exp) == {"queued": 0, "error": None}
    assert store.list() == []


# -- check_all ---------------------------------------------------------------------

def test_check_all_continues_after_a_failure(subs, store, pool, settings, monkeypatch):
    a = subs.add("https://example.com/a", "mp4", "best", None, 10, True)
    b = subs.add("https://example.com/b", "mp4", "best", None, 10, True)
    c = subs.add("https://example.com/c", "mp4", "best", None, 10, True)
    subs.update(c.id, enabled=False)

    def exp(url, settings):
        if url.endswith("/a"):
            raise RuntimeError("unexpected")
        return {"title": "B", "total": 1, "entries": [{"url": url + "/1", "title": "1"}]}

    monkeypatch.setattr(subs_mod, "expander", exp)
    res = check_all(subs, store, pool, settings)
    assert res == {"checked": 2, "queued": 1, "failed": 1}
    assert [j.url for j in store.list()] == ["https://example.com/b/1"]
    assert subs.get(a.id).last_error and subs.get(c.id).last_checked is None


def test_check_all_never_raises_even_if_listing_fails(subs, store, pool, settings, monkeypatch):
    def broken():
        raise RuntimeError("x")

    monkeypatch.setattr(subs, "list", broken)
    assert check_all(subs, store, pool, settings) == {"checked": 0, "queued": 0, "failed": 0}


# -- review fixes ------------------------------------------------------------------

@pytest.mark.parametrize("first", ["probe_error", "low_disk"])
def test_failed_first_check_then_success_only_seeds(subs, store, pool, settings, monkeypatch, first):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, False)
    if first == "probe_error":
        def bad(url, settings):
            raise ProbeError("temporarily unavailable")
        check_one(sub, subs, store, pool, settings, expander=bad)
    else:
        monkeypatch.setattr(worker_mod, "disk_low", lambda settings: True)
        check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c1")))
        monkeypatch.setattr(worker_mod, "disk_low", lambda settings: False)
    after_fail = subs.get(sub.id)
    assert after_fail.last_checked and after_fail.seeded is False
    res = check_one(sub, subs, store, pool, settings, expander=_expander(_urls("c3", "c2", "c1")))
    assert res == {"queued": 0, "error": None}
    assert store.list() == [] and pool.enqueued == []
    sub = subs.get(sub.id)
    assert sub.seeded is True and set(sub.seen) == set(_urls("c1", "c2", "c3"))


def test_check_all_error_path_does_not_seed(subs, store, pool, settings, monkeypatch):
    sub = subs.add("https://example.com/c", "mp4", "best", None, 10, False)

    def boom(url, settings):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(subs_mod, "expander", boom)
    check_all(subs, store, pool, settings)
    assert subs.get(sub.id).last_checked and subs.get(sub.id).seeded is False
    monkeypatch.setattr(subs_mod, "expander", _expander(_urls("c1", "c2")))
    check_all(subs, store, pool, settings)
    assert store.list() == [] and subs.get(sub.id).seeded is True


def test_seeded_defaults_false_for_older_files(settings):
    settings.subscriptions_file.parent.mkdir(parents=True, exist_ok=True)
    settings.subscriptions_file.write_text(json.dumps([
        {"id": "ab12cd34", "url": "https://example.com/c", "format": "mp4", "quality": "best"}]))
    s = SubscriptionStore(settings.subscriptions_file)
    s.load()
    assert s.get("ab12cd34").seeded is False
    assert "seeded" in s.get("ab12cd34").to_dict(public=False)


@pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (1, 1), (100, 100), (101, 100), (10_000, 100)])
def test_limit_is_clamped_on_add_and_update(subs, given, expected):
    sub = subs.add("https://example.com/c", "mp4", "best", None, given, False)
    assert sub.limit == expected
    assert subs.update(sub.id, limit=given).limit == expected


def test_load_starts_empty_when_the_bad_file_cannot_be_moved(settings, monkeypatch, caplog):
    import logging
    from pathlib import Path

    settings.subscriptions_file.parent.mkdir(parents=True, exist_ok=True)
    settings.subscriptions_file.write_text("{not json")

    def no_replace(self, target):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "replace", no_replace)
    s = SubscriptionStore(settings.subscriptions_file)
    with caplog.at_level(logging.WARNING, logger="app.subscriptions"):
        s.load()
    assert s.list() == []
    assert "could not move" in caplog.text


def test_auto_keep_defaults_off_and_round_trips(settings, subs):
    plain = subs.add("https://example.com/a", "mp4", "best")
    kept = subs.add("https://example.com/b", "mp4", "best", auto_keep=True)
    assert plain.auto_keep is False and kept.to_dict()["auto_keep"] is True
    again = SubscriptionStore(settings.subscriptions_file)
    again.load()
    assert again.get(kept.id).auto_keep is True and again.get(plain.id).auto_keep is False
