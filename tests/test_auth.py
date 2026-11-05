import asyncio
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app import api as api_mod
from app import auth
from app import worker as worker_mod
from app.config import Settings
from app.main import create_app


CSRF = {"X-Requested-With": "sns-downloader"}
PIN = "4321"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: False)
    monkeypatch.setattr(api_mod, "AUTH_FAIL_DELAY", 0)


@pytest.fixture
def pin_settings(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path), "ACCESS_PIN": PIN})
    s.ensure_dirs()
    return s


@pytest.fixture
def pclient(pin_settings):
    with TestClient(create_app(pin_settings, run_background=False)) as c:
        yield c


def unlock(client, pin=PIN):
    return client.post("/api/auth", json={"pin": pin}, headers=CSRF)


# --- secret / token ---------------------------------------------------------------------------

def test_secret_is_32_bytes_0600_and_stable(settings):
    first = auth.secret_for(settings)
    path = settings.data_dir / "config" / "secret"
    assert len(first) == 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert auth.secret_for(settings) == first
    assert path.read_bytes() == first


def test_secret_creates_config_dir(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path / "fresh"), "COOKIES_FILE": str(tmp_path / "c.txt")})
    assert len(auth.secret_for(s)) == 32
    assert (tmp_path / "fresh" / "config" / "secret").exists()


@pytest.mark.parametrize("content", [b"", b"short"])
def test_secret_empty_or_short_file_is_regenerated(settings, content):
    path = settings.data_dir / "config" / "secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    secret = auth.secret_for(settings)
    assert len(secret) == 32 and path.read_bytes() == secret
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert auth.secret_for(settings) == secret


def test_secret_crash_before_rename_leaves_no_partial_file(settings, monkeypatch):
    path = settings.data_dir / "config" / "secret"

    def crash(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr(auth.os, "replace", crash)
    with pytest.raises(OSError):
        auth.secret_for(settings)
    assert not path.exists()
    assert list(path.parent.iterdir()) == []   # the temp file is cleaned up too
    monkeypatch.undo()
    assert len(auth.secret_for(settings)) == 32


def test_secret_wrong_length_long_file_still_refused(settings):
    path = settings.data_dir / "config" / "secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 40)
    with pytest.raises(RuntimeError, match="damaged"):
        auth.secret_for(settings)


def test_token_is_hmac_hex_and_depends_on_both():
    t = auth.token(b"k" * 32, "1234")
    assert len(t) == 64 and int(t, 16) >= 0
    assert t == auth.token(b"k" * 32, "1234")
    assert t != auth.token(b"k" * 32, "1235")
    assert t != auth.token(b"j" * 32, "1234")


# --- exempt rule ------------------------------------------------------------------------------

@pytest.mark.parametrize("path,exempt", [
    ("/", True), ("/index.html", True), ("/favicon.ico", True), ("/app.css", True),
    ("/api/auth", True),
    ("/api/jobs", False), ("/api/status", False), ("/api/", False), ("/files/x.mp4", False),
    ("/files/", False),
    ("/cert.pem", True), ("/cert.crt", True), ("/manifest.webmanifest", True), ("/sw.js", True),
    ("/icons/icon-192.png", True),
])
def test_exempt_rule(path, exempt):
    assert auth.is_exempt(path) is exempt


# --- limiter ----------------------------------------------------------------------------------

def test_failure_limiter_window_and_clear():
    now = [0.0]
    lim = auth.FailureLimiter(limit=5, window=60.0, clock=lambda: now[0])
    assert [lim.hit("a") for _ in range(5)] == [False] * 5
    assert lim.hit("a") is True
    assert lim.hit("b") is False                 # per address
    now[0] = 61.0
    assert lim.hit("a") is False                 # old failures expired
    lim.clear("a")
    assert all(lim.hit("a") is False for _ in range(5))


def test_failure_limiter_blocked_check_does_not_record():
    lim = auth.FailureLimiter(limit=2, window=60.0, clock=lambda: 0.0)
    assert lim.blocked("a") is False
    lim.hit("a")
    assert lim.blocked("a") is False
    lim.hit("a")
    assert lim.blocked("a") is True
    assert lim.blocked("b") is False


def test_failure_limiter_forgets_addresses_whose_failures_expired():
    now = [0.0]
    lim = auth.FailureLimiter(limit=5, window=60.0, clock=lambda: now[0])
    for i in range(100):
        lim.hit(f"10.0.0.{i}")
    assert len(lim._fails) == 100
    now[0] = 61.0
    lim.hit("10.0.1.1")
    assert set(lim._fails) == {"10.0.1.1"}


# --- middleware -------------------------------------------------------------------------------

def test_api_and_files_need_cookie(pclient):
    for path in ("/api/jobs", "/api/status", "/files/x.mp4"):
        r = pclient.get(path)
        assert r.status_code == 401, path
        assert r.headers["content-type"] == "application/json"
        assert r.json() == {"detail": "PIN required"}
    r = pclient.post("/api/jobs", json={"url": "https://youtu.be/a"}, headers=CSRF)
    assert r.status_code == 401


def test_cert_download_is_open_with_a_pin(tmp_path):
    from app import tls
    s = Settings.from_env({"DATA_DIR": str(tmp_path), "ACCESS_PIN": PIN, "HTTPS": "1"})
    s.ensure_dirs()
    tls.ensure_certs(s.tls_dir, ["localhost", "127.0.0.1"])
    with TestClient(create_app(s, run_background=False)) as c:
        for path in ("/cert.pem", "/cert.crt"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.content == (s.tls_dir / tls.CA_CERT).read_bytes()
        assert c.get("/api/status").status_code == 401


def test_index_and_auth_are_open(pclient):
    assert pclient.get("/").status_code == 200
    assert pclient.get("/index.html").status_code == 200
    assert pclient.get("/api/auth").json() == {"required": True, "ok": False}


def test_correct_pin_sets_cookie_and_unlocks(pclient):
    r = unlock(pclient)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE}=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low and "max-age=31536000" in low
    assert "secure" not in low
    assert PIN not in cookie
    assert pclient.get("/api/jobs").status_code == 200
    assert pclient.get("/api/auth").json() == {"required": True, "ok": True}


def test_cookie_value_is_the_token(pclient):
    unlock(pclient)
    secret = pclient.app.state.secret
    assert pclient.cookies.get(auth.COOKIE) == auth.token(secret, PIN)


def test_forged_cookie_rejected(pclient):
    pclient.cookies.set(auth.COOKIE, "0" * 64)
    assert pclient.get("/api/jobs").status_code == 401
    assert pclient.get("/api/auth").json()["ok"] is False


def test_secure_flag_on_https(pin_settings):
    app = create_app(pin_settings, run_background=False)
    with TestClient(app, base_url="https://testserver") as c:
        r = unlock(c)
        assert r.status_code == 204
        assert "secure" in r.headers["set-cookie"].lower()


def test_wrong_pin_waits_then_401(pclient, monkeypatch):
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(api_mod, "AUTH_FAIL_DELAY", 1.0)
    monkeypatch.setattr(api_mod.asyncio, "sleep", fake_sleep)
    r = unlock(pclient, "9999")
    assert r.status_code == 401
    assert slept == [1.0]
    assert "set-cookie" not in r.headers
    assert pclient.get("/api/jobs").status_code == 401


def test_sixth_attempt_after_five_failures_is_429(pclient):
    for _ in range(5):
        assert unlock(pclient, "0000").status_code == 401
    r = unlock(pclient, "0000")
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many attempts, wait a minute"}
    assert unlock(pclient).status_code == 429     # checked before comparing


def test_limit_expires_after_window(pclient, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pclient.app.state.auth_limiter, "clock", lambda: now[0])
    for _ in range(5):
        unlock(pclient, "0000")
    assert unlock(pclient).status_code == 429
    now[0] += 61
    assert unlock(pclient).status_code == 204


def test_success_clears_failures(pclient):
    for _ in range(4):
        unlock(pclient, "0000")
    assert unlock(pclient).status_code == 204
    for _ in range(5):
        assert unlock(pclient, "0000").status_code == 401


def test_delete_clears_cookie(pclient):
    unlock(pclient)
    r = pclient.delete("/api/auth", headers=CSRF)
    assert r.status_code == 204
    assert f'{auth.COOKIE}=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    assert pclient.get("/api/jobs").status_code == 401


def test_auth_mutations_need_csrf(pclient):
    assert pclient.post("/api/auth", json={"pin": PIN}).status_code == 403
    assert pclient.delete("/api/auth").status_code == 403


def test_post_validates_body(pclient):
    assert pclient.post("/api/auth", json={}, headers=CSRF).status_code == 422


# --- no PIN configured ------------------------------------------------------------------------

def test_no_pin_no_middleware(settings):
    app = create_app(settings, run_background=False)
    assert not any(m.cls is auth.AuthMiddleware for m in app.user_middleware)
    with TestClient(app) as c:
        assert c.get("/api/jobs").status_code == 200
        assert c.get("/api/auth").json() == {"required": False, "ok": True}
        r = c.post("/api/auth", json={"pin": "whatever"}, headers=CSRF)
        assert r.status_code == 204
        assert "set-cookie" not in r.headers


def test_secret_on_app_state(pin_settings):
    app = create_app(pin_settings, run_background=False)
    assert app.state.secret == auth.secret_for(pin_settings)


# --- pure ASGI passthrough --------------------------------------------------------------------

def test_middleware_passes_non_http_scopes(pin_settings):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = auth.AuthMiddleware(inner, pin_settings)
    asyncio.run(mw({"type": "lifespan"}, None, None))
    asyncio.run(mw({"type": "websocket", "path": "/api/jobs", "headers": []}, None, None))
    assert seen == ["lifespan", "websocket"]


# --- Docker HEALTHCHECK -----------------------------------------------------------------------

def _healthcheck_path():
    import re
    from pathlib import Path
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    line = next(l for l in dockerfile.splitlines() if l.startswith("HEALTHCHECK"))
    assert line.endswith("CMD python -m app.healthcheck")
    from app import healthcheck
    return re.fullmatch(r"https?://127\.0\.0\.1:8080(/.*)", healthcheck.url({})).group(1)


@pytest.mark.parametrize("pin", [None, PIN])
def test_healthcheck_probe_answers_200_with_and_without_a_pin(tmp_path, pin):
    env = {"DATA_DIR": str(tmp_path)}
    if pin:
        env["ACCESS_PIN"] = pin
    s = Settings.from_env(env)
    s.ensure_dirs()
    path = _healthcheck_path()
    assert path == "/api/auth"
    with TestClient(create_app(s, run_background=False)) as c:
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "application/json"
