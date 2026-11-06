import http.server
import os
import stat
import threading
import time

import pytest

from app import thumbs

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
GIF = b"GIF89a" + b"\x00" * 60
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 60
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 60
AVIS = b"\x00\x00\x00\x20ftypavis\x00\x00\x00\x00avisavifmsf1" + b"\x00" * 60
KEY = "abcDEF12_-x"


class _Resp:
    """File-like urllib response stand-in: records how many bytes were asked for."""

    def __init__(self, data: bytes, headers=None):
        self._data, self._pos = data, 0
        self.headers = headers or {}
        self.requested = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        self.requested += n
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, data=b"", exc=None, headers=None):
        self.resp = _Resp(data, headers)
        self.exc = exc
        self.calls = []

    def open(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.exc is not None:
            raise self.exc
        return self.resp


# -- sniff -----------------------------------------------------------------------

@pytest.mark.parametrize("data, ext", [(JPEG, "jpg"), (PNG, "png"), (GIF, "gif"), (b"GIF87a" + b"\0" * 10, "gif"),
                                       (WEBP, "webp"), (AVIF, "avif"), (AVIS, "avif")])
def test_sniff_recognises_image_magic(data, ext):
    assert thumbs.sniff(data) == ext


@pytest.mark.parametrize("data", [b"", b"<html>", b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\xff\xd8", b"GIF88a",
                                  b"\x00\x00\x00\x18ftypmp42", b"\x00\x00\x00\x18ftypheic", b"ftypavif"])
def test_sniff_rejects_other_bytes(data):
    assert thumbs.sniff(data) is None


def test_fetch_stores_an_avif_thumbnail(settings):
    name = thumbs.fetch_thumbnail("https://i.ytimg.com/vi/x/hq.avif", settings.thumbs_dir, KEY, settings,
                                  opener=_Opener(AVIF))
    assert name == f"{KEY}.avif"
    assert thumbs.find(settings.thumbs_dir, KEY) == settings.thumbs_dir / name
    assert thumbs.media_type(settings.thumbs_dir / name) == "image/avif"


# -- keys ------------------------------------------------------------------------

def test_new_key_matches_the_route_pattern():
    keys = {thumbs.new_key() for _ in range(50)}
    assert len(keys) == 50 and all(thumbs.KEY_PATTERN.fullmatch(k) for k in keys)


# -- fetch -----------------------------------------------------------------------

def test_fetch_writes_file_with_sniffed_extension_and_mode_0644(settings):
    opener = _Opener(PNG, headers={"Content-Type": "image/jpeg"})   # header lies; bytes win
    name = thumbs.fetch_thumbnail("https://i.example/t.jpg", settings.thumbs_dir, KEY, settings, opener=opener)
    assert name == f"{KEY}.png"
    path = settings.thumbs_dir / name
    assert path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert opener.calls == [("https://i.example/t.jpg", thumbs.TIMEOUT)]
    assert [p.name for p in settings.thumbs_dir.iterdir()] == [name]   # no temp file left


def test_fetch_creates_the_destination_directory(settings, tmp_path):
    dest = tmp_path / "new" / "thumbs"
    assert thumbs.fetch_thumbnail("http://i.example/t", dest, KEY, settings, opener=_Opener(JPEG)) == f"{KEY}.jpg"
    assert (dest / f"{KEY}.jpg").is_file()


def test_fetch_rejects_a_body_over_the_cap_reading_at_most_cap_plus_one(settings):
    opener = _Opener(JPEG + b"\x00" * thumbs.MAX_BYTES, headers={"Content-Length": "10"})   # length lies
    assert thumbs.fetch_thumbnail("https://i.example/t", settings.thumbs_dir, KEY, settings, opener=opener) is None
    assert opener.resp.requested <= thumbs.MAX_BYTES + 1
    assert list(settings.thumbs_dir.iterdir()) == []


def test_fetch_accepts_exactly_the_cap(settings):
    opener = _Opener(JPEG + b"\x00" * (thumbs.MAX_BYTES - len(JPEG)))
    assert thumbs.fetch_thumbnail("https://i.example/t", settings.thumbs_dir, KEY, settings,
                                  opener=opener) == f"{KEY}.jpg"


def test_fetch_rejects_unknown_content(settings):
    opener = _Opener(b"<html>not an image</html>", headers={"Content-Type": "image/png"})
    assert thumbs.fetch_thumbnail("https://i.example/t", settings.thumbs_dir, KEY, settings, opener=opener) is None
    assert list(settings.thumbs_dir.iterdir()) == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/t.jpg", "data:image/png;base64,AAAA", "", None,
                                 "https://", "javascript:alert(1)", 42])
def test_fetch_only_accepts_http_urls(settings, url):
    opener = _Opener(JPEG)
    assert thumbs.fetch_thumbnail(url, settings.thumbs_dir, KEY, settings, opener=opener) is None
    assert opener.calls == []


@pytest.mark.parametrize("key", ["short", "x" * 17, "../../etc", "a b c d e f", "", None])
def test_fetch_refuses_invalid_keys(settings, key):
    opener = _Opener(JPEG)
    assert thumbs.fetch_thumbnail("https://i.example/t", settings.thumbs_dir, key, settings, opener=opener) is None
    assert opener.calls == []


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), OSError("refused"), ValueError("bad"), RuntimeError("x")])
def test_fetch_never_raises(settings, exc):
    opener = _Opener(exc=exc)
    assert thumbs.fetch_thumbnail("https://i.example/t", settings.thumbs_dir, KEY, settings, opener=opener) is None


def test_fetch_never_raises_when_the_directory_cannot_be_written(settings, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert thumbs.fetch_thumbnail("https://i.example/t", blocker / "thumbs", KEY, settings,
                                  opener=_Opener(JPEG)) is None


def test_build_opener_honours_an_http_proxy(settings):
    settings.proxy = "http://user:pw@proxy.local:3128"
    opener = thumbs.build_opener(settings)
    handler = next(h for h in opener.handlers if h.__class__.__name__ == "ProxyHandler")
    assert handler.proxies == {"http": settings.proxy, "https": settings.proxy}


def test_socks_proxy_skips_the_fetch_instead_of_going_direct(settings):
    settings.proxy = "socks5://proxy.local:1080"
    assert thumbs.build_opener(settings) is None
    assert thumbs.fetch_thumbnail("https://i.example/t", settings.thumbs_dir, KEY, settings) is None
    assert list(settings.thumbs_dir.iterdir()) == []


# -- real urllib against a local server --------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        status, body, delay, location = self.routes.get(self.path, (404, b"", 0, None))
        time.sleep(delay)
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_fetch_over_real_http(settings, server, monkeypatch):
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    _Handler.routes = {"/t.gif": (200, GIF, 0, None), "/missing": (404, b"", 0, None),
                       "/big": (200, JPEG + b"\0" * thumbs.MAX_BYTES, 0, None),
                       "/to-file": (302, b"", 0, "file:///etc/passwd")}
    assert thumbs.fetch_thumbnail(f"{server}/t.gif", settings.thumbs_dir, KEY, settings) == f"{KEY}.gif"
    assert thumbs.fetch_thumbnail(f"{server}/missing", settings.thumbs_dir, "otherkey1", settings) is None
    assert thumbs.fetch_thumbnail(f"{server}/big", settings.thumbs_dir, "otherkey2", settings) is None
    assert thumbs.fetch_thumbnail(f"{server}/to-file", settings.thumbs_dir, "otherkey3", settings) is None
    assert [p.name for p in settings.thumbs_dir.iterdir()] == [f"{KEY}.gif"]


def test_fetch_times_out(settings, server, monkeypatch):
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setattr(thumbs, "TIMEOUT", 0.2)
    _Handler.routes = {"/slow": (200, JPEG, 1.0, None)}
    started = time.monotonic()
    assert thumbs.fetch_thumbnail(f"{server}/slow", settings.thumbs_dir, KEY, settings) is None
    assert time.monotonic() - started < 0.9


# -- find / delete / media type ------------------------------------------------------

def test_find_and_delete(settings):
    d = settings.thumbs_dir
    (d / f"{KEY}.webp").write_bytes(WEBP)
    assert thumbs.find(d, KEY) == d / f"{KEY}.webp"
    assert thumbs.find(d, "missing12") is None
    thumbs.delete(d, KEY)
    assert thumbs.find(d, KEY) is None
    thumbs.delete(d, KEY)   # already gone: no error


@pytest.mark.parametrize("key", ["*", "../x", "a" * 7, "a" * 17, "", None, "abc.def12"])
def test_find_and_delete_validate_the_key(settings, key):
    (settings.thumbs_dir / "abcdefgh.jpg").write_bytes(JPEG)
    assert thumbs.find(settings.thumbs_dir, key) is None
    thumbs.delete(settings.thumbs_dir, key)
    assert (settings.thumbs_dir / "abcdefgh.jpg").exists()


def test_find_tolerates_a_missing_directory(tmp_path):
    assert thumbs.find(tmp_path / "nope", KEY) is None
    thumbs.delete(tmp_path / "nope", KEY)


@pytest.mark.parametrize("name, mime", [("a.jpg", "image/jpeg"), ("a.png", "image/png"), ("a.gif", "image/gif"),
                                        ("a.webp", "image/webp"), ("a.avif", "image/avif"),
                                        ("a.bin", "application/octet-stream")])
def test_media_type(tmp_path, name, mime):
    assert thumbs.media_type(tmp_path / name) == mime


# -- sweep ---------------------------------------------------------------------------

def test_sweep_removes_old_unreferenced_files_only(settings):
    d = settings.thumbs_dir
    now = 1_000_000.0
    files = {"keepref01.jpg": now - 7200, "oldorphan.png": now - 7200, "neworphan.gif": now - 60,
             ".tmpleftover-x.tmp": now - 7200}
    for name, mtime in files.items():
        (d / name).write_bytes(b"x")
        os.utime(d / name, (mtime, mtime))
    removed = thumbs.sweep_orphans(d, {"keepref01"}, older_than=3600, now=now)
    assert removed == 2
    assert sorted(p.name for p in d.iterdir()) == ["keepref01.jpg", "neworphan.gif"]


def test_sweep_tolerates_a_missing_directory(tmp_path):
    assert thumbs.sweep_orphans(tmp_path / "nope", set()) == 0
