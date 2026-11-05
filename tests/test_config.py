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
    assert s.impersonate == "chrome"


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


@pytest.mark.parametrize("raw", ["fast", "0", "-5M", "5T", "5 MBps", "M", "B", "b", "0B", "0.0001", "0.5"])
def test_rate_limit_rejects_garbage(raw):
    with pytest.raises(ValueError, match="RATE_LIMIT"):
        Settings.from_env({"RATE_LIMIT": raw})


@pytest.mark.parametrize("raw, expected", [("", None), ("  ", None), ("chrome", "chrome"), ("safari", "safari"),
                                           ("chrome-120:windows-10", "chrome-120:windows-10"), (" edge:macos ", "edge:macos")])
def test_impersonate_parsing(raw, expected):
    assert Settings.from_env({"IMPERSONATE": raw}).impersonate == expected


@pytest.mark.parametrize("raw", ["chrome 120", "a:b:c", "chrome--1", "-chrome"])
def test_impersonate_rejects_garbage(raw):
    with pytest.raises(ValueError, match="IMPERSONATE"):
        Settings.from_env({"IMPERSONATE": raw})


def test_round6_defaults():
    s = Settings.from_env({})
    assert s.min_free_space == 1024 ** 3
    assert s.proxy is None
    assert s.proxy_display is None
    assert s.fragments == 1
    assert s.thumbs_dir == Path("/data/thumbs")


def test_ensure_dirs_creates_thumbs_dir(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    s.ensure_dirs()
    assert s.thumbs_dir.is_dir()


@pytest.mark.parametrize("raw, expected", [
    ("0", 0), (" 0 ", 0), ("0M", 0), ("500M", 500 * 1024 ** 2), ("1G", 1024 ** 3), ("2.5G", int(2.5 * 1024 ** 3)),
    ("1048576", 1048576), ("", 1024 ** 3),   # empty = default, like an unset variable
])
def test_min_free_space_parsing(raw, expected):
    assert Settings.from_env({"MIN_FREE_SPACE": raw}).min_free_space == expected


@pytest.mark.parametrize("raw", ["lots", "-1G", "5T", "0.5"])
def test_min_free_space_rejects_garbage(raw):
    with pytest.raises(ValueError, match="MIN_FREE_SPACE"):
        Settings.from_env({"MIN_FREE_SPACE": raw})


def test_parse_size_is_shared_with_rate_limit():
    from app import config
    assert config._parse_size("X", "5M") == 5 * 1024 ** 2
    with pytest.raises(ValueError, match="X"):
        config._parse_size("X", "nope")


@pytest.mark.parametrize("raw, expected, display", [
    ("", None, None), ("  ", None, None),
    ("http://proxy:3128", "http://proxy:3128", "http://proxy:3128"),
    ("https://proxy.example.com", "https://proxy.example.com", "https://proxy.example.com"),
    ("socks5://host:1080", "socks5://host:1080", "socks5://host:1080"),
    ("socks5h://user:secret@host:1080", "socks5h://user:secret@host:1080", "socks5h://host:1080"),
    ("socks4://10.0.0.1:1080", "socks4://10.0.0.1:1080", "socks4://10.0.0.1:1080"),
    ("socks4a://h:1", "socks4a://h:1", "socks4a://h:1"),
    (" HTTP://user@Proxy:8080 ", "HTTP://user@Proxy:8080", "http://proxy:8080"),
    ("http://[::1]:3128", "http://[::1]:3128", "http://[::1]:3128"),
])
def test_proxy_parsing(raw, expected, display):
    s = Settings.from_env({"PROXY": raw})
    assert s.proxy == expected
    assert s.proxy_display == display


@pytest.mark.parametrize("raw", ["proxy:3128", "ftp://proxy:21", "http://", "socks5://user:pw@:1080",
                                 "http://host:notaport", "file:///etc/passwd", "host"])
def test_proxy_rejects_garbage(raw):
    with pytest.raises(ValueError, match="PROXY"):
        Settings.from_env({"PROXY": raw})


@pytest.mark.parametrize("raw, expected", [("1", 1), ("4", 4), ("16", 16), (" 8 ", 8)])
def test_fragments_parsing(raw, expected):
    assert Settings.from_env({"FRAGMENTS": raw}).fragments == expected


@pytest.mark.parametrize("raw", ["0", "17", "-1", "x", "", "2.5"])
def test_fragments_rejects_out_of_range(raw):
    with pytest.raises(ValueError, match="FRAGMENTS"):
        Settings.from_env({"FRAGMENTS": raw})


# -- round 7 ---------------------------------------------------------------------------


def test_round7_defaults():
    s = Settings.from_env({})
    assert s.max_per_site == 1
    assert s.site_gap == 3.0
    assert s.keep_dir is None
    assert s.access_pin is None


@pytest.mark.parametrize("raw, expected", [("0", 0), ("1", 1), ("5", 5), (" 3 ", 3)])
def test_max_per_site_parsing(raw, expected):
    assert Settings.from_env({"MAX_PER_SITE": raw}).max_per_site == expected


@pytest.mark.parametrize("raw", ["-1", "x", "", "1.5"])
def test_max_per_site_rejects_garbage(raw):
    with pytest.raises(ValueError, match="MAX_PER_SITE"):
        Settings.from_env({"MAX_PER_SITE": raw})


@pytest.mark.parametrize("raw, expected", [("0", 0.0), ("3", 3.0), ("1.5", 1.5), (" 10 ", 10.0)])
def test_site_gap_parsing(raw, expected):
    assert Settings.from_env({"SITE_GAP": raw}).site_gap == expected


@pytest.mark.parametrize("raw", ["-1", "x", "", "nan", "inf"])
def test_site_gap_rejects_garbage(raw):
    with pytest.raises(ValueError, match="SITE_GAP"):
        Settings.from_env({"SITE_GAP": raw})


def test_keep_dir_empty_is_none_and_set_is_path():
    assert Settings.from_env({"KEEP_DIR": ""}).keep_dir is None
    assert Settings.from_env({"KEEP_DIR": "  "}).keep_dir is None
    assert Settings.from_env({"KEEP_DIR": "/keep"}).keep_dir == Path("/keep")


def test_ensure_dirs_creates_keep_dir_when_set(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path / "d"), "KEEP_DIR": str(tmp_path / "keep")})
    s.ensure_dirs()
    assert (tmp_path / "keep").is_dir()


def test_ensure_dirs_without_keep_dir(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    s.ensure_dirs()
    assert not (tmp_path / "keep").exists()


@pytest.mark.parametrize("raw, expected", [("", None), ("  ", None), ("1234", "1234"), ("abcd-EFGH_99", "abcd-EFGH_99"),
                                           ("x" * 64, "x" * 64)])
def test_access_pin_parsing(raw, expected):
    """Empty (or blank) means off."""
    assert Settings.from_env({"ACCESS_PIN": raw}).access_pin == expected


@pytest.mark.parametrize("raw", ["123", "x" * 65, "12 34", "1234\t", " 1234"])
def test_access_pin_rejects_bad_values(raw):
    with pytest.raises(ValueError, match="ACCESS_PIN") as info:
        Settings.from_env({"ACCESS_PIN": raw})
    assert raw not in str(info.value)   # never echo the (attempted) PIN


@pytest.mark.parametrize("keep", ["downloads", "downloads/kept", "tmp", "tmp/x", "./downloads/../tmp"])
def test_keep_dir_inside_downloads_or_tmp_is_rejected(tmp_path, keep):
    with pytest.raises(ValueError, match="KEEP_DIR must not be inside the downloads or tmp directory"):
        Settings.from_env({"DATA_DIR": str(tmp_path), "KEEP_DIR": str(tmp_path / keep)})


@pytest.mark.parametrize("keep", ["keep", "downloads-kept", "../elsewhere"])
def test_keep_dir_beside_downloads_is_accepted(tmp_path, keep):
    s = Settings.from_env({"DATA_DIR": str(tmp_path / "data"), "KEEP_DIR": str(tmp_path / "data" / keep)})
    assert s.keep_dir == tmp_path / "data" / keep


# --- round 8: HTTPS ---------------------------------------------------------------------------

def test_https_defaults_off():
    s = Settings.from_env({})
    assert s.https is False
    assert s.tls_hosts == []
    assert s.tls_dir == Path("/data/config/tls")


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
])
def test_https_parsing(raw, expected):
    assert Settings.from_env({"HTTPS": raw}).https is expected


@pytest.mark.parametrize("raw", ["2", "maybe", "y"])
def test_https_rejects_garbage(raw):
    with pytest.raises(ValueError, match="HTTPS"):
        Settings.from_env({"HTTPS": raw})


@pytest.mark.parametrize("raw, expected", [
    ("", []), (" , ,", []),
    ("192.168.1.10", ["192.168.1.10"]),
    (" 192.168.1.10 , NAS.local ,nas.local,", ["192.168.1.10", "nas.local"]),
    ("fd00::10,host-1.lan,192.168.1.10,fd00::10", ["fd00::10", "host-1.lan", "192.168.1.10"]),
    ("localhost", ["localhost"]),
])
def test_tls_hosts_parsing(raw, expected):
    assert Settings.from_env({"TLS_HOSTS": raw}).tls_hosts == expected


@pytest.mark.parametrize("bad", [
    "nas_box", "-nas", "nas-", "a..b", "nas.local.", "my nas", "ünïcode.local", "50%",
    "fe80::1%eth0", "http://nas", "nas:8080", "192.168.001.010", "1.2.3.4.5", "a" * 64 + ".lan", ("a" * 63 + ".") * 4 + "a",
])
def test_tls_hosts_rejects_bad_entries(bad):
    with pytest.raises(ValueError) as exc:
        Settings.from_env({"TLS_HOSTS": f"nas.local,{bad}"})
    entry = bad.strip().lower()
    assert str(exc.value) == f"TLS_HOSTS entry {entry!r} is not a hostname or IP address"


def test_tls_dir_follows_data_dir(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    assert s.tls_dir == tmp_path / "config" / "tls"


def test_ensure_dirs_does_not_create_tls_dir(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path), "HTTPS": "1"})
    s.ensure_dirs()
    assert not s.tls_dir.exists()


# -- round 9 ------------------------------------------------------------------

from datetime import time as _time

from app.window import Window


def test_round9_defaults():
    s = Settings.from_env({})
    assert s.sponsorblock is False
    assert s.prefer_h264 is False
    assert s.download_window is None
    assert s.webhook_url is None
    assert s.webhook_secret is None
    assert s.public_url is None
    assert s.subscription_cron == "0 */6 * * *"
    assert s.subscriptions_file == Path("/data/subscriptions.json")


@pytest.mark.parametrize("name, attr", [("SPONSORBLOCK", "sponsorblock"), ("PREFER_H264", "prefer_h264")])
def test_round9_bools(name, attr):
    assert getattr(Settings.from_env({name: "1"}), attr) is True
    assert getattr(Settings.from_env({name: "yes"}), attr) is True
    assert getattr(Settings.from_env({name: "0"}), attr) is False
    assert getattr(Settings.from_env({name: ""}), attr) is False
    with pytest.raises(ValueError, match=name):
        Settings.from_env({name: "maybe"})


def test_download_window_parsing():
    s = Settings.from_env({"DOWNLOAD_WINDOW": "22:00-06:00"})
    assert s.download_window == Window(_time(22), _time(6))
    assert Settings.from_env({"DOWNLOAD_WINDOW": "  "}).download_window is None


@pytest.mark.parametrize("bad", ["22:00", "05:00-05:00", "25:00-06:00", "night"])
def test_download_window_rejects_garbage(bad):
    with pytest.raises(ValueError, match="DOWNLOAD_WINDOW"):
        Settings.from_env({"DOWNLOAD_WINDOW": bad})


@pytest.mark.parametrize("raw", ["https://hooks.example.com/x?token=1", "http://192.168.1.5:8123/api/webhook/abc"])
def test_webhook_url_valid(raw):
    assert Settings.from_env({"WEBHOOK_URL": raw}).webhook_url == raw


def test_webhook_url_empty_and_stripped():
    assert Settings.from_env({"WEBHOOK_URL": ""}).webhook_url is None
    assert Settings.from_env({"WEBHOOK_URL": "   "}).webhook_url is None
    assert Settings.from_env({"WEBHOOK_URL": " https://h.example/x "}).webhook_url == "https://h.example/x"


@pytest.mark.parametrize("bad", ["ftp://h.example/x", "hooks.example.com/x", "https://", "https:///path",
                                 "http://h.example:notaport/", "javascript:alert(1)"])
def test_webhook_url_invalid(bad):
    with pytest.raises(ValueError, match="WEBHOOK_URL"):
        Settings.from_env({"WEBHOOK_URL": bad})


def test_webhook_secret_and_repr_never_show_it():
    s = Settings.from_env({"WEBHOOK_SECRET": "s3cr3t-value"})
    assert s.webhook_secret == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(s)
    assert "s3cr3t-value" not in str(s)
    assert "webhook_secret" not in repr(s)
    assert Settings.from_env({"WEBHOOK_SECRET": ""}).webhook_secret is None
    assert Settings.from_env({"WEBHOOK_SECRET": "   "}).webhook_secret is None
    assert Settings.from_env({"WEBHOOK_SECRET": " k3y\n"}).webhook_secret == "k3y"


def test_webhook_url_is_not_in_repr():
    s = Settings.from_env({"WEBHOOK_URL": "https://h.example/hook?token=tok3n"})
    assert s.webhook_url == "https://h.example/hook?token=tok3n"
    assert "tok3n" not in repr(s) and "tok3n" not in str(s)


@pytest.mark.parametrize("raw, expected", [
    ("https://192.168.1.10:8080", "https://192.168.1.10:8080"),
    ("https://192.168.1.10:8080/", "https://192.168.1.10:8080"),
    ("http://nas.local", "http://nas.local"),
    (" http://nas.local// ", "http://nas.local"),
    ("", None),
    ("  ", None),
])
def test_public_url(raw, expected):
    assert Settings.from_env({"PUBLIC_URL": raw}).public_url == expected


@pytest.mark.parametrize("bad", ["nas.local", "ftp://nas.local", "https://", "https://nas.local/app",
                                 "https://nas.local?x=1", "https://nas.local#f", "https://nas.local:99999",
                                 "https://user@nas.local", "https://user:pw@nas.local:8080",
                                 "https://:pw@nas.local"])
def test_public_url_invalid(bad):
    with pytest.raises(ValueError, match="PUBLIC_URL"):
        Settings.from_env({"PUBLIC_URL": bad})


def test_subscription_cron():
    assert Settings.from_env({"SUBSCRIPTION_CRON": " 15 * * * * "}).subscription_cron == "15 * * * *"
    assert Settings.from_env({"SUBSCRIPTION_CRON": ""}).subscription_cron == ""
    assert Settings.from_env({"SUBSCRIPTION_CRON": "  "}).subscription_cron == ""


@pytest.mark.parametrize("bad", ["every hour", "61 * * * *", "* * *"])
def test_subscription_cron_invalid(bad):
    with pytest.raises(ValueError, match="SUBSCRIPTION_CRON"):
        Settings.from_env({"SUBSCRIPTION_CRON": bad})


def test_subscriptions_file_follows_data_dir(tmp_path):
    assert Settings.from_env({"DATA_DIR": str(tmp_path)}).subscriptions_file == tmp_path / "subscriptions.json"


def test_compose_passes_round9_variables():
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    for line in ("SPONSORBLOCK: ${SPONSORBLOCK:-0}", "PREFER_H264: ${PREFER_H264:-0}",
                 "DOWNLOAD_WINDOW: ${DOWNLOAD_WINDOW:-}", "WEBHOOK_URL: ${WEBHOOK_URL:-}",
                 "WEBHOOK_SECRET: ${WEBHOOK_SECRET:-}", "PUBLIC_URL: ${PUBLIC_URL:-}",
                 "SUBSCRIPTION_CRON: ${SUBSCRIPTION_CRON:-0 */6 * * *}"):
        assert line in compose
