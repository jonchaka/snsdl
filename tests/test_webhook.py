import hashlib
import hmac
import json
import logging
import urllib.error

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


def test_payload_quotes_slashes(settings):
    data = json.loads(webhook.payload("done", _job(filename="a/b?.mp4"), settings))
    assert data["file_url"] == "/files/a%2Fb%3F.mp4"


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
