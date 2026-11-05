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
