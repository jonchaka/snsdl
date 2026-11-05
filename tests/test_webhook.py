import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

import pytest

from app import webhook
from app.jobs import DONE, FAILED, Job


def _job(**kw):
    base = dict(id="j1", url="https://example.com/v", status=DONE, filename="My clip #1.mp4",
                title="My clip", log="secret log lines")
    base.update(kw)
    return Job(**base)


def test_retry_constant():
    assert webhook.WEBHOOK_RETRY == 30.0


def test_payload_shape_with_public_url(settings):
    settings.public_url = "https://192.168.1.10:8080"
    data = json.loads(webhook.payload("done", _job(), settings))
    assert set(data) == {"event", "job", "file_url", "sent_at"}
    assert data["event"] == "done"
    assert data["job"]["id"] == "j1" and data["job"]["title"] == "My clip"
    assert "log" not in data["job"]           # public dict
    assert data["file_url"] == "https://192.168.1.10:8080/files/My%20clip%20%231.mp4"
    assert data["sent_at"].endswith("+00:00")


def test_payload_relative_file_url_without_public_url(settings):
    settings.public_url = None
    data = json.loads(webhook.payload("done", _job(), settings))
    assert data["file_url"] == "/files/My%20clip%20%231.mp4"


def test_payload_no_file_url_without_filename(settings):
    settings.public_url = "https://h.example"
    data = json.loads(webhook.payload("failed", _job(status=FAILED, filename=None, error="boom"), settings))
    assert data["event"] == "failed" and data["file_url"] is None
    assert data["job"]["error"] == "boom"


def test_payload_quotes_special_characters(settings):
    data = json.loads(webhook.payload("done", _job(filename="b?#.mp4"), settings))
    assert data["file_url"] == "/files/b%3F%23.mp4"


def test_payload_uses_the_media_layout_folder_quoted_per_segment(settings):
    data = json.loads(webhook.payload("done", _job(subdir="My Chan?", filename="a #1.mp4"), settings))
    assert data["file_url"] == "/files/My%20Chan%3F/a%20%231.mp4"


def test_sign_known_vector():
    # RFC 4231-style check against hmac directly, plus a fixed literal.
    body = b'{"event":"done"}'
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert webhook.sign("s3cret", body) == expected
    assert webhook.sign("key", b"The quick brown fox jumps over the lazy dog") == \
        "sha256=f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(status=200, exc=None, calls=None):
    def opener(req, timeout=None):
        if calls is not None:
            calls.append((req, timeout))
        if exc is not None:
            raise exc
        return _Resp(status)
    return opener


def test_send_posts_with_headers_and_signature(settings):
    settings.webhook_url = "https://hooks.example/x?token=abc"
    settings.webhook_secret = "s3cret"
    calls = []
    body = b'{"a":1}'
    assert webhook.send(settings, body, "done", opener=_opener(204, calls=calls)) is True
    (req, timeout), = calls
    assert timeout == 5
    assert req.full_url == "https://hooks.example/x?token=abc"
    assert req.get_method() == "POST" and req.data == body
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["content-type"] == "application/json"
    assert headers["x-sns-event"] == "done"
    assert headers["x-sns-signature"] == webhook.sign("s3cret", body)


def test_send_unsigned_without_secret(settings):
    settings.webhook_url = "http://hooks.example/x"
    settings.webhook_secret = None
    calls = []
    assert webhook.send(settings, b"{}", "failed", opener=_opener(200, calls=calls)) is True
    headers = {k.lower(): v for k, v in calls[0][0].header_items()}
    assert "x-sns-signature" not in headers
    assert headers["x-sns-event"] == "failed"


def test_send_false_without_url(settings):
    settings.webhook_url = None
    assert webhook.send(settings, b"{}", "done", opener=_opener(200)) is False


def test_send_false_on_non_2xx(settings, caplog):
    settings.webhook_url = "https://hooks.example/x?token=abc"
    with caplog.at_level(logging.WARNING, logger="app.webhook"):
        assert webhook.send(settings, b"{}", "done", opener=_opener(302)) is False
    assert "hooks.example" in caplog.text and "token=abc" not in caplog.text


@pytest.mark.parametrize("exc", [
    urllib.error.HTTPError("https://hooks.example/x", 500, "err", {}, None),
    urllib.error.URLError("refused"),
    TimeoutError("timed out"),
    OSError("reset"),
    ValueError("odd"),
])
def test_send_false_on_exception_and_logs_host_only(settings, caplog, exc):
    settings.webhook_url = "https://user:pw@hooks.example:8443/x?token=abc"
    settings.webhook_secret = "s3cret"
    body = b'{"private": "body"}'
    with caplog.at_level(logging.WARNING, logger="app.webhook"):
        assert webhook.send(settings, body, "done", opener=_opener(exc=exc)) is False
    assert "webhook to hooks.example failed" in caplog.text
    for leak in ("token=abc", "pw", "s3cret", "private"):
        assert leak not in caplog.text


def test_send_false_on_a_redirect_and_closes_it(settings, caplog):
    settings.webhook_url = "https://hooks.example/x?token=abc"
    exc = urllib.error.HTTPError("https://hooks.example/x", 302, "Found", {"Location": "http://evil/"}, None)
    closed = []
    exc.close = lambda: closed.append(True)
    with caplog.at_level(logging.WARNING, logger="app.webhook"):
        assert webhook.send(settings, b"{}", "done", opener=_opener(exc=exc)) is False
    assert closed == [True]
    assert "HTTP 302" in caplog.text and "evil" not in caplog.text


def test_default_opener_refuses_redirects():
    handlers = webhook._OPENER.handlers
    assert any(isinstance(h, webhook._NoRedirect) for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)
    assert webhook._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil/") is None


def test_default_opener_does_not_follow_a_real_redirect(settings):
    """A local server answering 302 to the POST: the send fails, the target is never hit."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(("POST", self.path))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            hits.append(("GET", self.path))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        settings.webhook_url = f"http://127.0.0.1:{server.server_port}/hook"
        assert webhook.send(settings, b"{}", "done") is False
    finally:
        server.shutdown()
        server.server_close()
    assert hits == [("POST", "/hook")]


def test_send_logs_only_the_type_of_other_exceptions(settings, caplog):
    settings.webhook_url = "https://hooks.example/x"
    with caplog.at_level(logging.WARNING, logger="app.webhook"):
        assert webhook.send(settings, b"{}", "done",
                            opener=_opener(exc=ValueError("bad https://hooks.example/x/secretpath"))) is False
    assert "ValueError" in caplog.text and "secretpath" not in caplog.text


# -- round 10: pending store ---------------------------------------------------

def test_pending_store_add_load_remove_clear(settings):
    path = settings.webhook_pending_file
    store = webhook.PendingStore(path)
    assert store.load() == []
    assert not path.exists()
    store.add("j1", "done", b'{"a": 1}')
    store.add("j2", "failed", "{\"é\": 2}".encode())
    assert path.exists()
    assert store.load() == [{"job_id": "j1", "event": "done", "body": '{"a": 1}'},
                            {"job_id": "j2", "event": "failed", "body": '{"é": 2}'}]
    assert webhook.PendingStore(path).load() == store.load()   # persisted
    store.remove("j1", "failed")   # wrong event: nothing removed
    assert len(store.load()) == 2
    store.remove("j1", "done")
    assert [e["job_id"] for e in store.load()] == ["j2"]
    store.clear()
    assert store.load() == []
    assert not list(path.parent.glob("*.tmp"))


def test_pending_store_creates_directory_on_first_write(tmp_path):
    path = tmp_path / "config" / "nested" / "webhook-pending.json"
    store = webhook.PendingStore(path)
    assert store.load() == [] and not path.parent.exists()
    store.add("j", "done", b"{}")
    assert json.loads(path.read_text()) == [{"job_id": "j", "event": "done", "body": "{}"}]


def test_pending_store_cap_drops_oldest(tmp_path, monkeypatch):
    assert webhook.PENDING_CAP == 100
    monkeypatch.setattr(webhook, "PENDING_CAP", 3)
    store = webhook.PendingStore(tmp_path / "p.json")
    for i in range(5):
        store.add(f"j{i}", "done", b"{}")
    assert [e["job_id"] for e in store.load()] == ["j2", "j3", "j4"]


@pytest.mark.parametrize("content", ["{not json", '{"a": 1}', '[1, 2]', '[{"job_id": "x"}]', ""])
def test_pending_store_corrupt_file_loads_empty(tmp_path, content):
    path = tmp_path / "p.json"
    path.write_text(content)
    store = webhook.PendingStore(path)
    assert store.load() == []
    store.add("j", "done", b"{}")   # a corrupt file is replaced on the next write
    assert store.load() == [{"job_id": "j", "event": "done", "body": "{}"}]
