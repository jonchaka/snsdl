import os

import pytest

from app import cleanup
from app.jobs import DONE, FAILED


def test_empty_dir_removes_files_and_subdirs(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.part").write_bytes(b"b")
    assert cleanup.empty_dir(tmp_path) == 2
    assert list(tmp_path.iterdir()) == []


def test_empty_dir_missing_path_is_zero(tmp_path):
    assert cleanup.empty_dir(tmp_path / "nope") == 0


def test_run_cleanup_removes_files_and_clears_jobs(settings, store):
    (settings.downloads_dir / "x.mp4").write_bytes(b"x")
    (settings.downloads_dir / "y.mp4").write_bytes(b"y")
    (settings.tmp_dir / "z.mp4.part").write_bytes(b"z")
    job = store.add("https://example.com/v")
    store.update(job.id, status=DONE, filename="x.mp4")

    removed = cleanup.run_cleanup(store, settings)

    assert removed == 3
    assert list(settings.downloads_dir.iterdir()) == []
    assert list(settings.tmp_dir.iterdir()) == []
    assert store.list() == []


def test_build_scheduler_registers_nightly_job(settings, store):
    sched = cleanup.build_scheduler(store, settings)
    job = sched.get_job("nightly-cleanup")
    assert job is not None
    assert "hour='3'" in str(job.trigger)
    assert "minute='0'" in str(job.trigger)
    assert job.args == (store, settings) or list(job.args) == [store, settings]
    assert job.kwargs == {"keep_active": True}   # the nightly run keeps active jobs


def test_build_scheduler_honours_custom_cron_and_tz(settings, store):
    settings.cleanup_cron = "15 4 * * *"
    settings.tz = "Asia/Tokyo"
    sched = cleanup.build_scheduler(store, settings)
    trig = str(sched.get_job("nightly-cleanup").trigger)
    assert "hour='4'" in trig and "minute='15'" in trig
    assert str(sched.get_job("nightly-cleanup").trigger.timezone) == "Asia/Tokyo"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_empty_dir_survives_undeletable_entry(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f.mp4").write_bytes(b"f")
    os.chmod(locked, 0o500)
    try:
        assert cleanup.empty_dir(locked) == 0
        assert (locked / "f.mp4").exists()
    finally:
        os.chmod(locked, 0o700)


def test_scheduler_has_misfire_grace(settings, store):
    sched = cleanup.build_scheduler(store, settings)
    assert sched.get_job(cleanup.JOB_ID).misfire_grace_time == 3600


def test_scheduler_adds_update_job_only_when_cron_set(settings, store):
    from app.cleanup import build_scheduler

    async def noop():
        pass

    sched = build_scheduler(store, settings, update_job=noop)
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"nightly-cleanup", "yt_dlp_update"}
    assert "hour='4'" in str(sched.get_job("yt_dlp_update").trigger)
    settings.update_cron = ""
    sched2 = build_scheduler(store, settings, update_job=noop)
    assert "yt_dlp_update" not in {j.id for j in sched2.get_jobs()}
    sched3 = build_scheduler(store, settings)
    assert "yt_dlp_update" not in {j.id for j in sched3.get_jobs()}


def test_empty_dir_protects_named_entries(tmp_path):
    (tmp_path / "keep.mp4").write_bytes(b"k")
    (tmp_path / "gone.mp4").write_bytes(b"g")
    assert cleanup.empty_dir(tmp_path, frozenset({"keep.mp4"})) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["keep.mp4"]


def test_run_cleanup_spares_kept_done_jobs_and_their_files(settings, store):
    (settings.downloads_dir / "kept.mp4").write_bytes(b"k")
    (settings.downloads_dir / "plain.mp4").write_bytes(b"p")
    (settings.tmp_dir / "kept.mp4").write_bytes(b"t")        # same name in tmp is NOT protected
    kept = store.add("https://example.com/k")
    store.update(kept.id, status=DONE, filename="kept.mp4", keep=True)
    plain = store.add("https://example.com/p")
    store.update(plain.id, status=DONE, filename="plain.mp4")
    failed_kept = store.add("https://example.com/f")
    store.update(failed_kept.id, status=FAILED, keep=True)    # keep only protects done jobs

    removed = cleanup.run_cleanup(store, settings)

    assert removed == 2
    assert [p.name for p in settings.downloads_dir.iterdir()] == ["kept.mp4"]
    assert list(settings.tmp_dir.iterdir()) == []
    assert [j.id for j in store.list()] == [kept.id]
    assert store.get(kept.id).keep is True


def test_is_kept_requires_done_and_flag():
    from app.jobs import Job
    assert cleanup.is_kept(Job(id="a", url="u", status=DONE, keep=True))
    assert not cleanup.is_kept(Job(id="a", url="u", status=DONE, keep=False))
    assert not cleanup.is_kept(Job(id="a", url="u", status=FAILED, keep=True))


def test_run_cleanup_deletes_thumbs_of_removed_jobs_and_sweeps_old_orphans(settings, store):
    import time
    thumbs_dir = settings.thumbs_dir
    for key in ("keptKEY123", "goneKEY123", "orphKEY123", "younKEY123"):
        (thumbs_dir / f"{key}.jpg").write_bytes(b"\xff\xd8\xff")
    old = time.time() - 7200
    os.utime(thumbs_dir / "orphKEY123.jpg", (old, old))
    os.utime(thumbs_dir / "keptKEY123.jpg", (old, old))
    kept = store.add("https://example.com/k", thumb="keptKEY123")
    store.update(kept.id, status=DONE, keep=True)
    store.add("https://example.com/g", thumb="goneKEY123")

    cleanup.run_cleanup(store, settings)

    # kept job keeps its (old) thumb; removed job's young thumb goes; old orphan swept; young orphan stays
    assert sorted(p.name for p in thumbs_dir.iterdir()) == ["keptKEY123.jpg", "younKEY123.jpg"]


def test_run_cleanup_keeps_a_thumb_shared_with_a_kept_job(settings, store):
    (settings.thumbs_dir / "sharKEY123.jpg").write_bytes(b"\xff\xd8\xff")
    kept = store.add("https://example.com/v", thumb="sharKEY123")
    store.update(kept.id, status=DONE, keep=True)
    store.add("https://example.com/v", format="mp3", thumb="sharKEY123")

    cleanup.run_cleanup(store, settings)

    assert (settings.thumbs_dir / "sharKEY123.jpg").exists()
    assert [j.thumb for j in store.list()] == ["sharKEY123"]


def test_run_cleanup_leaves_a_sibling_keep_dir_untouched(tmp_path):
    from app.config import Settings
    from app.jobs import JobStore
    settings = Settings.from_env({"DATA_DIR": str(tmp_path), "KEEP_DIR": str(tmp_path / "keep")})
    settings.ensure_dirs()
    store = JobStore(settings.jobs_file)
    store.load()
    (settings.keep_dir / "kept.mp4").write_bytes(b"k")
    (settings.downloads_dir / "plain.mp4").write_bytes(b"p")
    cleanup.run_cleanup(store, settings)
    assert list(settings.downloads_dir.iterdir()) == []
    assert (settings.keep_dir / "kept.mp4").read_bytes() == b"k"


def test_scheduler_adds_subscription_job_only_with_a_cron(settings, store):
    from app.cleanup import SUBSCRIPTIONS_JOB_ID, build_scheduler

    async def noop():
        pass

    assert SUBSCRIPTIONS_JOB_ID == "subscriptions"
    settings.subscription_cron = "0 */6 * * *"
    settings.tz = "Asia/Tokyo"
    sched = build_scheduler(store, settings, subscription_job=noop)
    job = sched.get_job("subscriptions")
    assert job is not None and job.func is noop and job.misfire_grace_time == 3600
    assert "hour='*/6'" in str(job.trigger) and str(job.trigger.timezone) == "Asia/Tokyo"
    settings.subscription_cron = ""
    assert build_scheduler(store, settings, subscription_job=noop).get_job("subscriptions") is None
    settings.subscription_cron = "0 */6 * * *"
    assert build_scheduler(store, settings).get_job("subscriptions") is None


def test_create_app_wires_subscriptions(settings, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from app import main, subscriptions
    from app.subscriptions import SubscriptionStore

    SubscriptionStore(settings.subscriptions_file).add("https://example.com/c", "mp4", "best")
    captured = {}
    real = main.build_scheduler

    def spy(store, se, update_job=None, subscription_job=None):
        captured["job"] = subscription_job
        return real(store, se, update_job=update_job, subscription_job=subscription_job)

    monkeypatch.setattr(main, "build_scheduler", spy)
    calls = []
    monkeypatch.setattr(subscriptions, "check_all", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(main.Updater, "check", lambda self: None)
    app = main.create_app(settings, run_background=True)
    subs = app.state.subscriptions
    assert isinstance(subs, SubscriptionStore) and len(subs.list()) == 1   # loaded
    with TestClient(app):
        assert captured["job"] is not None
    # The scheduled job runs check_all in a thread with the app's objects.
    asyncio.run(captured["job"]())
    assert calls == [(subs, app.state.store, app.state.pool, settings)]


def _active_and_finished(settings, store):
    from app.jobs import DOWNLOADING, PAUSED, QUEUED
    jobs = {}
    for status in (QUEUED, PAUSED, DOWNLOADING, DONE, FAILED):
        job = store.add(f"https://example.com/{status}")
        store.update(job.id, status=status, subscription_id="sub1" if status == QUEUED else None)
        jobs[status] = job
    store.update(jobs[DONE].id, filename="done.mp4")
    (settings.downloads_dir / "done.mp4").write_bytes(b"d")
    dl = jobs[DOWNLOADING].id
    (settings.tmp_dir / f"{dl}-Title.f137.mp4.part").write_bytes(b"p")
    (settings.tmp_dir / f"cookies-{dl}.txt").write_bytes(b"c")
    (settings.tmp_dir / f"cookies-once-{jobs[QUEUED].id}.txt").write_bytes(b"o")
    (settings.tmp_dir / "stray.part").write_bytes(b"s")
    (settings.tmp_dir / f"{jobs[FAILED].id}-Old.part").write_bytes(b"f")
    return jobs


def test_run_cleanup_keep_active_spares_queued_paused_downloading(settings, store):
    from app.jobs import DOWNLOADING, PAUSED, QUEUED
    jobs = _active_and_finished(settings, store)
    dl, q = jobs[DOWNLOADING].id, jobs[QUEUED].id

    cleanup.run_cleanup(store, settings, keep_active=True)

    assert {j.id for j in store.list()} == {jobs[s].id for s in (QUEUED, PAUSED, DOWNLOADING)}
    assert store.get(q).subscription_id == "sub1"
    assert sorted(p.name for p in settings.tmp_dir.iterdir()) == sorted(
        [f"{dl}-Title.f137.mp4.part", f"cookies-{dl}.txt", f"cookies-once-{q}.txt"])
    assert list(settings.downloads_dir.iterdir()) == []      # not-kept done file still goes


def test_run_cleanup_default_still_clears_active_jobs(settings, store):
    _active_and_finished(settings, store)

    cleanup.run_cleanup(store, settings)

    assert store.list() == []
    assert list(settings.tmp_dir.iterdir()) == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_empty_dir_protects_prefixes(tmp_path):
    (tmp_path / "abc-1.part").write_bytes(b"x")
    (tmp_path / "abd-1.part").write_bytes(b"x")
    assert cleanup.empty_dir(tmp_path, protect_prefixes=("abc-",)) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["abc-1.part"]


def test_run_cleanup_keep_active_spares_a_file_just_moved_into_downloads(settings, store):
    """A running download can move its finished file into downloads/ after the store snapshot
    but before its job records the filename: the nightly run must not delete it."""
    from app.jobs import DOWNLOADING, FAILED
    jobs = _active_and_finished(settings, store)
    dl, failed = jobs[DOWNLOADING].id, jobs[FAILED].id
    (settings.downloads_dir / f"{dl}-Title.mp4").write_bytes(b"v")
    (settings.downloads_dir / f"{dl}-Title.en.srt").write_bytes(b"s")
    (settings.downloads_dir / f"{failed}-Old.mp4").write_bytes(b"f")

    cleanup.run_cleanup(store, settings, keep_active=True)

    assert sorted(p.name for p in settings.downloads_dir.iterdir()) == sorted(
        [f"{dl}-Title.mp4", f"{dl}-Title.en.srt"])


def test_run_cleanup_manual_still_removes_active_downloads(settings, store):
    from app.jobs import DOWNLOADING
    jobs = _active_and_finished(settings, store)
    (settings.downloads_dir / f"{jobs[DOWNLOADING].id}-Title.mp4").write_bytes(b"v")

    cleanup.run_cleanup(store, settings)

    assert list(settings.downloads_dir.iterdir()) == []


# -- round 10: retention -------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _old(path, days_ago):
    t = (NOW - timedelta(days=days_ago)).timestamp()
    os.utime(path, (t, t))


def test_run_retention_removes_old_finished_jobs_with_files(settings, store):
    settings.retention_days = 7
    d = settings.downloads_dir
    for name in ("old.mp4", "old.en.srt", "young.mp4", "kept.mp4", "oldfail-x.mp4"):
        (d / name).write_bytes(b"x")
        _old(d / name, 1)   # young files: only the jobs' age decides
    old = store.add("https://e.com/old")
    store.update(old.id, status=DONE, filename="old.mp4", sidecars=["old.en.srt"], finished_at=_iso(8))
    failed = store.add("https://e.com/f")
    store.update(failed.id, status=FAILED, finished_at=_iso(30))
    young = store.add("https://e.com/young")
    store.update(young.id, status=DONE, filename="young.mp4", finished_at=_iso(6))
    kept = store.add("https://e.com/k")
    store.update(kept.id, status=DONE, filename="kept.mp4", keep=True, finished_at=_iso(100))
    # No finished_at: the age falls back to created_at.
    legacy = store.add("https://e.com/legacy")
    store.update(legacy.id, status=DONE, created_at=_iso(9))
    active = {}
    for status in ("queued", "paused", "downloading"):
        j = store.add(f"https://e.com/{status}")
        store.update(j.id, status=status, created_at=_iso(50))
        active[status] = j.id

    removed = cleanup.run_retention(store, settings, now=NOW)

    ids = {j.id for j in store.list()}
    assert ids == {young.id, kept.id, *active.values()}
    assert sorted(p.name for p in d.iterdir()) == ["kept.mp4", "oldfail-x.mp4", "young.mp4"]
    assert removed == 3   # old, failed and legacy jobs; no orphan or tmp entry


def test_run_retention_default_now_is_utc_now(settings, store):
    settings.retention_days = 1
    j = store.add("https://e.com/x")
    store.update(j.id, status=DONE, finished_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    j2 = store.add("https://e.com/y")
    store.update(j2.id, status=DONE, finished_at=datetime.now(timezone.utc).isoformat())
    cleanup.run_retention(store, settings)
    assert [x.id for x in store.list()] == [j2.id]


def test_run_retention_orphan_files_by_mtime(settings, store):
    settings.retention_days = 7
    d = settings.downloads_dir
    for name in ("orphan-old.mp4", "orphan-young.mp4", "ref.mp4", "ref.en.srt"):
        (d / name).write_bytes(b"x")
    for name in ("orphan-old.mp4", "ref.mp4", "ref.en.srt"):
        _old(d / name, 10)
    _old(d / "orphan-young.mp4", 6)
    ref = store.add("https://e.com/r")
    store.update(ref.id, status=DONE, filename="ref.mp4", sidecars=["ref.en.srt"], finished_at=_iso(1))
    running = store.add("https://e.com/run")
    store.update(running.id, status="downloading")
    (d / f"{running.id}-file.mp4").write_bytes(b"x")   # being moved in by a running download
    _old(d / f"{running.id}-file.mp4", 10)

    cleanup.run_retention(store, settings, now=NOW)

    assert sorted(p.name for p in d.iterdir()) == sorted(
        ["orphan-young.mp4", "ref.mp4", "ref.en.srt", f"{running.id}-file.mp4"])


def test_run_retention_empties_tmp_but_protects_active_jobs(settings, store):
    settings.retention_days = 7
    t = settings.tmp_dir
    running = store.add("https://e.com/run")
    store.update(running.id, status="downloading")
    for name in ("stale.part", f"{running.id}-v.mp4.part", f"cookies-once-{running.id}.txt",
                 f"cookies-{running.id}.txt", "cookies-other.txt"):
        (t / name).write_bytes(b"x")

    cleanup.run_retention(store, settings, now=NOW)

    assert sorted(p.name for p in t.iterdir()) == sorted(
        [f"{running.id}-v.mp4.part", f"cookies-once-{running.id}.txt", f"cookies-{running.id}.txt"])


def test_run_retention_keeps_thumbnails_history_uses(settings, store, tmp_path):
    import time

    from app.history import HistoryStore
    settings.retention_days = 7
    th = settings.thumbs_dir
    for key in ("histKEY123", "goneKEY123", "orphKEY123", "hor2KEY123"):
        (th / f"{key}.jpg").write_bytes(b"\xff\xd8\xff")
        t = time.time() - 7200
        os.utime(th / f"{key}.jpg", (t, t))
    history = HistoryStore(tmp_path / "h.json")
    history.load()
    a = store.add("https://e.com/a", thumb="histKEY123")
    store.update(a.id, status=DONE, finished_at=_iso(10))
    history.record([store.get(a.id)])
    b = store.add("https://e.com/b", thumb="goneKEY123")
    store.update(b.id, status=DONE, finished_at=_iso(10))
    history.record([store.get(b.id)])
    history.remove(history.find("https://e.com/b").id)
    from app.jobs import Job
    history.record([Job(id="z", url="https://e.com/z", status=DONE, thumb="hor2KEY123")])

    cleanup.run_retention(store, settings, history=history, now=NOW)

    assert store.list() == []
    assert sorted(p.name for p in th.iterdir()) == ["histKEY123.jpg", "hor2KEY123.jpg"]


def test_sweep_and_delete_unused_thumbs_honour_history(settings, store, tmp_path):
    import time

    from app.history import HistoryStore
    from app.jobs import Job
    th = settings.thumbs_dir
    for key in ("histKEY123", "noneKEY123"):
        (th / f"{key}.jpg").write_bytes(b"\xff\xd8\xff")
        t = time.time() - 7200
        os.utime(th / f"{key}.jpg", (t, t))
    history = HistoryStore(tmp_path / "h.json")
    history.load()
    history.record([Job(id="z", url="u", status=DONE, thumb="histKEY123")])
    cleanup.delete_unused_thumbs(store, settings, [Job(id="z", url="u", thumb="histKEY123")], history)
    assert (th / "histKEY123.jpg").exists()
    assert cleanup.sweep_thumbs(store, settings, history) == 1
    assert sorted(p.name for p in th.iterdir()) == ["histKEY123.jpg"]
    cleanup.sweep_thumbs(store, settings)   # without history the key is unreferenced
    assert list(th.iterdir()) == []


def test_build_scheduler_runs_retention_only_when_configured(settings, store):
    sentinel = object()
    job = cleanup.build_scheduler(store, settings, history=sentinel).get_job(cleanup.JOB_ID)
    assert job.func is cleanup.run_cleanup and job.kwargs == {"keep_active": True}
    settings.retention_days = 14
    job = cleanup.build_scheduler(store, settings, history=sentinel).get_job(cleanup.JOB_ID)
    assert job.func is cleanup.run_retention
    assert list(job.args) == [store, settings]
    assert job.kwargs == {"history": sentinel}
    assert job.misfire_grace_time == 3600
    assert "hour='3'" in str(job.trigger)
