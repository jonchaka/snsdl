import ssl

import pytest

from app import healthcheck


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(status, calls):
    def opener(url, **kwargs):
        calls.append((url, kwargs))
        return _Resp(status)
    return opener


def test_url_follows_https():
    assert healthcheck.url({}) == "http://127.0.0.1:8080/api/auth"
    assert healthcheck.url({"HTTPS": "0"}) == "http://127.0.0.1:8080/api/auth"
    assert healthcheck.url({"HTTPS": "1"}) == "https://127.0.0.1:8080/api/auth"
    assert healthcheck.url({"HTTPS": "on"}) == "https://127.0.0.1:8080/api/auth"


def test_check_ok_on_200_http_without_context():
    calls = []
    assert healthcheck.check({}, opener=_opener(200, calls)) == 0
    url, kwargs = calls[0]
    assert url == "http://127.0.0.1:8080/api/auth"
    assert "context" not in kwargs


def test_check_https_uses_unverified_context():
    calls = []
    assert healthcheck.check({"HTTPS": "1"}, opener=_opener(200, calls)) == 0
    url, kwargs = calls[0]
    assert url.startswith("https://")
    ctx = kwargs["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False


@pytest.mark.parametrize("status", [204, 401, 500])
def test_check_fails_on_other_status(status):
    assert healthcheck.check({}, opener=_opener(status, [])) == 1


def test_check_fails_on_exception():
    def opener(url, **kwargs):
        raise OSError("connection refused")
    assert healthcheck.check({}, opener=opener) == 1


def test_check_fails_on_bad_https_value():
    assert healthcheck.check({"HTTPS": "maybe"}, opener=_opener(200, [])) == 1
