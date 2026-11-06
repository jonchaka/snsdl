import io
import json
import logging
import urllib.error
from email import policy
from email.parser import BytesParser

import pytest

from app import telegram
from app.jobs import DONE, FAILED, QUEUED, Job

TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123"
CHAT = "-1001234567890"


@pytest.fixture
def tg(settings):
    settings.telegram_bot_token = TOKEN
    settings.telegram_chat_id = CHAT
    return settings


def _job(**kw) -> Job:
    base = dict(id="j1", url="https://example.com/v", status=DONE, title="Clip", format="mp4")
    base.update(kw)
    return Job(**base)


def _file(settings, name, size=4):
    path = settings.downloads_dir / name
    path.write_bytes(b"x" * size)
    return path


# -- caption ------------------------------------------------------------------

def test_caption_title_and_duration():
    assert telegram.caption(_job(duration=3725)) == "Clip · 1:02:05"
    assert telegram.caption(_job(duration=65)) == "Clip · 0:01:05"
    assert telegram.caption(_job(title=None)) == "https://example.com/v"
    assert len(telegram.caption(_job(title="t" * 2000))) == telegram.CAPTION_MAX


# -- plan ---------------------------------------------------------------------

def test_plan_small_video(tg):
    path = _file(tg, "a.mp4")
    [req] = telegram.plan(_job(filename="a.mp4", duration=10), tg)
    assert req.method == "sendVideo"
    assert req.files == {"video": path}
    assert req.fields["supports_streaming"] == "true"
    assert req.fields["caption"] == "Clip · 0:00:10"
    assert "chat_id" not in req.fields   # send() adds it


@pytest.mark.parametrize("public, link", [
    (None, "/files/a%20b.mp4"),
    ("https://nas.local:8080", "https://nas.local:8080/files/a%20b.mp4"),
])
def test_plan_large_video_sends_a_link(tg, monkeypatch, public, link):
    monkeypatch.setattr(telegram, "MAX_FILE_BYTES", 3)
    tg.public_url = public
    _file(tg, "a b.mp4", size=4)
    [req] = telegram.plan(_job(filename="a b.mp4"), tg)
    assert req.method == "sendMessage" and req.files == {}
    assert req.fields["text"].startswith("Clip\n" + link)
    assert (telegram.NO_PUBLIC_URL in req.fields["text"]) == (public is None)


@pytest.mark.parametrize("fmt", ["mp3", "audio"])
def test_plan_audio(tg, fmt):
    path = _file(tg, "a.m4a")
    [req] = telegram.plan(_job(filename="a.m4a", format=fmt, uploader="Band", duration=200), tg)
    assert req.method == "sendAudio" and req.files == {"audio": path}
    assert req.fields["title"] == "Clip" and req.fields["performer"] == "Band"
    assert req.fields["duration"] == "200"


def test_plan_large_audio_sends_a_link(tg, monkeypatch):
    monkeypatch.setattr(telegram, "MAX_FILE_BYTES", 3)
    _file(tg, "a.mp3", size=10)
    [req] = telegram.plan(_job(filename="a.mp3", format="mp3"), tg)
    assert req.method == "sendMessage" and "/files/a.mp3" in req.fields["text"]


def test_plan_missing_file_sends_nothing(tg):
    assert telegram.plan(_job(filename="gone.mp4"), tg) == []


def test_plan_photo_post_album_and_rest(tg, monkeypatch):
    monkeypatch.setattr(telegram, "MAX_PHOTO_BYTES", 8)
    names = [f"p{i:02d}.jpg" for i in range(12)]
    for i, n in enumerate(names):
        _file(tg, n, size=20 if i == 3 else 4)   # p03 is too large for a photo
    job = _job(format="images", filename=names[0], sidecars=names[1:], title="Post")
    album, rest = telegram.plan(job, tg)
    assert album.method == "sendMediaGroup"
    media = json.loads(album.fields["media"])
    assert len(media) == telegram.ALBUM_MAX
    assert all(m["type"] == "photo" for m in media)
    assert media[0]["caption"] == "Post" and all("caption" not in m for m in media[1:])
    assert [m["media"] for m in media] == [f"attach://photo{i}" for i in range(10)]
    sent = [album.files[m["media"].removeprefix("attach://")].name for m in media]
    assert sent == [n for n in names if n != "p03.jpg"][:10]
    assert rest.method == "sendMessage"
    assert rest.fields["text"].startswith("2 more files\n/api/zip?ids=j1")


def test_plan_photo_post_skips_missing_and_non_photos(tg):
    _file(tg, "a.jpg"), _file(tg, "b.png"), _file(tg, "c.mp4")
    job = _job(format="images", filename="a.jpg", sidecars=["gone.jpg", "b.png", "c.mp4"])
    album, rest = telegram.plan(job, tg)
    assert [m["media"] for m in json.loads(album.fields["media"])] == ["attach://photo0", "attach://photo1"]
    assert rest.fields["text"].startswith("1 more file\n")


def test_plan_photo_post_single_photo_uses_send_photo(tg):
    path = _file(tg, "a.jpg")
    [req] = telegram.plan(_job(format="images", filename="a.jpg", sidecars=[]), tg)
    assert req.method == "sendPhoto" and req.files == {"photo": path}


def test_plan_photo_post_with_nothing_sendable(tg, monkeypatch):
    tg.public_url = "https://nas.local"
    _file(tg, "a.mp4"), _file(tg, "b.mov")
    [req] = telegram.plan(_job(format="images", filename="a.mp4", sidecars=["b.mov"], title="Post"), tg)
    assert req.method == "sendMessage"
    assert req.fields["text"] == "Post\n2 files\nhttps://nas.local/api/zip?ids=j1"


def test_plan_failed_job():
    [req] = telegram.plan(_job(status=FAILED, error="HTTP Error 403"), None)
    assert req == telegram.Request("sendMessage", {"text": "Failed: Clip · HTTP Error 403"}, {})
    [req] = telegram.plan(_job(status=FAILED, title=None, error=None), None)
    assert req.fields["text"] == "Failed: https://example.com/v"


def test_plan_not_ended_job_sends_nothing(tg):
    assert telegram.plan(_job(status=QUEUED, filename="a.mp4"), tg) == []


def test_plan_split_part_is_its_own_video(tg):
    path = _file(tg, "Clip - 02 Chapter.mp4")
    [req] = telegram.plan(_job(id="j2", filename=path.name, title="Clip - Chapter", playlist_id="pl1"), tg)
    assert req.method == "sendVideo" and req.files == {"video": path}


def test_plan_finds_a_kept_file(tg, keep_dir):
    path = keep_dir / "a.mp4"
    path.write_bytes(b"v")
    [req] = telegram.plan(_job(filename="a.mp4", keep=True), tg)
    assert req.files == {"video": path}


# -- send ---------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, body=b'{"ok": true, "result": {}}'):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, result=None):
        self.requests, self.timeouts = [], []
        self.result = result if result is not None else _Resp()

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _parse(req):
    raw = b"Content-Type: " + req.get_header("Content-type").encode() + b"\r\n\r\n" + req.data
    msg = BytesParser(policy=policy.HTTP).parsebytes(raw)
    assert msg.is_multipart()
    out = {}
    for part in msg.iter_parts():
        out[part.get_param("name", header="content-disposition")] = (
            part.get_filename(), part.get_payload(decode=True))
    return out


def test_send_builds_multipart_with_fields_and_files(tg, tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / 'b "x".jpg'
    a.write_bytes(b"\x00\xffAAA\r\n--not-a-boundary")
    b.write_bytes(b"BBB")
    opener = _Opener()
    media = json.dumps([{"type": "photo", "media": "attach://photo0", "caption": "Päst ✓"},
                        {"type": "photo", "media": "attach://photo1"}])
    req = telegram.Request("sendMediaGroup", {"media": media}, {"photo0": a, "photo1": b})
    assert telegram.send(tg, req, opener=opener) is True
    [sent] = opener.requests
    assert sent.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup"
    assert sent.get_method() == "POST"
    assert opener.timeouts == [telegram.TIMEOUT]
    parts = _parse(sent)
    assert parts["chat_id"] == (None, CHAT.encode())
    assert json.loads(parts["media"][1].decode()) == json.loads(media)
    assert parts["photo0"] == ("a.jpg", a.read_bytes())
    assert parts["photo1"] == ("b _x_.jpg", b"BBB")


def test_send_message_url_and_fields(tg):
    opener = _Opener()
    assert telegram.send(tg, telegram.Request("sendMessage", {"text": "hi"}, {}), opener=opener)
    assert opener.requests[0].full_url.endswith("/sendMessage")
    assert _parse(opener.requests[0]) == {"chat_id": (None, CHAT.encode()), "text": (None, b"hi")}


@pytest.mark.parametrize("result", [
    _Resp(200, b'{"ok": false, "description": "nope"}'),
    _Resp(200, b"not json"),
    _Resp(204, b""),
    urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"ok":false,"description":"Bad Request"}')),
    urllib.error.URLError("dns"),
    TimeoutError(),
    OSError("x"),
])
def test_send_false_on_failures(tg, result):
    assert telegram.send(tg, telegram.Request("sendMessage", {"text": "x"}, {}), opener=_Opener(result)) is False


def test_send_false_when_unconfigured(settings):
    opener = _Opener()
    assert telegram.send(settings, telegram.Request("sendMessage", {"text": "x"}, {}), opener=opener) is False
    assert opener.requests == []


def test_send_log_never_contains_the_token(tg, caplog):
    echo = json.dumps({"ok": False, "error_code": 404,
                       "description": f"Not Found: bot{TOKEN} " + "y" * 300}).encode()
    err = urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendVideo", 404, "Not Found", {},
                                 io.BytesIO(echo))
    caplog.set_level(logging.DEBUG)
    assert telegram.send(tg, telegram.Request("sendVideo", {}, {}), opener=_Opener(err)) is False
    assert telegram.send(tg, telegram.Request("sendVideo", {}, {}), opener=_Opener(_Resp(200, echo))) is False
    assert telegram.send(tg, telegram.Request("sendVideo", {}, {}),
                         opener=_Opener(urllib.error.URLError(f"bad {TOKEN}"))) is False
    text = caplog.text
    assert TOKEN not in text and "api.telegram.org" not in text
    assert "telegram sendVideo failed: HTTP 404 (Not Found: bot***" in text
    assert "telegram sendVideo failed: URLError" in text
    for rec in caplog.records:
        desc = rec.getMessage().partition("(")[2]
        assert len(desc) <= telegram.DESCRIPTION_MAX + 1


def test_redact():
    assert telegram.redact(f"a {TOKEN} b {TOKEN}", TOKEN) == "a *** b ***"
    assert telegram.redact("a", None) == "a"


# -- deliver ------------------------------------------------------------------

def test_deliver_stops_at_the_first_failure(tg, monkeypatch):
    reqs = [telegram.Request("sendMediaGroup", {}, {}), telegram.Request("sendMessage", {}, {})]
    monkeypatch.setattr(telegram, "plan", lambda job, s: reqs)
    sent = []
    monkeypatch.setattr(telegram, "send", lambda s, r: sent.append(r.method) or False)
    assert telegram.deliver(tg, _job()) is False
    assert sent == ["sendMediaGroup"]
    sent.clear()
    monkeypatch.setattr(telegram, "send", lambda s, r: sent.append(r.method) or True)
    assert telegram.deliver(tg, _job()) is True
    assert sent == ["sendMediaGroup", "sendMessage"]


def test_deliver_sends_the_real_file(tg, monkeypatch):
    _file(tg, "a.mp4", size=7)
    opener = _Opener()
    monkeypatch.setattr(telegram, "_OPENER", type("O", (), {"open": staticmethod(opener)})())
    assert telegram.deliver(tg, _job(filename="a.mp4")) is True
    [req] = opener.requests
    assert req.full_url.endswith("/sendVideo")
    assert _parse(req)["video"] == ("a.mp4", b"x" * 7)
