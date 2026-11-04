"""Real-browser tests: the app runs in-process on a free port, yt-dlp is faked, Chromium drives
the page. Everything skips cleanly when Playwright or Chromium is unavailable."""
import socket
import threading
import time
import urllib.request

import pytest

from app import probe as probe_mod
from app.main import create_app

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:  # pragma: no cover - exercised only on hosts without playwright
    sync_playwright = None
    PlaywrightError = Exception


SINGLE = {"title": "Me at the zoo", "heights": [2160, 1080, 720],
          "sizes": [{"height": 2160, "width": 3840}, {"height": 1080, "width": 1920}, {"height": 720, "width": 1280}],
          "subtitles": [{"lang": "en", "name": "English", "auto": False},
                        {"lang": "en-orig", "name": "English (Original)", "auto": True}],
          "playlist": None}
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
        if state["mode"] == "playlist":
            return {**SINGLE, "title": "Video 1", "playlist": {"title": "Trending", "count": 3, "total": 3}}
        return SINGLE

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
