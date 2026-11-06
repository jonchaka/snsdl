import xml.etree.ElementTree as ET

import pytest

from app import feed, files
from app.jobs import DONE, Job
from app.subscriptions import Subscription

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
BASE = "https://dl.example"
KEY = "k" * 32


def _sub(title="My Channel"):
    return Subscription(id="ab12cd34", url="https://www.youtube.com/@chan/videos", format="mp3",
                        quality="320", title=title)


def _job(jid, filename, finished_at=None, created_at="2026-11-01T00:00:00+00:00", **fields):
    return Job(id=jid, url=f"https://e.com/{jid}", status=DONE, filename=filename,
               finished_at=finished_at, created_at=created_at, **fields)


# -- key / path ----------------------------------------------------------------------------

def test_feed_key_is_32_hex_and_deterministic():
    secret = b"s" * 32
    key = feed.feed_key(secret, "ab12cd34")
    assert len(key) == 32 and int(key, 16) >= 0
    assert feed.feed_key(secret, "ab12cd34") == key
    assert feed.feed_key(secret, "other") != key
    assert feed.feed_key(b"t" * 32, "ab12cd34") != key
    import hashlib
    import hmac
    assert key == hmac.new(secret, b"feed:ab12cd34", hashlib.sha256).hexdigest()[:32]


def test_feed_path():
    assert feed.feed_path("ab12cd34", KEY) == f"/feed/ab12cd34/{KEY}.xml"


# -- helpers -------------------------------------------------------------------------------

@pytest.mark.parametrize("name, mime", [
    ("a.mp3", "audio/mpeg"), ("a.m4a", "audio/mp4"), ("a.mp4", "video/mp4"), ("a.opus", "audio/ogg"),
    ("a.ogg", "audio/ogg"), ("a.oga", "audio/ogg"), ("a.webm", "video/webm"),
    ("a.mkv", "video/x-matroska"), ("a.flac", "audio/flac"), ("a.wav", "audio/wav"),
    ("A.MP3", "audio/mpeg"), ("a.b.mp4", "video/mp4"), ("a.srt", "application/octet-stream"),
    ("noext", "application/octet-stream"), ("", "application/octet-stream"),
])
def test_mime_for(name, mime):
    assert feed.mime_for(name) == mime


def test_mime_table():
    # The audio/video part of files.MEDIA_TYPES (one table) plus mkv, which only the feed serves.
    assert feed.MIME == {"mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "video/mp4", "opus": "audio/ogg",
                         "ogg": "audio/ogg", "oga": "audio/ogg", "webm": "video/webm",
                         "mkv": "video/x-matroska", "flac": "audio/flac", "wav": "audio/wav",
                         "mov": "video/quicktime", "m4v": "video/x-m4v"}
    assert all(files.MEDIA_TYPES[ext] == kind for ext, kind in feed.MIME.items() if ext != "mkv")


@pytest.mark.parametrize("seconds, text", [(0, "0:00:00"), (59, "0:00:59"), (61, "0:01:01"),
                                           (3600, "1:00:00"), (36000 + 125, "10:02:05")])
def test_hms(seconds, text):
    assert feed.hms(seconds) == text


def test_rfc2822():
    assert feed.rfc2822("2026-11-06T10:20:30.5+00:00") == "Fri, 06 Nov 2026 10:20:30 GMT"
    assert feed.rfc2822("2026-11-06T12:20:30+02:00") == "Fri, 06 Nov 2026 10:20:30 GMT"
    assert feed.rfc2822("2026-11-06T10:20:30") == "Fri, 06 Nov 2026 10:20:30 GMT"   # naive = UTC


# -- render --------------------------------------------------------------------------------

def test_render_fixture(tmp_path):
    f1 = tmp_path / "old one.mp3"
    f1.write_bytes(b"12345")
    f2 = tmp_path / "new.mp4"
    f2.write_bytes(b"1")
    old = _job("j1", "old one.mp3", finished_at="2026-11-05T08:00:00+00:00", title="Old & <one>",
               description="About\x01 it", duration=3725, thumb="abcdefgh123")
    new = _job("j2", "new.mp4", finished_at="2026-11-06T09:30:00+00:00", title="New", size=999)
    body = feed.render(_sub(), [(old, f1), (new, f2)], BASE, KEY)

    assert body.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert b'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in body
    root = ET.fromstring(body)
    assert root.tag == "rss" and root.get("version") == "2.0"
    channel = root.find("channel")
    assert channel.findtext("title") == "My Channel"
    assert channel.findtext("link") == "https://www.youtube.com/@chan/videos"
    assert channel.findtext("description") == "Downloads of My Channel from SNS Downloader"
    assert channel.findtext(f"{ITUNES}author") == "My Channel"
    assert channel.find("{http://www.w3.org/2005/Atom}link") is None

    items = channel.findall("item")
    assert [i.findtext("guid") for i in items] == ["j2", "j1"]   # newest first
    first, second = items
    assert first.find("guid").get("isPermaLink") == "false"
    assert first.findtext("title") == "New" and first.findtext("description") == ""
    assert first.findtext("pubDate") == "Fri, 06 Nov 2026 09:30:00 GMT"
    enc = first.find("enclosure").attrib
    assert enc == {"url": f"{BASE}/feed/ab12cd34/{KEY}/new.mp4", "length": "999", "type": "video/mp4"}
    assert first.find(f"{ITUNES}duration") is None and first.find(f"{ITUNES}image") is None

    assert second.findtext("title") == "Old & <one>" and second.findtext("description") == "About it"
    assert second.findtext("pubDate") == "Thu, 05 Nov 2026 08:00:00 GMT"
    enc = second.find("enclosure").attrib
    assert enc == {"url": f"{BASE}/feed/ab12cd34/{KEY}/old%20one.mp3", "length": "5", "type": "audio/mpeg"}
    assert second.findtext(f"{ITUNES}duration") == "1:02:05"
    assert second.find(f"{ITUNES}image").get("href") == f"{BASE}/feed/ab12cd34/{KEY}/thumbs/abcdefgh123"


def test_render_orders_by_finished_at_falling_back_to_created_at(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"x")
    a = _job("a", "a.mp3", finished_at="2026-11-03T00:00:00+00:00")
    b = _job("b", "b.mp3", created_at="2026-11-04T00:00:00+00:00")   # no finished_at
    c = _job("c", "c.mp3", finished_at="2026-11-02T00:00:00+00:00")
    body = feed.render(_sub(), [(c, p), (a, p), (b, p)], BASE, KEY)
    items = ET.fromstring(body).find("channel").findall("item")
    assert [i.findtext("guid") for i in items] == ["b", "a", "c"]
    assert items[0].findtext("pubDate") == "Wed, 04 Nov 2026 00:00:00 GMT"


def test_render_without_title_uses_the_url_and_no_items():
    sub = _sub(title=None)
    root = ET.fromstring(feed.render(sub, [], BASE, KEY))
    channel = root.find("channel")
    assert channel.findtext("title") == sub.url
    assert channel.findtext("description") == f"Downloads of {sub.url} from SNS Downloader"
    assert channel.findall("item") == []


def test_redact_hides_feed_keys_in_paths():
    assert feed.redact("/feed/abc/0123456789abcdef0123456789abcdef.xml") == "/feed/abc/***.xml"
    assert feed.redact('"GET /feed/abc/KEY/thumbs/t1 HTTP/1.1" 200') == '"GET /feed/abc/***/thumbs/t1 HTTP/1.1" 200'
    assert feed.redact("/api/jobs") == "/api/jobs"
    assert feed.redact("/files/feed/a/b") == "/files/feed/a/b"
    assert feed.redact('x "GET /feed/a/SECRET.xml HTTP/1.1"') == 'x "GET /feed/a/***.xml HTTP/1.1"'


def test_access_log_filter_redacts_record_args():
    import logging
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/feed/abc/secretkey.xml", "1.1", 200), None)
    assert feed.RedactFeedKeys().filter(rec) is True
    assert "secretkey" not in rec.getMessage() and "/feed/abc/***.xml" in rec.getMessage()


def test_install_log_filter_is_idempotent():
    import logging
    feed.install_log_filter("test.feed.access")
    feed.install_log_filter("test.feed.access")
    assert sum(isinstance(f, feed.RedactFeedKeys) for f in logging.getLogger("test.feed.access").filters) == 1
