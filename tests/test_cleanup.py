import os

import pytest

from app import cleanup
from app.jobs import DONE


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
