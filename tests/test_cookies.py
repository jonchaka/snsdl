import os
import stat
from pathlib import Path

import pytest

from app import cookies
from app.cookies import CookieFormatError, CookieStore, parse_netscape

GOOD = ("# Netscape HTTP Cookie File\n"
        "# http://curl.haxx.se/rfc/cookie_spec.html\n"
        "\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tSECRET1\n"
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1893456000\tcsrftoken\tSECRET2\n"
        "www.facebook.com\tFALSE\t/\tTRUE\t0\tc_user\tSECRET3\n")


# -- parse_netscape -----------------------------------------------------------------

def test_parse_netscape_reads_cookies_without_keeping_values():
    got = parse_netscape(GOOD)
    assert [(c.domain, c.name, c.expires) for c in got] == [
        (".instagram.com", "sessionid", 1893456000),
        (".instagram.com", "csrftoken", 1893456000),       # HttpOnly prefix stripped
        ("www.facebook.com", "c_user", 0),                   # session cookie
    ]
    assert not any("SECRET" in repr(c) for c in got)


@pytest.mark.parametrize("text, reason", [
    ("", "no cookies"),
    ("# only comments\n\n", "no cookies"),
    ("instagram.com TRUE / TRUE 0 a b\n", "7 tab-separated"),               # spaces, not tabs
    (".x.com\tTRUE\t/\tTRUE\tsoon\tname\tv\n", "not a positive number"),
    ("x.com\tTRUE\t/\tTRUE\t0\tname\tv\n", "domain flag"),
    (".x.com\tFALSE\t/\tTRUE\t0\tname\tv\n", "domain flag"),
    ("\tFALSE\t/\tTRUE\t0\tname\tv\n", "empty domain"),
    ("<html>login</html>\n", "7 tab-separated"),
    ("x" * (1024 * 1024 + 1), "1 MiB"),
])
def test_parse_netscape_rejects_bad_input_with_a_reason(text, reason):
    with pytest.raises(CookieFormatError, match=reason):
        parse_netscape(text)


def test_parse_netscape_accepts_crlf_and_float_expiry_and_empty_value():
    got = parse_netscape(".a.com\tTRUE\t/\tFALSE\t1700000000.5\tk\t\r\n")
    assert got[0].expires == 1700000000 and got[0].name == "k"


def test_site_of_strips_dot_and_www():
    assert cookies.site_of(".instagram.com") == "instagram.com"
    assert cookies.site_of("www.Facebook.com") == "facebook.com"
    assert cookies.site_of("m.youtube.com") == "m.youtube.com"


def test_entry_metadata():
    cs = parse_netscape(GOOD)
    e = cookies.entry_from_cookies("abc", "kept", cs, added_at=1_700_000_000, now=1_800_000_000)
    assert e.sites == ["facebook.com", "instagram.com"] and e.count == 3
    assert e.added_at == "2023-11-14T22:13:20+00:00"
    assert e.expires_at == "2030-01-01T00:00:00+00:00" and e.expired is False
    stale = cookies.entry_from_cookies("abc", "kept", cs, added_at=0, now=1_900_000_000)
    assert stale.expired is True
    only_session = cookies.entry_from_cookies("s", "kept", [cookies.Cookie("a", "b", 0)], added_at=0)
    assert only_session.expires_at is None and only_session.expired is False


# -- CookieStore ----------------------------------------------------------------------

def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_add_kept_writes_private_file_and_lists_it(settings):
    store = CookieStore(settings)
    e = store.add(GOOD, keep=True)
    path = store.dir / f"{e.id}.txt"
    assert path.is_file() and _mode(path) == 0o600 and e.source == "kept"
    assert path.read_text() == GOOD                      # GOOD already carries the header line
    [listed] = store.entries()
    assert listed.id == e.id and listed.sites == ["facebook.com", "instagram.com"] and listed.count == 3


def test_add_once_writes_tmp_file_for_that_job_and_is_not_listed(settings):
    store = CookieStore(settings)
    e = store.add(GOOD, keep=False, job_id="job1")
    assert e.source == "once" and e.id == "job1"
    assert store.once_path("job1").is_file() and _mode(store.once_path("job1")) == 0o600
    assert store.entries() == []
    store.discard_once("job1")
    assert not store.once_path("job1").exists()
    store.discard_once("job1")   # idempotent


def test_add_once_without_job_id_is_an_error(settings):
    with pytest.raises(ValueError):
        CookieStore(settings).add(GOOD, keep=False)


def test_add_rejects_bad_text_without_writing(settings):
    store = CookieStore(settings)
    with pytest.raises(CookieFormatError):
        store.add("garbage", keep=True)
    assert store.entries() == [] and not store.dir.exists()


def test_entries_include_legacy_file_read_only(settings):
    settings.cookies_file.write_text(GOOD)
    store = CookieStore(settings)
    [legacy] = store.entries()
    assert legacy.id == "legacy" and legacy.source == "legacy" and legacy.count == 3
    assert store.delete("legacy") is False and settings.cookies_file.exists()


def test_delete_removes_only_known_kept_ids(settings):
    store = CookieStore(settings)
    e = store.add(GOOD, keep=True)
    assert store.delete("../../etc/passwd") is False
    assert store.delete("nope") is False
    assert store.delete(e.id) is True and store.entries() == []
    assert store.delete(e.id) is False


def test_entries_tolerate_a_corrupt_kept_file(settings):
    store = CookieStore(settings)
    store.dir.mkdir(parents=True)
    (store.dir / "bad1.txt").write_text("not cookies")
    [bad] = store.entries()
    assert bad.count == 0 and bad.sites == []


def test_merged_for_single_source_is_a_private_copy_removed_afterwards(settings):
    settings.cookies_file.write_text(GOOD)
    store = CookieStore(settings)
    with store.merged_for("probe-x") as path:
        assert path == settings.tmp_dir / "cookies-probe-x.txt"
        assert path.read_text() == GOOD and _mode(path) == 0o600           # one header, not two
    assert not path.exists()


def _load_with_yt_dlp(path):
    from yt_dlp.cookies import YoutubeDLCookieJar
    jar = YoutubeDLCookieJar(str(path))
    jar.load()
    return {(c.domain, c.name): c.value for c in jar}


def test_header_less_export_is_accepted_by_yt_dlp_cookie_loader(settings):
    """Exporters sometimes omit the magic first line; without it yt-dlp refuses the whole jar."""
    store = CookieStore(settings)
    store.add(".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tS1\n", keep=True)   # no header
    with store.merged_for("j", job_id=None) as path:
        assert _load_with_yt_dlp(path) == {(".instagram.com", "sessionid"): "S1"}
    store.add("o.com\tFALSE\t/\tTRUE\t0\tc\t3\n", keep=False, job_id="j1")
    with store.merged_for("j1", job_id="j1") as path:
        assert set(_load_with_yt_dlp(path)) == {(".instagram.com", "sessionid"), ("o.com", "c")}


def test_with_header_keeps_an_existing_magic_line():
    assert cookies.with_header("# HTTP Cookie File\nx\ty\n") == "# HTTP Cookie File\nx\ty\n"
    assert cookies.with_header("\n# Netscape HTTP Cookie File\nx\n") == "\n# Netscape HTTP Cookie File\nx\n"
    assert cookies.with_header("# cookies\nx").startswith(cookies.HEADER + "# cookies\n")


def test_newest_kept_export_wins_on_duplicate_cookies(settings, monkeypatch):
    import os, time
    store = CookieStore(settings)
    old = store.add(".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tOLD\n", keep=True)
    new = store.add(".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tNEW\n", keep=True)
    os.utime(store.dir / f"{old.id}.txt", (1_700_000_000, 1_700_000_000))
    os.utime(store.dir / f"{new.id}.txt", (1_800_000_000, 1_800_000_000))
    assert [e.id for e in store.entries()] == [new.id, old.id]                 # listing: newest first
    with store.merged_for("x") as path:
        assert _load_with_yt_dlp(path) == {(".instagram.com", "sessionid"): "NEW"}   # loader: last wins


@pytest.mark.parametrize("expires", ["inf", "1e400", "-5", "1e5", "nan", "0x10", "1.2.3"])
def test_parse_netscape_rejects_expiries_yt_dlp_would_skip_or_that_overflow(expires):
    with pytest.raises(CookieFormatError, match="expiry"):
        parse_netscape(f".a.com\tTRUE\t/\tTRUE\t{expires}\tk\tv\n")


def test_parse_netscape_clamps_huge_expiry_and_truncates_error_echo():
    got = parse_netscape(".a.com\tTRUE\t/\tTRUE\t99999999999999999999\tk\tv\n")
    assert got[0].expires == cookies.MAX_EXPIRY
    e = cookies.entry_from_cookies("x", "kept", got, added_at=0)
    assert e.expires_at.startswith("2242")
    with pytest.raises(CookieFormatError) as err:
        parse_netscape(".a.com\tTRUE\t/\tTRUE\t" + "9x" * 500 + "\tk\tv\n")
    assert len(str(err.value)) < 80


def test_entries_and_merge_survive_files_vanishing_and_odd_content(settings, monkeypatch):
    store = CookieStore(settings)
    e = store.add(GOOD, keep=True)
    store.dir.joinpath("odd1.txt").write_bytes(b"\xff\xfe\x00garbage")
    assert {x.id for x in store.entries()} == {e.id, "odd1"}
    real_read = Path.read_text

    def vanish(self, *a, **kw):
        if self.name == f"{e.id}.txt":
            raise FileNotFoundError(self)
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", vanish)
    with store.merged_for("x") as path:
        assert path.is_file()                 # the vanished file is skipped, the run still gets a jar
    assert store.delete(e.id) is True and store.delete(e.id) is False


def test_write_private_fixes_mode_of_a_pre_existing_world_readable_file(settings):
    store = CookieStore(settings)
    store.dir.mkdir(parents=True)
    target = store.once_path("j9")
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    target.write_text("old"); os.chmod(target, 0o644)
    store.add(GOOD, keep=False, job_id="j9")
    assert _mode(target) == 0o600 and target.read_text() == GOOD          # GOOD already has the header


def test_merged_for_concatenates_legacy_kept_and_the_job_once_file(settings):
    settings.cookies_file.write_text("# legacy\nl.com\tFALSE\t/\tTRUE\t0\ta\t1")   # no trailing newline
    store = CookieStore(settings)
    store.add("k.com\tFALSE\t/\tTRUE\t0\tb\t2\n", keep=True)
    store.add("o.com\tFALSE\t/\tTRUE\t0\tc\t3\n", keep=False, job_id="j1")
    with store.merged_for("j1", job_id="j1") as path:
        text = path.read_text()
        assert text.startswith("# Netscape HTTP Cookie File\n")
        assert [c.domain for c in parse_netscape(text)] == ["l.com", "k.com", "o.com"]
        assert set(_load_with_yt_dlp(path)) == {("l.com", "a"), ("k.com", "b"), ("o.com", "c")}
        assert _mode(path) == 0o600
    with store.merged_for("j2", job_id="j2") as path:      # another job never sees j1's once-file
        assert [c.domain for c in parse_netscape(path.read_text())] == ["l.com", "k.com"]
    assert not path.exists()


def test_merged_for_yields_none_without_any_source(settings):
    with CookieStore(settings).merged_for("x") as path:
        assert path is None
    assert list(settings.tmp_dir.glob("cookies-*")) == []


def test_merged_output_has_exactly_one_magic_line(settings):
    settings.cookies_file.write_text(GOOD)                          # has a header
    store = CookieStore(settings)
    store.add("# Netscape HTTP Cookie File\n.k.com\tTRUE\t/\tTRUE\t0\tb\t2\n", keep=True)
    store.add(".o.com\tTRUE\t/\tTRUE\t0\tc\t3\n", keep=False, job_id="j1")   # none
    with store.merged_for("j1", job_id="j1") as path:
        lines = path.read_text().splitlines()
        assert lines[0] == "# Netscape HTTP Cookie File"
        assert sum(1 for l in lines if "HTTP Cookie File" in l) == 1
        assert len(_load_with_yt_dlp(path)) == 5
