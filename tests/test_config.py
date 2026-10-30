from pathlib import Path

from app.config import Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.data_dir == Path("/data")
    assert s.max_concurrent == 2
    assert s.cleanup_cron == "0 3 * * *"
    assert s.tz == "UTC"
    assert s.cookies_file == Path("/data/config/cookies.txt")
    assert s.downloads_dir == Path("/data/downloads")
    assert s.tmp_dir == Path("/data/tmp")
    assert s.jobs_file == Path("/data/jobs.json")


def test_overrides(tmp_path):
    s = Settings.from_env({
        "DATA_DIR": str(tmp_path),
        "MAX_CONCURRENT": "4",
        "CLEANUP_CRON": "30 4 * * *",
        "TZ": "Asia/Tokyo",
        "COOKIES_FILE": "/elsewhere/c.txt",
    })
    assert s.data_dir == tmp_path
    assert s.max_concurrent == 4
    assert s.cleanup_cron == "30 4 * * *"
    assert s.tz == "Asia/Tokyo"
    assert s.cookies_file == Path("/elsewhere/c.txt")


def test_ensure_dirs(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    s.ensure_dirs()
    assert s.downloads_dir.is_dir()
    assert s.tmp_dir.is_dir()
    assert s.cookies_file.parent.is_dir()
