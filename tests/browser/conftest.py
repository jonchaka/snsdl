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
from app import thumbs as thumbs_mod
from app.main import create_app

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:  # pragma: no cover - exercised only on hosts without playwright
    sync_playwright = None
    PlaywrightError = Exception


SINGLE = {"title": "Me at the zoo", "duration": 754, "heights": [2160, 1080, 720],
          "sizes": [{"height": 2160, "width": 3840, "bytes": None},
                    {"height": 1080, "width": 1920, "bytes": 180 * 1024 * 1024},
                    {"height": 720, "width": 1280, "bytes": None}],
          "audio_bytes": 12 * 1024 * 1024,
          "subtitles": [{"lang": "en", "name": "English", "auto": False},
                        {"lang": "en-orig", "name": "English (Original)", "auto": True}],
          "thumb": None, "playlist": None}


def _png_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


PNG_1X1 = _png_1x1()
PLAYLIST = {"title": "Trending", "total": 3, "entries": [
    {"url": f"https://www.youtube.com/watch?v=v{i}", "title": f"Video {i}"} for i in (1, 2, 3)]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fakes(monkeypatch):
    """Switchable fake probe: tests set fakes['mode'] to 'single' or 'playlist'."""
    state = {"mode": "single"}

    def fake_probe(url, settings):
        if state["mode"] == "login":
            raise probe_mod.ProbeError("[instagram] abc: You need to log in to access this content", needs_login=True)
        # Like the real probe: a fresh server-chosen key whose file exists in thumbs_dir.
        key = thumbs_mod.new_key()
        (settings.thumbs_dir / f"{key}.png").write_bytes(PNG_1X1)
        if state["mode"] == "playlist":
            return {**SINGLE, "title": "Video 1", "thumb": key, "playlist": {"title": "Trending", "count": 3, "total": 3}}
        return {**SINGLE, "thumb": key}

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    monkeypatch.setattr(probe_mod, "expand_playlist", lambda url, settings: PLAYLIST)
    return state


@pytest.fixture
def server(settings, fakes):
    import uvicorn
    app = create_app(settings, run_background=False)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/jobs", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("test server did not start")
    yield type("Server", (), {"base": base, "app": app, "store": app.state.store})()
    srv.should_exit = True
    thread.join(timeout=5)


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
