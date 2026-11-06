import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from gallery_dl import config as gdl_config
from gallery_dl import exception as gdl_exception
from gallery_dl.extractor.message import Message

from app import downloader, images
from app.jobs import DONE, FAILED, QUEUED
from app.probe import ProbeError

COOKIE_VALUE = "s3cr3tCookieValue987654"
POST = "https://www.instagram.com/p/ABC123/"


class FakeExtractor:
    """Yields scripted gallery-dl messages; an exception in the script is raised there."""

    def __init__(self, url, category, script):
        self.url, self.category, self.subcategory = url, category, "post"
        self.script = script

    def __iter__(self):
        for item in self.script:
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            yield item


def url_msg(n, ext="jpg", **kw):
    return (Message.Url, f"https://cdn.example/{n}.{ext}", {"extension": ext, "filename": str(n), "num": n, **kw})


def dir_msg(**kw):
    return (Message.Directory, None, kw)   # gallery-dl 1.32: every message is a 3-tuple


class Finder:
    """Stands in for gallery_dl.extractor.find: a fresh FakeExtractor per call (gallery-dl
    builds one for the listing pass and one for the DownloadJob)."""

    def __init__(self, script, category="instagram", job_script=None):
        self.script, self.category, self.job_script = script, category, job_script
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if self.category is None:
            return None
        script = self.script if self.calls == 1 or self.job_script is None else self.job_script
        return FakeExtractor(url, self.category, list(script))


class FakeJob:
    """Stands in for gallery_dl.job.DownloadJob: run() feeds every Url message to handle_url,
    whose base version writes the file where gallery-dl's config says. Like gallery-dl, run()
    logs exceptions and returns a status instead of raising."""

    instances: list = []
    fail_with = None     # exception raised (and logged by run) after the files
    status_override = None
    config_seen: dict = {}

    def __init__(self, extr):
        self.extractor = extr
        FakeJob.instances.append(self)
        FakeJob.config_seen = {
            "base": gdl_config.get((), "base-directory"), "directory": gdl_config.get((), "directory"),
            "filename": gdl_config.get((), "filename"), "skip": gdl_config.get((), "skip"),
            "mode": gdl_config.get(("output",), "mode"),
            "cookies": gdl_config.get(("extractor",), "cookies"),
            "proxy": gdl_config.get(("extractor",), "proxy"),
            "timeout": gdl_config.get(("extractor",), "timeout"),
            "retries": gdl_config.get(("extractor",), "retries"),
            "range": gdl_config.get(("extractor",), "image-range"),
            "whitelist": gdl_config.get(("extractor",), "whitelist"),
            "locked": images.CONFIG_LOCK.locked(),
        }

    def handle_url(self, url, kwdict):
        base = Path(gdl_config.get((), "base-directory"))
        name = gdl_config.get((), "filename").format(**kwdict)
        (base / name).write_bytes(b"x" * 10)

    def run(self):
        log = logging.getLogger(self.extractor.category)
        try:
            for msg in self.extractor:
                if msg[0] == Message.Url:
                    self.handle_url(msg[1], msg[2])
            if FakeJob.fail_with is not None:
                raise FakeJob.fail_with
        except gdl_exception.GalleryDLException as exc:
            log.error("%s: %s", exc.__class__.__name__, exc)
            return exc.code
        except Exception as exc:  # noqa: BLE001 - as gallery-dl's Job.run
            log.error("An unexpected error occurred: %s - %s", exc.__class__.__name__, exc)
            return 1
        return FakeJob.status_override or 0


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeJob.instances = []
    FakeJob.fail_with = None
    FakeJob.status_override = None
    FakeJob.config_seen = {}
    monkeypatch.setattr(images, "job_class", FakeJob)
    thumb_calls = []
    monkeypatch.setattr(downloader, "fetch_job_thumb",
                        lambda job_id, url, store, settings: thumb_calls.append((job_id, url)))
    return thumb_calls


def use(monkeypatch, script, **kw):
    finder = Finder(script, **kw)
    monkeypatch.setattr(images, "find", finder)
    return finder


def write_cookies(settings):
    settings.cookies_file.parent.mkdir(parents=True, exist_ok=True)
    settings.cookies_file.write_text(
        "# Netscape HTTP Cookie File\n"
        f".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\t{COOKIE_VALUE}\n")


# -- supports ------------------------------------------------------------------

def test_supports_allowlisted_category(monkeypatch):
    use(monkeypatch, [], category="instagram")
    assert images.supports(POST) is True


def test_supports_refuses_categories_outside_the_allowlist(monkeypatch):
    use(monkeypatch, [], category="directlink")
    assert images.supports("https://example.com/a.jpg") is False


def test_supports_no_extractor(monkeypatch):
    use(monkeypatch, [], category=None)
    assert images.supports("https://example.com/") is False


def test_supports_never_raises(monkeypatch):
    def boom(url):
        raise ValueError("bad")
    monkeypatch.setattr(images, "find", boom)
    assert images.supports(POST) is False


def test_supports_real_gallery_dl_find(monkeypatch):
    from gallery_dl import extractor
    monkeypatch.setattr(images, "find", extractor.find)   # matching only: no network
    assert images.supports("https://www.instagram.com/p/ABC123/") is True
    assert images.supports("https://bsky.app/profile/a.bsky.social/post/3kabc") is True
    assert images.supports("https://example.com/a.jpg") is False     # directlink: refused
    assert images.supports("https://example.com/") is False


# -- probe ---------------------------------------------------------------------

def test_probe_shape(monkeypatch, settings):
    use(monkeypatch, [dir_msg(description="Sunset\nat the beach", username="ann"),
                      url_msg(1, width=1080, height=1350, description="Sunset\nat the beach"),
                      url_msg(2, ext="mp4", width=720, height=1280),
                      url_msg(3, ext="PNG")])
    result = images.probe(POST, settings)
    assert result == {
        "kind": "images", "title": "Sunset", "count": 3, "video_count": 1,
        "files": [{"ext": "jpg", "width": 1080, "height": 1350},
                  {"ext": "mp4", "width": 720, "height": 1280},
                  {"ext": "png", "width": None, "height": None}],
        "thumb_url": "https://cdn.example/1.jpg"}


@pytest.mark.parametrize("kw,title", [
    ({"content": "C", "text": "T", "description": "D"}, "C"),
    ({"text": "Bluesky text", "description": "D"}, "Bluesky text"),
    ({"description": "", "title": "Ttl", "caption": "Cap"}, "Ttl"),
    ({"caption": "\n  Cap line\nmore"}, "Cap line"),
    ({"username": "ann"}, "ann"),
    ({"user": {"name": "Ann N", "id": 1}}, "Ann N"),
    ({"author": {"handle": "ann.bsky.social", "name": "Ann"}}, "ann.bsky.social"),
    ({"author": "plain"}, "plain"),
    ({}, None),
])
def test_probe_title_fallback_chain(monkeypatch, settings, kw, title):
    use(monkeypatch, [url_msg(1, **kw)])
    assert images.probe(POST, settings)["title"] == title


def test_probe_title_cut_to_120(monkeypatch, settings):
    use(monkeypatch, [url_msg(1, content="x" * 300)])
    assert images.probe(POST, settings)["title"] == "x" * 120


def test_probe_counts_videos_and_no_thumb_for_a_leading_video(monkeypatch, settings):
    use(monkeypatch, [url_msg(1, ext="mp4"), url_msg(2, ext="webm"), url_msg(3, ext="MOV"),
                      url_msg(4, ext="m4v"), url_msg(5)])
    result = images.probe(POST, settings)
    assert result["video_count"] == 4 and result["count"] == 5
    assert result["thumb_url"] is None


def test_probe_zero_files(monkeypatch, settings):
    use(monkeypatch, [dir_msg(description="text only")])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert str(e.value) == "No photos found in this post" and e.value.needs_login is False


@pytest.mark.parametrize("exc", [
    gdl_exception.AuthorizationError("nope"),
    gdl_exception.AuthenticationError(),
    gdl_exception.HttpError("'401 Unauthorized' for 'https://x'"),
    gdl_exception.AbortExtraction("You need to log in to see this"),
])
def test_probe_login_errors_need_login(monkeypatch, settings, exc):
    if isinstance(exc, gdl_exception.HttpError):
        exc.status = 401
    use(monkeypatch, [exc])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert e.value.needs_login is True


def test_probe_other_error_does_not_need_login(monkeypatch, settings):
    use(monkeypatch, [gdl_exception.NotFoundError("post")])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert e.value.needs_login is False and "could not be found" in str(e.value)


def test_probe_refuses_unlisted_category(monkeypatch, settings):
    use(monkeypatch, [url_msg(1)], category="directlink")
    with pytest.raises(ProbeError):
        images.probe(POST, settings)


def test_probe_caps_listing_at_max_files(monkeypatch, settings):
    use(monkeypatch, [url_msg(n) for n in range(1, images.MAX_FILES + 50)])
    assert images.probe(POST, settings)["count"] == images.MAX_FILES


def test_probe_holds_lock_and_clears_config(monkeypatch, settings):
    seen = {}

    def peek():
        seen["locked"] = images.CONFIG_LOCK.locked()
        seen["timeout"] = gdl_config.get(("extractor",), "timeout")
        seen["cookies"] = gdl_config.get(("extractor",), "cookies")

    use(monkeypatch, [peek, url_msg(1)])
    images.probe(POST, settings, cookiefile=Path("/tmp/cookies-x.txt"))
    assert seen == {"locked": True, "timeout": 30, "cookies": "/tmp/cookies-x.txt"}
    assert not images.CONFIG_LOCK.locked()
    assert gdl_config.get(("extractor",), "timeout") is None


def test_probe_clears_config_after_failure(monkeypatch, settings):
    use(monkeypatch, [gdl_exception.NotFoundError("post")])
    with pytest.raises(ProbeError):
        images.probe(POST, settings)
    assert gdl_config.get(("extractor",), "timeout") is None and not images.CONFIG_LOCK.locked()


# -- download ------------------------------------------------------------------

def test_download_happy_path(monkeypatch, settings, store, fakes):
    date = datetime(2026, 3, 4, 5, 6, tzinfo=timezone.utc)
    use(monkeypatch, [dir_msg(username="ann"),
                      url_msg(1, description="Sunset at the beach!\nsecond", username="ann", date=date),
                      url_msg(2, ext="mp4"), url_msg(3, ext="png")])
    job = store.add(POST, format="images")
    assert images.download(job.id, POST, store, settings) == [job.id]
    done = store.get(job.id)
    stem = "Sunset_at_the_beach"
    assert done.status == DONE and done.progress == 100
    assert done.filename == f"{job.id}-{stem}-01.jpg"
    assert done.sidecars == [f"{job.id}-{stem}-02.mp4", f"{job.id}-{stem}-03.png"]
    for name in [done.filename, *done.sidecars]:
        assert (settings.downloads_dir / name).is_file()
    assert done.size == 30 and done.duration is None and done.finished_at
    assert done.uploader == "ann" and done.upload_date == "2026-03-04"
    assert done.description == "Sunset at the beach!\nsecond"
    assert done.title == "Sunset at the beach!"
    assert fakes == [(job.id, "https://cdn.example/1.jpg")]
    assert not (settings.tmp_dir / f"{job.id}-images").exists()
    assert list(settings.tmp_dir.glob(f"{job.id}-*")) == []
    seen = FakeJob.config_seen
    assert seen["base"] == str(settings.tmp_dir / f"{job.id}-images")
    assert seen["directory"] == [] and seen["skip"] is False and seen["mode"] == "null"
    assert seen["timeout"] == 30 and seen["retries"] == 2 and seen["range"] == "1-200"
    assert seen["locked"] is True and seen["cookies"] is None
    assert not images.CONFIG_LOCK.locked()
    assert gdl_config.get((), "base-directory") is None


def test_download_uses_custom_name_and_existing_thumb(monkeypatch, settings, store, fakes):
    use(monkeypatch, [url_msg(1), url_msg(2)])
    job = store.add(POST, format="images", custom_name="my-trip", thumb="k1")
    images.download(job.id, POST, store, settings)
    done = store.get(job.id)
    assert done.filename == f"{job.id}-my-trip-01.jpg" and done.sidecars == [f"{job.id}-my-trip-02.jpg"]
    assert fakes == []


def test_download_without_text_names_files_post(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    done = store.get(job.id)
    assert done.filename == f"{job.id}-post-01.jpg" and done.sidecars == []


def test_download_taken_name_gets_unique_suffix(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1)])
    job = store.add(POST, format="images")
    (settings.downloads_dir / f"{job.id}-post-01.jpg").write_bytes(b"old")
    images.download(job.id, POST, store, settings)
    assert store.get(job.id).filename == f"{job.id}-post-01 (2).jpg"


def test_download_sanitises_extensions(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1, ext="JPG"), url_msg(2, ext="exe"), url_msg(3, ext="php")])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    done = store.get(job.id)
    assert done.filename.endswith("-01.jpg")
    assert [n[-7:] for n in done.sidecars] == ["-02.bin", "-03.bin"]


def test_ext_of():
    assert images.ext_of("JPEG") == "jpeg" and images.ext_of(".webp") == "webp"
    assert images.ext_of("svg") == "bin" and images.ext_of(None) == "bin" and images.ext_of("") == "bin"


def test_download_caps_at_max_files(monkeypatch, settings, store, fakes):
    use(monkeypatch, [url_msg(n) for n in range(1, images.MAX_FILES + 20)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    done = store.get(job.id)
    assert 1 + len(done.sidecars) == images.MAX_FILES
    assert done.sidecars[-1].endswith(f"-{images.MAX_FILES}.jpg")


def test_download_reports_progress_per_file(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1), url_msg(2), url_msg(3), url_msg(4)])
    job = store.add(POST, format="images")
    writes = []
    real = store.update

    def spy(job_id, **fields):
        if "progress" in fields and fields.get("status") is None:
            writes.append(fields["progress"])
        return real(job_id, **fields)

    monkeypatch.setattr(store, "update", spy)
    images.download(job.id, POST, store, settings)
    assert writes == [0, 25, 50, 75]


def test_download_cancel_midway_leaves_nothing(monkeypatch, settings, store, fakes):
    job = store.add(POST, format="images")
    use(monkeypatch, [url_msg(1), url_msg(2), url_msg(3)],
        job_script=[url_msg(1), lambda: store.remove(job.id), url_msg(2), url_msg(3)])
    assert images.download(job.id, POST, store, settings) == []
    assert list(settings.downloads_dir.iterdir()) == []
    assert not (settings.tmp_dir / f"{job.id}-images").exists()
    assert store.get(job.id) is None and fakes == []


def test_download_cancel_when_job_raises_through(monkeypatch, settings, store):
    """A job class that lets the cancel exception escape run() is handled the same way."""

    class Raw(FakeJob):
        def run(self):
            for msg in self.extractor:
                if msg[0] == Message.Url:
                    self.handle_url(msg[1], msg[2])
            return 0

    job = store.add(POST, format="images")
    monkeypatch.setattr(images, "job_class", Raw)
    use(monkeypatch, [url_msg(1)], job_script=[lambda: store.remove(job.id), url_msg(1)])
    assert images.download(job.id, POST, store, settings) == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_download_of_missing_job(monkeypatch, settings, store):
    finder = use(monkeypatch, [url_msg(1)])
    assert images.download("nope", POST, store, settings) == []
    assert finder.calls == 0


def test_download_failure_captures_log_without_cookie_values(monkeypatch, settings, store):
    write_cookies(settings)
    settings.proxy = "http://user:pw@proxy.example:3128"

    def echo_config():
        # A misbehaving extractor echoing its config (and the cookie file) into the log.
        path = gdl_config.get(("extractor",), "cookies")
        logging.getLogger("instagram").warning(
            "config: %s proxy=%s file=%s", path, gdl_config.get(("extractor",), "proxy"),
            Path(path).read_text())

    FakeJob.fail_with = gdl_exception.AbortExtraction(
        f"broke with {COOKIE_VALUE} at {settings.tmp_dir}/x")
    use(monkeypatch, [url_msg(1)], job_script=[echo_config, url_msg(1)])
    job = store.add(POST, format="images")
    assert images.download(job.id, POST, store, settings) == [job.id]
    failed = store.get(job.id)
    assert failed.status == FAILED and failed.finished_at
    assert "broke with" in failed.error
    assert "[instagram][warning] config:" in failed.log and "[instagram][error]" in failed.log
    for text in (failed.log, failed.error):
        assert COOKIE_VALUE not in text
        assert "pw@proxy" not in text
        assert "cookies-" not in text
        assert str(settings.tmp_dir) not in text
    assert not (settings.tmp_dir / f"{job.id}-images").exists()
    assert list(settings.downloads_dir.iterdir()) == []
    assert list(settings.tmp_dir.glob("cookies-*")) == []
    assert not images.CONFIG_LOCK.locked() and gdl_config.get(("extractor",), "cookies") is None
    assert all(not isinstance(h, images._Capture) for h in logging.getLogger().handlers)


def test_download_passes_cookie_copy_to_gallery_dl(monkeypatch, settings, store):
    write_cookies(settings)
    use(monkeypatch, [url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    assert "cookies-" in FakeJob.config_seen["cookies"]
    assert list(settings.tmp_dir.glob("cookies-*")) == []


def test_download_discards_use_once_cookies(monkeypatch, settings, store):
    from app.cookies import CookieStore
    use(monkeypatch, [url_msg(1)])
    job = store.add(POST, format="images")
    once = CookieStore(settings).once_path(job.id)
    once.parent.mkdir(parents=True, exist_ok=True)
    once.write_text("# Netscape HTTP Cookie File\n")
    images.download(job.id, POST, store, settings)
    assert not once.exists()


def test_download_listing_login_error_needs_login(monkeypatch, settings, store):
    use(monkeypatch, [gdl_exception.AuthorizationError("Login required")])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    failed = store.get(job.id)
    assert failed.status == FAILED and failed.needs_login is True
    assert "Login required" in failed.log


def test_download_zero_files_fails(monkeypatch, settings, store):
    use(monkeypatch, [dir_msg()])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    failed = store.get(job.id)
    assert failed.status == FAILED and failed.error == "No photos found in this post"


@pytest.mark.parametrize("exc", [
    gdl_exception.HttpError("'429 Too Many Requests' for 'https://x'"),
    gdl_exception.AbortExtraction("Read timed out."),
])
def test_download_transient_failure_queues_retry(monkeypatch, settings, store, exc):
    if isinstance(exc, gdl_exception.HttpError):
        exc.status = 429
    use(monkeypatch, [exc])
    job = store.add(POST, format="images")
    assert images.download(job.id, POST, store, settings) == [job.id]
    queued = store.get(job.id)
    assert queued.status == QUEUED and queued.attempts == 1 and queued.retry_at
    assert queued.error.startswith("retrying: ")


def test_download_transient_failure_without_retries_left_fails(monkeypatch, settings, store):
    exc = gdl_exception.HttpError("'503 Service Unavailable' for 'https://x'")
    exc.status = 503
    use(monkeypatch, [exc])
    job = store.add(POST, format="images")
    store.update(job.id, attempts=1)
    images.download(job.id, POST, store, settings)
    assert store.get(job.id).status == FAILED


def test_download_nonzero_status_from_job_fails(monkeypatch, settings, store):
    FakeJob.status_override = 4
    use(monkeypatch, [url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    failed = store.get(job.id)
    assert failed.status == FAILED and "status 4" in failed.error
    assert list(settings.downloads_dir.iterdir()) == []


def test_download_refuses_unlisted_category(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1)], category="directlink")
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    assert store.get(job.id).status == FAILED and FakeJob.instances == []


def test_lock_serialises_operations(monkeypatch, settings, store):
    """While the lock is held elsewhere, a probe waits for it."""
    import threading
    use(monkeypatch, [url_msg(1)])
    done = threading.Event()
    images.CONFIG_LOCK.acquire()
    try:
        t = threading.Thread(target=lambda: (images.probe(POST, settings), done.set()))
        t.start()
        assert not done.wait(0.2)
    finally:
        images.CONFIG_LOCK.release()
    t.join(5)
    assert done.is_set()



# -- fix wave: share links, feeds, whitelist, per-file transient, capture level -------

class ChildExtractor(FakeExtractor):
    """A child extractor class as a Queue kwdict's `_extractor`: from_url builds one."""
    category = "instagram"
    subcategory = "post"
    script: list = []
    built: list = []

    def __init__(self, url, category, script):
        super().__init__(url, category, script)
        self.subcategory = type(self).subcategory

    @classmethod
    def from_url(cls, url):
        cls.built.append(url)
        return cls(url, cls.category, list(cls.script))


def queue_msg(url, **kw):
    return (Message.Queue, url, kw)


def test_list_follows_a_queue_one_level(monkeypatch, settings):
    ChildExtractor.built = []
    ChildExtractor.script = [dir_msg(username="ann"), url_msg(1), url_msg(2),
                             queue_msg("https://www.instagram.com/p/DEEPER/", _extractor=ChildExtractor)]
    use(monkeypatch, [queue_msg("https://www.instagram.com/p/REAL/", _extractor=ChildExtractor)])
    result = images.probe("https://imgur.com/gallery/abcDEF1", settings)
    assert result["count"] == 2 and result["title"] == "ann"
    assert ChildExtractor.built == ["https://www.instagram.com/p/REAL/"]   # depth 1 only


def test_list_follows_a_queue_through_find(monkeypatch, settings):
    def finder(url):
        if url == POST:
            extr = FakeExtractor(url, "pinterest", [queue_msg("https://www.pinterest.com/pin/1/")])
        else:
            extr = FakeExtractor(url, "pinterest", [url_msg(1), url_msg(2)])
        extr.subcategory = "pin"
        return extr
    monkeypatch.setattr(images, "find", finder)
    assert images.probe(POST, settings)["count"] == 2


def test_list_ignores_a_queue_child_outside_sites(monkeypatch, settings):
    class Foreign(ChildExtractor):
        category = "directlink"
    Foreign.script = [url_msg(1)]
    use(monkeypatch, [queue_msg("https://example.com/a.jpg", _extractor=Foreign)])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert str(e.value) == images.NO_FILES


def test_list_ignores_a_queue_child_that_is_a_feed(monkeypatch, settings):
    class Board(ChildExtractor):
        category, subcategory = "pinterest", "board"
    Board.script = [url_msg(1)]
    use(monkeypatch, [queue_msg("https://www.pinterest.com/a/b/", _extractor=Board)])
    with pytest.raises(ProbeError):
        images.probe(POST, settings)


def test_list_cap_spans_parent_and_child(monkeypatch, settings):
    ChildExtractor.script = [url_msg(n) for n in range(images.MAX_FILES)]
    use(monkeypatch, [url_msg(9999), queue_msg("https://www.instagram.com/p/REAL/", _extractor=ChildExtractor)])
    assert images.probe(POST, settings)["count"] == images.MAX_FILES


def test_download_sets_the_child_whitelist(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    assert FakeJob.config_seen["whitelist"] == sorted(images.SITES)
    assert gdl_config.get(("extractor",), "whitelist") is None


@pytest.mark.parametrize("category,subcategory,ok", [
    ("instagram", "post", True), ("twitter", "tweet", True), ("twitter", "image", True),
    ("pinterest", "pin", True), ("pinterest", "pinit", True), ("reddit", "submission", True),
    ("reddit", "redirect", True), ("reddit", "image", True), ("bluesky", "post", True),
    ("imgur", "image", True), ("imgur", "album", True), ("imgur", "gallery", True),
    ("tiktok", "post", True), ("tiktok", "vmpost", True), ("facebook", "photo", True),
    ("facebook", "set", True), ("tumblr", "post", True), ("flickr", "image", True),
    ("flickr", "album", True), ("vsco", "image", True), ("vsco", "video", True),
    ("weibo", "status", True),
    ("instagram", "user", False), ("instagram", "stories", False), ("instagram", "photos", False),
    ("twitter", "timeline", False), ("twitter", "likes", False), ("twitter", "with-replies", False),
    ("pinterest", "board", False), ("pinterest", "allpins", False), ("reddit", "subreddit", False),
    ("tumblr", "tag", False), ("tiktok", "user", False), ("flickr", "favorite", False),
    ("facebook", "post", False), ("instagram", "some-new-feed", False), ("directlink", "post", False),
])
def test_supports_only_single_post_extractors(monkeypatch, category, subcategory, ok):
    def finder(url):
        extr = FakeExtractor(url, category, [])
        extr.subcategory = subcategory
        return extr
    monkeypatch.setattr(images, "find", finder)
    assert images.supports(POST) is ok


@pytest.mark.parametrize("url,ok", [
    ("https://x.com/jack/with_replies", False),
    ("https://www.pinterest.com/someone/pins/", False),
    ("https://www.instagram.com/someone/photos/", False),
    ("https://www.reddit.com/r/pics/", False),
    ("https://www.instagram.com/p/abc/", True),
    ("https://x.com/jack/status/20", True),
    ("https://www.pinterest.com/pin/1234567890/", True),
    ("https://bsky.app/profile/a.bsky.social/post/3k", True),
    ("https://imgur.com/gallery/abcDEF1", True),      # gallery-dl wants a 5+ char imgur id
    ("https://vm.tiktok.com/ZMabc/", True),
    ("https://www.reddit.com/r/pics/s/abcDEF1234", True),   # and a 10-char share id
])
def test_supports_with_the_real_find(monkeypatch, url, ok):
    from gallery_dl import extractor
    monkeypatch.setattr(images, "find", extractor.find)
    assert images.supports(url) is ok


def test_probe_refuses_a_feed(monkeypatch, settings):
    def finder(url):
        extr = FakeExtractor(url, "instagram", [url_msg(1)])
        extr.subcategory = "user"
        return extr
    monkeypatch.setattr(images, "find", finder)
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert str(e.value) == images.UNSUPPORTED


def _warn_http(message):
    return lambda: logging.getLogger("downloader.http").warning("%s (%s/%s)", message, 3, 3)


@pytest.mark.parametrize("message", [
    "'429 Too Many Requests' for 'https://cdn.example/1.jpg'",
    "'503 Service Unavailable' for 'https://cdn.example/1.jpg'",
    "HTTPSConnectionPool(host='cdn.example', port=443): Read timed out.",
])
def test_download_per_file_transient_failure_queues_retry(monkeypatch, settings, store, message):
    FakeJob.status_override = 4
    use(monkeypatch, [url_msg(1)], job_script=[_warn_http(message), url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    queued = store.get(job.id)
    assert queued.status == QUEUED and queued.attempts == 1 and queued.retry_at


@pytest.mark.parametrize("fail_with", [
    gdl_exception.AuthorizationError("login required"),
    gdl_exception.HttpError(response=SimpleNamespace(status_code=403, reason="Forbidden",
                                                     url="https://cdn.example/1.jpg")),
])
def test_download_per_file_transient_failure_that_is_login_walled_fails(monkeypatch, settings, store,
                                                                        fail_with):
    FakeJob.fail_with = fail_with
    use(monkeypatch, [url_msg(1)],
        job_script=[_warn_http("'429 Too Many Requests' for 'https://cdn.example/1.jpg'"), url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    failed = store.get(job.id)
    assert failed.status == FAILED and failed.needs_login is True
    assert failed.attempts == 0 and failed.retry_at is None


def test_download_per_file_other_http_failure_fails(monkeypatch, settings, store):
    FakeJob.status_override = 4
    use(monkeypatch, [url_msg(1)],
        job_script=[lambda: logging.getLogger("downloader.http").warning("'404 Not Found' for 'https://x'"),
                    url_msg(1)])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    assert store.get(job.id).status == FAILED


def test_capture_drops_debug_records(monkeypatch, settings, store):
    FakeJob.fail_with = gdl_exception.AbortExtraction("broke")
    use(monkeypatch, [url_msg(1)], job_script=[
        lambda: logging.getLogger("instagram").debug("debug-noise"),
        lambda: logging.getLogger("instagram").info("info-line"), url_msg(1)])
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        job = store.add(POST, format="images")
        images.download(job.id, POST, store, settings)
    finally:
        root.setLevel(old)
    failed = store.get(job.id)
    assert "info-line" in failed.log and "debug-noise" not in failed.log


def test_download_failure_log_starts_with_the_prior_yt_dlp_error(monkeypatch, settings, store):
    use(monkeypatch, [dir_msg()])
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings, prior_error="[Instagram] X: There is no video in this post")
    failed = store.get(job.id)
    assert failed.status == FAILED
    assert failed.log.splitlines()[0] == "yt-dlp: [Instagram] X: There is no video in this post"


def test_job_refuses_queue_children_not_allowed(monkeypatch):
    from gallery_dl import extractor
    monkeypatch.setattr(images, "find", extractor.find)
    seen = []

    class Base:
        def handle_queue(self, url, kwdict):
            seen.append(url)

    class Foreign:
        category = "directlink"

    job = images._make_job(Base, lambda kw: None)()
    job.handle_queue("https://example.com/a.jpg", {"_extractor": Foreign})
    job.handle_queue("https://www.instagram.com/p/X/", {"_extractor": ChildExtractor})
    job.handle_queue("https://www.instagram.com/p/Y/", {})
    job.handle_queue("https://www.pinterest.com/someone/pins/", {})   # found by URL, a feed
    assert seen == ["https://www.instagram.com/p/X/", "https://www.instagram.com/p/Y/"]


def test_job_refuses_a_queue_child_class_whose_subcategory_is_a_feed():
    seen = []

    class Base:
        def handle_queue(self, url, kwdict):
            seen.append(url)

    class Profile(ChildExtractor):
        category, subcategory = "instagram", "user"   # category allowed, subcategory a feed

    job = images._make_job(Base, lambda kw: None)()
    job.handle_queue("https://www.instagram.com/someone/", {"_extractor": Profile})
    assert seen == []


# -- final fix wave: extractor loggers, resilient listing, rate limit, cache file ----

class TracebackExtractor(FakeExtractor):
    """Like a real extractor: a plain logging.Logger as `log`, and a recoverable error path
    that calls self.log.traceback(exc) (only gallery-dl's LoggerAdapter has it)."""

    def __init__(self, url, category, script):
        super().__init__(url, category, script)
        self.log = logging.getLogger(category)

    def __iter__(self):
        try:
            raise ValueError("x")
        except ValueError as exc:
            self.log.traceback(exc)
            self.log.warning("Error while extracting media files (%s)", exc)
        yield from super().__iter__()


def test_list_wraps_the_root_extractor_logger(monkeypatch, settings):
    monkeypatch.setattr(images, "find", lambda url: TracebackExtractor(url, "instagram", [url_msg(1), url_msg(2)]))
    assert images.probe(POST, settings)["count"] == 2


def test_list_wraps_a_queue_child_logger(monkeypatch, settings):
    class Child(TracebackExtractor):
        category, subcategory = "instagram", "post"

        @classmethod
        def from_url(cls, url):
            return cls(url, "instagram", [url_msg(1), url_msg(2)])

    use(monkeypatch, [queue_msg("https://www.instagram.com/p/REAL/", _extractor=Child)])
    assert images.probe("https://imgur.com/gallery/abcDEF1", settings)["count"] == 2


def test_wrap_log_mirrors_gallery_dl_job():
    from gallery_dl import output
    extr = TracebackExtractor(POST, "instagram", [])
    images._wrap_log(extr)
    assert isinstance(extr.log, output.LoggerAdapter) and extr.log.logger is logging.getLogger("instagram")
    assert extr.log.extra["extractor"] is extr and extr.log.extra["path"].directory is None
    wrapped = extr.log
    assert images._wrap_log(extr).log is wrapped   # not wrapped twice


def test_real_twitter_extractor_logs_a_media_error_instead_of_raising(caplog):
    from gallery_dl import extractor
    extr = extractor.find("https://x.com/a/status/123")
    assert (extr.category, extr.subcategory) == ("twitter", "tweet")

    def broken(*a, **kw):
        raise ValueError("bad media")

    extr._extract_media = broken
    data = {"id_str": "123", "extended_entities": {"media": [{}]}}
    with pytest.raises(AttributeError):   # the reviewer's reproduction, unwrapped
        extr._extract_files(data, {})
    images._wrap_log(extr)
    extr.cards = extr.articles = extr.twitpic = False
    with caplog.at_level(logging.WARNING, logger="twitter"):
        assert extr._extract_files(data, {}) == []
    assert "Error while extracting media files (ValueError: bad media)" in caplog.text


def test_list_keeps_files_listed_before_an_error(monkeypatch, settings):
    use(monkeypatch, [url_msg(1), url_msg(2), gdl_exception.NotFoundError("post")])
    assert images.probe(POST, settings)["count"] == 2


def test_list_keeps_parent_files_when_a_queue_child_fails(monkeypatch, settings):
    ChildExtractor.script = [url_msg(2), ValueError("child broke")]
    use(monkeypatch, [url_msg(1), queue_msg("https://www.instagram.com/p/REAL/", _extractor=ChildExtractor),
                      url_msg(3)])
    assert images.probe(POST, settings)["count"] == 3


def test_list_error_with_no_files_still_fails(monkeypatch, settings):
    use(monkeypatch, [dir_msg(username="ann"), ValueError("broke")])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert "broke" in str(e.value)


def test_list_login_error_after_files_still_fails(monkeypatch, settings):
    use(monkeypatch, [url_msg(1), gdl_exception.AuthorizationError("Login required")])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert e.value.needs_login is True


def test_list_stop_extraction_is_not_an_error(monkeypatch, settings):
    use(monkeypatch, [gdl_exception.StopExtraction()])
    with pytest.raises(ProbeError) as e:
        images.probe(POST, settings)
    assert str(e.value) == images.NO_FILES


def test_download_listing_error_after_files_is_in_the_job_log(monkeypatch, settings, store):
    use(monkeypatch, [url_msg(1), gdl_exception.NotFoundError("post")], job_script=[url_msg(1)])
    FakeJob.status_override = 1   # make the job fail so its log is kept
    job = store.add(POST, format="images")
    images.download(job.id, POST, store, settings)
    assert "[instagram][warning] Listing stopped after 1 file(s) (NotFoundError" in store.get(job.id).log


def _session_config(settings):
    with images._session(settings, None, None):
        return (gdl_config.get(("downloader",), "rate"), gdl_config.get(("cache",), "file"))


def test_session_sets_the_rate_limit_only_when_set(settings):
    assert _session_config(settings)[0] is None
    settings.rate_limit = 2_000_000
    assert _session_config(settings)[0] == 2_000_000
    assert gdl_config.get(("downloader",), "rate") is None


def test_session_points_the_cache_file_under_the_data_dir(settings):
    path = _session_config(settings)[1]
    assert path == str(settings.data_dir / "config" / "gallery-dl-cache.sqlite3")
    assert (settings.data_dir / "config").is_dir()
    assert gdl_config.get(("cache",), "file") is None
