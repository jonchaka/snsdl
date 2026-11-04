from pathlib import Path

import pytest

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
    assert s.state_file == Path("/data/state.json")
    assert s.lib_dir == Path("/data/lib")
    assert s.update_cron == "0 4 * * *"
    assert s.rate_limit is None


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


@pytest.mark.parametrize("bad", ["0", "abc", "-1", ""])
def test_max_concurrent_must_be_positive_int(bad):
    with pytest.raises(ValueError, match="MAX_CONCURRENT"):
        Settings.from_env({"MAX_CONCURRENT": bad})


def test_max_playlist_items_default_and_override(tmp_path):
    assert Settings.from_env({}).max_playlist_items == 500
    assert Settings.from_env({"MAX_PLAYLIST_ITEMS": "20"}).max_playlist_items == 20


@pytest.mark.parametrize("bad", ["0", "x", "-5", ""])
def test_max_playlist_items_must_be_positive_int(bad):
    with pytest.raises(ValueError, match="MAX_PLAYLIST_ITEMS"):
        Settings.from_env({"MAX_PLAYLIST_ITEMS": bad})


def test_embed_thumbnails_default_and_parsing():
    assert Settings.from_env({}).embed_thumbnails is True
    for raw in ("1", "true", "YES", "on"):
        assert Settings.from_env({"EMBED_THUMBNAILS": raw}).embed_thumbnails is True
    for raw in ("0", "false", "No", "off", ""):
        assert Settings.from_env({"EMBED_THUMBNAILS": raw}).embed_thumbnails is False


def test_embed_thumbnails_rejects_garbage():
    with pytest.raises(ValueError, match="EMBED_THUMBNAILS"):
        Settings.from_env({"EMBED_THUMBNAILS": "maybe"})


def test_update_cron_override_and_empty_disables():
    assert Settings.from_env({"UPDATE_CRON": "30 5 * * *"}).update_cron == "30 5 * * *"
    assert Settings.from_env({"UPDATE_CRON": ""}).update_cron == ""


def test_ensure_dirs_does_not_create_lib_dir(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    s.ensure_dirs()
    assert not s.lib_dir.exists()


@pytest.mark.parametrize("raw, expected", [
    ("", None), ("  ", None),
    ("500000", 500000), ("5M", 5 * 1024 ** 2), ("5m", 5 * 1024 ** 2), ("800K", 800 * 1024),
    ("1.5M", int(1.5 * 1024 ** 2)), ("1G", 1024 ** 3), ("2MB", 2 * 1024 ** 2), (" 3 M ", 3 * 1024 ** 2),
])
def test_rate_limit_parsing(raw, expected):
    assert Settings.from_env({"RATE_LIMIT": raw}).rate_limit == expected


@pytest.mark.parametrize("raw", ["fast", "0", "-5M", "5T", "5 MBps", "M"])
def test_rate_limit_rejects_garbage(raw):
    with pytest.raises(ValueError, match="RATE_LIMIT"):
        Settings.from_env({"RATE_LIMIT": raw})
