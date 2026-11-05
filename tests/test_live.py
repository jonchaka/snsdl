import subprocess
from pathlib import Path

import pytest

from app import downloader, live
from app import worker as worker_mod
from app.cookies import CookieStore
from app.jobs import DONE, DOWNLOADING, FAILED
from app.live import Stream, cookie_header, ffmpeg_args, format_selector, output_path, parse_progress, resolve
from app.probe import ProbeError


# -- selectors ---------------------------------------------------------------

def test_format_selector():
    assert format_selector("mp4", "720") == "best[protocol!*=dash][height<=720]/best[protocol!*=dash]"
    assert format_selector("mp4", "best") == "best[protocol!*=dash]"
    assert format_selector("mp3", "192") == "bestaudio[protocol!*=dash]/best[protocol!*=dash]"
    assert format_selector("audio", "best") == "bestaudio[protocol!*=dash]/best[protocol!*=dash]"


def test_constants():
    assert (live.MAX_RECORDINGS, live.STOP_GRACE, live.WRITE_INTERVAL, live.STREAM_TIMEOUT) == (3, 10.0, 2.0, 30)


# -- resolve -----------------------------------------------------------------

class _YDL:
    info: dict = {}
    opts_seen: list = []

    def __init__(self, opts):
        _YDL.opts_seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        assert download is False
        if isinstance(_YDL.info, BaseException):
            raise _YDL.info
        return _YDL.info


def _ydl(info):
    _YDL.info = info
    _YDL.opts_seen = []
    return _YDL


LIVE_INFO = {"title": "On air", "is_live": True, "url": "https://cdn.example.com/live.m3u8",
             "http_headers": {"User-Agent": "UA", "Referer": "https://example.com/"},
             "uploader": "Alice", "upload_date": "20261106", "description": "desc",
             "thumbnail": "https://i.example.com/t.jpg", "duration": None}


def test_resolve_live_stream(settings):
    stream = resolve("https://example.com/live", settings, "mp4", "720", ydl_class=_ydl(dict(LIVE_INFO)))
    assert stream == Stream(url="https://cdn.example.com/live.m3u8",
                            headers={"User-Agent": "UA", "Referer": "https://example.com/"},
                            title="On air", uploader="Alice", upload_date="2026-11-06", description="desc",
                            thumbnail="https://i.example.com/t.jpg", is_live=True, duration=None)
    opts = _YDL.opts_seen[0]
    assert opts["format"] == format_selector("mp4", "720") and opts["noplaylist"] is True
    assert opts["skip_download"] is True and "cookiefile" not in opts


def test_resolve_adds_cookie_header_for_the_stream_host(settings, tmp_path):
    jar = tmp_path / "c.txt"
    jar.write_text("# Netscape HTTP Cookie File\n"
                   ".example.com\tTRUE\t/\tFALSE\t0\tsid\tsecret123\n"
                   ".other.org\tTRUE\t/\tFALSE\t0\tno\tpe\n")
    stream = resolve("https://example.com/live", settings, "mp4", "best", cookiefile=jar,
                     ydl_class=_ydl(dict(LIVE_INFO)))
    assert stream.headers["Cookie"] == "sid=secret123"
    assert _YDL.opts_seen[0]["cookiefile"] == str(jar)
    assert cookie_header(jar, "https://nothing.net/x") is None
    assert cookie_header(None, "https://example.com/") is None
    assert cookie_header(tmp_path / "missing.txt", "https://example.com/") is None


def test_resolve_upcoming_and_not_live(settings):
    with pytest.raises(ProbeError, match="has not started yet"):
        resolve("https://x", settings, "mp4", "best", ydl_class=_ydl({"live_status": "is_upcoming"}))
    stream = resolve("https://x", settings, "mp4", "best",
                     ydl_class=_ydl({"title": "VOD", "is_live": False, "duration": 61.5, "url": "https://v"}))
    assert stream.is_live is False and stream.duration == 62 and stream.url is None


def test_resolve_requested_formats_is_an_error(settings):
    info = {**LIVE_INFO, "requested_formats": [{"url": "v"}, {"url": "a"}]}
    with pytest.raises(ProbeError, match="^no single live stream to record$"):
        resolve("https://x", settings, "mp4", "best", ydl_class=_ydl(info))


def test_resolve_wraps_extractor_errors(settings):
    with pytest.raises(ProbeError, match="boom"):
        resolve("https://x", settings, "mp4", "best", ydl_class=_ydl(RuntimeError("ERROR: boom")))


# -- ffmpeg_args ---------------------------------------------------------------

STREAM = Stream(url="https://cdn/live.m3u8", headers={"User-Agent": "UA", "Cookie": "a=1"}, is_live=True)
HEAD = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
        "-rw_timeout", "30000000"]
FRAG = "+frag_keyframe+empty_moov+default_base_moof"


def test_ffmpeg_args_mp4(settings, tmp_path):
    out = tmp_path / "j-x.mp4"
    assert ffmpeg_args(STREAM, out, "mp4", "720", settings) == HEAD + [
        "-headers", "User-Agent: UA\r\nCookie: a=1\r\n", "-i", "https://cdn/live.m3u8",
        "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", FRAG, "-f", "mp4", str(out)]


def test_ffmpeg_args_mp3_and_audio(settings, tmp_path):
    plain = Stream(url="https://cdn/a", is_live=True)
    out = tmp_path / "j-x.mp3"
    assert ffmpeg_args(plain, out, "mp3", "best", settings) == HEAD + [
        "-i", "https://cdn/a", "-vn", "-c:a", "libmp3lame", "-q:a", "0", "-f", "mp3", str(out)]
    assert ffmpeg_args(plain, out, "mp3", "192", settings) == HEAD + [
        "-i", "https://cdn/a", "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", str(out)]
    m4a = tmp_path / "j-x.m4a"
    assert ffmpeg_args(plain, m4a, "audio", "best", settings) == HEAD + [
        "-i", "https://cdn/a", "-vn", "-c:a", "copy", "-movflags", FRAG, "-f", "mp4", str(m4a)]


def test_ffmpeg_args_proxy(settings, tmp_path):
    out = tmp_path / "o.mp4"
    settings.proxy = "http://u:p@proxy:3128"
    args = ffmpeg_args(STREAM, out, "mp4", "best", settings)
    assert args[len(HEAD):len(HEAD) + 2] == ["-http_proxy", "http://u:p@proxy:3128"]
    settings.proxy = "https://proxy:443"
    assert "-http_proxy" in ffmpeg_args(STREAM, out, "mp4", "best", settings)
    settings.proxy = "socks5://proxy:1080"
    with pytest.raises(ValueError, match="SOCKS is not supported by ffmpeg"):
        ffmpeg_args(STREAM, out, "mp4", "best", settings)
    settings.proxy = None
    assert "-http_proxy" not in ffmpeg_args(STREAM, out, "mp4", "best", settings)


def test_output_path(settings, store):
    job = store.add("https://x", format="mp4")
    assert output_path(settings, job) == settings.tmp_dir / f"{job.id}-live.mp4"
    job.title = "My live show!"
    assert output_path(settings, job).name == f"{job.id}-My_live_show.mp4"
    job.custom_name = "chosen"
    job.format = "audio"
    assert output_path(settings, job).name == f"{job.id}-chosen.m4a"
    job.format = "mp3"
    assert output_path(settings, job).suffix == ".mp3"


def test_parse_progress():
    assert parse_progress("out_time_us=1500000\n") == ("out_time_us", "1500000")
    assert parse_progress("progress=end") == ("progress", "end")
    assert parse_progress("garbage\n") is None
    assert parse_progress("") is None


# -- record ------------------------------------------------------------------

class FakeProc:
    """Scripted ffmpeg: stdout yields `lines` (calling `on_line(n)` after each), the output file
    gets `output` bytes on the first line, stderr yields `stderr`."""

    def __init__(self, args, lines, rc=0, output=b"data", stderr=(), on_line=None, quit_exits=True):
        self.args = args
        self.out = Path(args[-1])
        self.lines = list(lines)
        self.rc = rc
        self.output = output
        self.on_line = on_line
        self.quit_exits = quit_exits
        self.count = 0
        self.killed = False
        self.returncode = None
        self.writes: list[bytes] = []
        proc = self

        class _Stdin:
            def write(self, data):
                proc.writes.append(data)

            def flush(self):
                pass

        class _Stderr:
            def __init__(self, items):
                self.items = [s.encode() for s in items]

            def readline(self):
                return self.items.pop(0) if self.items else b""

        self.stdin = _Stdin()
        self.stdout = self
        self.stderr = _Stderr(stderr)

    @property
    def quit(self):
        return b"q\n" in self.writes

    def readline(self):
        if self.killed or self.quit or not self.lines:
            return b""
        line = self.lines.pop(0)
        self.count += 1
        if self.count == 1 and self.output:
            self.out.write_bytes(self.output)
        if self.on_line:
            self.on_line(self.count)
        return line.encode()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.killed:
            self.returncode = -9
        elif self.quit and not self.quit_exits:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        else:
            self.returncode = self.rc
        return self.returncode

    def kill(self):
        self.killed = True


class Popen:
    """popen factory: records the call and the created FakeProc."""

    def __init__(self, **script):
        self.script = script
        self.proc = None
        self.kwargs = None

    def __call__(self, args, **kwargs):
        self.kwargs = kwargs
        self.proc = FakeProc(args, **self.script)
        return self.proc


PROGRESS = ["out_time_us=1000000\n", "total_size=100\n", "progress=continue\n",
            "out_time_us=2500000\n", "total_size=300\n", "progress=end\n"]


@pytest.fixture
def stream(monkeypatch):
    s = Stream(url="https://cdn/live.m3u8", headers={"User-Agent": "UA"}, title="Show", uploader="Alice",
               upload_date="2026-11-06", description="d", thumbnail=None, is_live=True)
    calls = []

    def fake_resolve(url, settings, fmt, quality, cookiefile=None, ydl_class=None):
        calls.append({"url": url, "fmt": fmt, "quality": quality, "cookiefile": cookiefile})
        return s

    monkeypatch.setattr(live, "resolve", fake_resolve)
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: False)
    s.calls = calls
    return s


def _live_job(store, **fields):
    job = store.add("https://example.com/live", live=True)
    store.update(job.id, status=DOWNLOADING, **fields)
    return job


def test_record_normal_end(settings, store, stream):
    job = _live_job(store)
    popen = Popen(lines=PROGRESS)
    assert live.record(job.id, store, settings, popen=popen) == [job.id]
    j = store.get(job.id)
    assert j.status == DONE and j.note == "Stream ended" and j.progress == 100
    assert j.duration == 3 and j.size == 4 and j.finished_at and j.filename == f"{job.id}-Show.mp4"
    assert j.title == "Show" and j.uploader == "Alice" and j.upload_date == "2026-11-06" and j.description == "d"
    assert j.speed is None and j.eta is None and j.stop_requested is False
    assert (settings.downloads_dir / j.filename).read_bytes() == b"data"
    assert not list(settings.tmp_dir.glob(f"{job.id}-*"))
    assert popen.kwargs == {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    assert popen.proc.writes == []
    assert stream.calls[0]["url"] == "https://example.com/live"


def test_record_writes_progress_on_the_interval(settings, store, stream):
    job = _live_job(store)
    t = [0.0]
    seen = []

    def on_line(n):
        seen.append((store.get(job.id).duration, store.get(job.id).size))
        t[0] += 1.0   # WRITE_INTERVAL is 2 s: every other line writes

    live.record(job.id, store, settings, popen=Popen(lines=PROGRESS, on_line=on_line), clock=lambda: t[0])
    # Each entry is read before that line's write: first write at line 1, then lines 3 and 5.
    assert seen[1] == (1, None) and seen[3] == (1, 100) and seen[5] == (2, 300)


def test_record_user_stop(settings, store, stream):
    job = _live_job(store)

    def on_line(n):
        if n == 2:
            store.update(job.id, stop_requested=True)

    popen = Popen(lines=PROGRESS, on_line=on_line, rc=255)
    assert live.record(job.id, store, settings, popen=popen) == [job.id]
    assert popen.proc.writes == [b"q\n"] and not popen.proc.killed
    j = store.get(job.id)
    assert j.status == DONE and j.note == "Recording stopped" and j.stop_requested is False


def test_record_stop_keeps_existing_note(settings, store, stream):
    job = _live_job(store)

    def on_line(n):
        store.update(job.id, stop_requested=True, note="Recording stopped: app restarting")

    live.record(job.id, store, settings, popen=Popen(lines=PROGRESS, on_line=on_line))
    assert store.get(job.id).note == "Recording stopped: app restarting"


def test_record_stop_kills_ffmpeg_that_ignores_quit(settings, store, stream):
    job = _live_job(store, stop_requested=True)
    popen = Popen(lines=PROGRESS, quit_exits=False)
    assert live.record(job.id, store, settings, popen=popen) == [job.id]
    assert popen.proc.writes == [b"q\n"] and popen.proc.killed
    assert store.get(job.id).status == DONE


def test_record_quit_ignores_broken_pipe(settings, store, stream):
    job = _live_job(store, stop_requested=True)
    popen = Popen(lines=PROGRESS)
    real = FakeProc.__init__

    def init(self, *a, **k):
        real(self, *a, **k)

        def broken(data):
            raise BrokenPipeError()

        self.stdin.write = broken

    FakeProc.__init__ = init
    try:
        assert live.record(job.id, store, settings, popen=popen) == [job.id]
    finally:
        FakeProc.__init__ = real
    assert store.get(job.id).status == DONE


def test_record_cancelled_job(settings, store, stream):
    job = _live_job(store)

    def on_line(n):
        if n == 2:
            store.remove(job.id)

    popen = Popen(lines=PROGRESS, on_line=on_line)
    assert live.record(job.id, store, settings, popen=popen) == []
    assert popen.proc.killed
    assert not list(settings.tmp_dir.glob(f"{job.id}-*")) and not list(settings.downloads_dir.iterdir())


def test_record_max_hours(settings, store, stream):
    settings.live_max_hours = 1
    job = _live_job(store)
    t = [0.0]

    def on_line(n):
        t[0] += 1800.0

    popen = Popen(lines=PROGRESS, on_line=on_line)
    live.record(job.id, store, settings, popen=popen, clock=lambda: t[0])
    assert popen.proc.writes == [b"q\n"]
    j = store.get(job.id)
    assert j.status == DONE and j.note == "Recording stopped after 1 h (LIVE_MAX_HOURS)"


def test_record_low_disk(settings, store, stream, monkeypatch):
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: True)
    job = _live_job(store)
    popen = Popen(lines=PROGRESS)
    live.record(job.id, store, settings, popen=popen)
    assert popen.proc.writes == [b"q\n"] and popen.proc.count == 1
    j = store.get(job.id)
    assert j.status == DONE and j.note == "Recording stopped: low disk space"


def test_record_nonzero_exit_with_output_is_done(settings, store, stream):
    job = _live_job(store)
    live.record(job.id, store, settings, popen=Popen(lines=PROGRESS, rc=1, stderr=["warn\n"]))
    j = store.get(job.id)
    assert j.status == DONE and j.note == "Stream ended with an error; the recording is kept up to that point"


def test_record_nonzero_exit_without_output_fails(settings, store, stream):
    settings.proxy = "http://user:pw@proxy:3128"
    stream.headers = {"Cookie": "sid=topsecret"}
    job = _live_job(store)
    err = ["first\n", f"open http://user:pw@proxy:3128 Cookie: sid=topsecret {settings.tmp_dir}/x\n",
           "Server returned 403 Forbidden\n"]
    popen = Popen(lines=PROGRESS, rc=1, output=b"", stderr=err)
    assert live.record(job.id, store, settings, popen=popen) == [job.id]
    j = store.get(job.id)
    assert j.status == FAILED and j.error == "Server returned 403 Forbidden" and j.finished_at
    assert "first" in j.log and "403" in j.log
    assert "topsecret" not in j.log and "user:pw" not in j.log and str(settings.tmp_dir) not in j.log
    assert not list(settings.tmp_dir.glob(f"{job.id}-*"))
    args = popen.proc.args
    assert args[args.index("-headers") + 1] == "Cookie: sid=topsecret\r\n"


def test_record_not_live_delegates_to_download(settings, store, stream, monkeypatch):
    stream.is_live = False
    calls = []

    def fake_download(job_id, url, st, se):
        calls.append((job_id, url))
        return ["x"]

    monkeypatch.setattr(downloader, "download", fake_download)
    job = _live_job(store)
    popen = Popen(lines=PROGRESS)
    assert live.record(job.id, store, settings, popen=popen) == ["x"]
    assert calls == [(job.id, job.url)] and popen.proc is None
    assert store.get(job.id).live is True


def test_record_not_live_keeps_use_once_cookies_for_download(settings, store, stream, monkeypatch):
    stream.is_live = False
    job = _live_job(store)
    cs = CookieStore(settings)
    cs.add(".example.com\tTRUE\t/\tFALSE\t0\tsid\tv\n", keep=False, job_id=job.id)
    seen = []
    monkeypatch.setattr(downloader, "download",
                        lambda j, u, st, se: seen.append(cs.once_path(j).is_file()) or [j])
    live.record(job.id, store, settings, popen=Popen(lines=PROGRESS))
    assert seen == [True]


def test_record_discards_use_once_cookies(settings, store, stream):
    job = _live_job(store)
    cs = CookieStore(settings)
    cs.add(".example.com\tTRUE\t/\tFALSE\t0\tsid\tv\n", keep=False, job_id=job.id)
    live.record(job.id, store, settings, popen=Popen(lines=PROGRESS))
    assert not cs.once_path(job.id).exists()
    assert stream.calls[0]["cookiefile"] is not None


def test_record_resolve_failure_fails_the_job(settings, store, monkeypatch):
    def boom(*a, **k):
        raise ProbeError("no single live stream to record")

    monkeypatch.setattr(live, "resolve", boom)
    job = _live_job(store)
    assert live.record(job.id, store, settings, popen=Popen(lines=PROGRESS)) == [job.id]
    j = store.get(job.id)
    assert j.status == FAILED and j.error == "no single live stream to record" and j.finished_at


def test_record_socks_proxy_fails_the_job(settings, store, stream):
    settings.proxy = "socks5://proxy:1080"
    job = _live_job(store)
    popen = Popen(lines=PROGRESS)
    live.record(job.id, store, settings, popen=popen)
    j = store.get(job.id)
    assert j.status == FAILED and "SOCKS" in j.error and popen.proc is None


def test_record_missing_ffmpeg_fails(settings, store, stream):
    job = _live_job(store)

    def popen(*a, **k):
        raise FileNotFoundError("ffmpeg")

    assert live.record(job.id, store, settings, popen=popen) == [job.id]
    assert store.get(job.id).status == FAILED


def test_record_removed_before_start(settings, store, stream):
    assert live.record("nope", store, settings, popen=Popen(lines=PROGRESS)) == []


def test_record_fetches_thumbnail_when_missing(settings, store, stream, monkeypatch):
    stream.thumbnail = "https://i.example.com/t.jpg"
    calls = []
    monkeypatch.setattr(downloader, "fetch_job_thumb", lambda j, u, st, se: calls.append((j, u)))
    job = _live_job(store)
    live.record(job.id, store, settings, popen=Popen(lines=PROGRESS))
    other = _live_job(store, thumb="have")
    live.record(other.id, store, settings, popen=Popen(lines=PROGRESS))
    assert calls == [(job.id, "https://i.example.com/t.jpg")]
