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
