"""Real-browser tests: the app runs in-process on a free port, yt-dlp is faked, Chromium drives
the page. Everything skips cleanly when Playwright or Chromium is unavailable."""
import socket
import struct
import threading
import time
import urllib.request
import zlib

import pytest

from app import probe as probe_mod
from app import subscriptions as subs_mod
from app import thumbs as thumbs_mod
from app.main import create_app

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:  # pragma: no cover - exercised only on hosts without playwright
    sync_playwright = None
    PlaywrightError = Exception


SINGLE = {"kind": "video", "title": "Me at the zoo", "duration": 754, "heights": [2160, 1080, 720],
          "sizes": [{"height": 2160, "width": 3840, "bytes": None},
                    {"height": 1080, "width": 1920, "bytes": 180 * 1024 * 1024},
                    {"height": 720, "width": 1280, "bytes": None}],
          "audio_bytes": 12 * 1024 * 1024,
          "subtitles": [{"lang": "en", "name": "English", "auto": False},
                        {"lang": "de", "name": "German", "auto": False},
                        {"lang": "en-orig", "name": "English (Original)", "auto": True},
                        {"lang": "ja", "name": "Japanese", "auto": True}],
          "chapters": [{"title": "Intro", "start": 0.0, "end": 65.5},
                       {"title": "Main part", "start": 65.5, "end": 400.0}],
          "thumb": None, "playlist": None, "live": False}


def _png_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


PNG_1X1 = _png_1x1()
# A photo post: three photos and a clip, plus the video-probe keys the page may read, empty.
IMAGES = {"kind": "images", "title": "Four photos of a cat", "count": 4, "video_count": 1,
          "files": [{"ext": "jpg", "width": 1000, "height": 800}] * 3 + [{"ext": "mp4", "width": 720, "height": 1280}],
          "thumb_url": None, "thumb": None, "previous": None,
          "heights": [], "sizes": [], "subtitles": [], "chapters": [], "duration": None,
          "playlist": None, "audio_bytes": None}
PLAYLIST = {"title": "Trending", "total": 3, "entries": [
    {"url": f"https://www.youtube.com/watch?v=v{i}", "title": f"Video {i}"} for i in (1, 2, 3)]}
SEARCH = [{"url": "https://www.youtube.com/watch?v=lofi1", "title": "Lofi beats to study to", "duration": 3600,
           "uploader": "Lofi Girl", "thumb": None, "live": False},
          {"url": "https://www.youtube.com/watch?v=lofi2", "title": "Chill lofi mix", "duration": 754,
           "uploader": "Chillhop", "thumb": None, "live": False}]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fakes(monkeypatch):
    """Switchable fake probe: tests set fakes['mode'] to 'single', 'playlist', 'login', 'nochapters',
    'live' (a stream on air: no duration, no chapters) or 'images' (a photo post, no thumbnail). Also fakes
    probe.search (records queries in fakes['searches']) and the subscription checker's expander
    (3 entries per call; bump fakes['sub_round'] to make them new)."""
    state = {"mode": "single", "searches": [], "sub_round": 1}

    def fake_probe(url, settings):
        if state["mode"] == "login":
            raise probe_mod.ProbeError("[instagram] abc: You need to log in to access this content", needs_login=True)
        if state["mode"] == "images":
            return {**IMAGES, "files": [dict(f) for f in IMAGES["files"]]}
        # Like the real probe: a fresh server-chosen key whose file exists in thumbs_dir.
        key = thumbs_mod.new_key()
        (settings.thumbs_dir / f"{key}.png").write_bytes(PNG_1X1)
        if state["mode"] == "live":
            return {**SINGLE, "thumb": key, "duration": None, "live": True, "chapters": []}
        if state["mode"] == "nochapters":
            return {**SINGLE, "thumb": key, "chapters": []}
        if state["mode"] == "playlist":
            return {**SINGLE, "title": "Video 1", "thumb": key,
                    "playlist": {"title": "Trending", "count": 3, "total": 3}}
        return {**SINGLE, "thumb": key}

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    monkeypatch.setattr(probe_mod, "expand_playlist", lambda url, settings: PLAYLIST)

    def fake_search(q, settings):
        state["searches"].append(q)
        return [dict(r) for r in SEARCH]

    def fake_expand(url, settings):
        n = state["sub_round"]
        return {"title": "Lofi channel", "total": 3, "entries": [
            {"url": f"https://www.youtube.com/watch?v=s{n}x{i}", "title": f"Upload {n}.{i}"} for i in (1, 2, 3)]}

    monkeypatch.setattr(probe_mod, "search", fake_search)
    monkeypatch.setattr(subs_mod, "expander", fake_expand)
    return state


def _serve(settings, tls_files=None):
    """tls_files: a tls.TlsFiles to serve HTTPS with that certificate (readiness is probed
    without verifying it)."""
    import ssl
    import uvicorn
    app = create_app(settings, run_background=False)
    port = _free_port()
    extra = {"ssl_certfile": str(tls_files.cert), "ssl_keyfile": str(tls_files.key)} if tls_files else {}
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio", **extra)
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"{'https' if tls_files else 'http'}://127.0.0.1:{port}"
    unverified = ssl._create_unverified_context() if tls_files else None
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/auth", timeout=1, context=unverified)
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("test server did not start")
    yield type("Server", (), {"base": base, "app": app, "store": app.state.store})()
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def server(settings, fakes):
    yield from _serve(settings)


@pytest.fixture
def window_server(settings, fakes, monkeypatch):
    """The app with DOWNLOAD_WINDOW=01:00-02:00 and the worker clock fixed at 12:00 UTC today
    (window closed; both set before create_app)."""
    from datetime import datetime, timezone
    from app import window as window_mod
    from app import worker as worker_mod
    settings.download_window = window_mod.parse_window("01:00-02:00")
    fixed = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(worker_mod, "now_fn", lambda s: fixed)
    yield from _serve(settings)


@pytest.fixture
def keep_server(settings, fakes, tmp_path):
    """The app with KEEP_DIR set (a temporary directory; set before create_app)."""
    keep = tmp_path / "keep"
    keep.mkdir()
    settings.keep_dir = keep
    yield from _serve(settings)


@pytest.fixture
def pin_server(settings, fakes):
    """The app with ACCESS_PIN=1234 (set before create_app: the gate is installed at creation)."""
    settings.access_pin = "1234"
    yield from _serve(settings)


@pytest.fixture
def telegram_server(settings, fakes):
    """The app with Telegram configured (a well-formed fake token and chat id, set before
    create_app); tests route /api/telegram/test so nothing reaches api.telegram.org."""
    settings.telegram_bot_token = "123456:" + "a" * 25
    settings.telegram_chat_id = "42"
    yield from _serve(settings)


@pytest.fixture
def https_server(settings, fakes):
    """The app over HTTPS with a freshly generated certificate (HTTPS set before create_app:
    cert_hosts is read at creation). Pages need browser.new_context(ignore_https_errors=True)."""
    from app import tls
    files = tls.ensure_certs(settings.tls_dir, tls.default_hosts())
    settings.https = True
    yield from _serve(settings, files)


@pytest.fixture(scope="module")
def browser():
    # Module scope, not session: the sync Playwright context keeps an asyncio loop running in
    # the main thread, which would break asyncio.run() in the ordinary tests that follow.
    if sync_playwright is None:
        pytest.skip("playwright is not installed (make browser-test)")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available: {str(exc).splitlines()[0]} (make browser-test)")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    pg = browser.new_page()
    pg.on("dialog", lambda d: d.accept())
    pg.goto(server.base + "/")
    yield pg
    pg.close()
