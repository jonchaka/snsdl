import re

import pytest

from app.jobs import DONE, DOWNLOADING, FAILED

pytestmark = pytest.mark.browser

T = 6000  # ms; the page polls every 2 s


def _expect():
    from playwright.sync_api import expect
    expect.set_options(timeout=T)
    return expect


def check(page, url="https://www.youtube.com/watch?v=jNQXAC9IVRw"):
    page.fill("#url", url)
    page.click("#check")
    _expect()(page.locator("#options")).to_be_visible()


def test_single_video_flow_queues_a_row_with_pause_and_cancel(page, server):
    expect = _expect()
    check(page)
    expect(page.locator("#ptitle")).to_have_text("Me at the zoo")
    expect(page.locator("#quality")).to_have_value("1080")
    page.check("input[name=fmt][value=audio]")
    expect(page.locator("#quality option")).to_have_text(["Original"])
    expect(page.locator("#quality")).to_have_value("best")
    page.check("input[name=fmt][value=mp4]")
    expect(page.locator("#quality")).to_have_value("1080")
    assert page.locator("#quality option").all_text_contents() == ["2160p", "1080p", "720p"]
    expect(page.locator("#plbox")).to_be_hidden()        # not a playlist: box really hidden
    page.click("#download")
    expect(page.locator("#options")).to_be_hidden()
    expect(page.locator("#url")).to_have_value("")
    row = page.locator("#jobs tr").first
    expect(row.locator(".badge")).to_have_text("queued")
    expect(row.locator("button.pause")).to_be_visible()
    expect(row.locator("button.remove")).to_have_text("Cancel")
    assert [j.status for j in server.store.list()] == ["queued"]


def test_pause_resume_retry_and_cancel_on_a_row(page, server):
    expect = _expect()
    check(page)
    page.click("#download")
    row = page.locator("#jobs tr").first
    row.locator("button.pause").click()
    expect(row.locator(".badge")).to_have_text("paused")
    expect(row.locator("button.resume")).to_be_visible()
    row.locator("button.resume").click()
    expect(row.locator(".badge")).to_have_text("queued")

    job = server.store.list()[0]
    server.store.update(job.id, status=DOWNLOADING, progress=42, speed=1258291, eta=45)
    expect(row.locator("td").nth(4)).to_have_text(re.compile(r"42% · 1\.2 MB/s · 0:45"))
    server.store.update(job.id, status=FAILED, error="Private video")
    expect(row.locator(".badge")).to_have_text("failed")
    expect(row.locator(".error")).to_have_text("Private video")
    row.locator("button.retry").click()
    expect(row.locator(".badge")).to_have_text("queued")

    row.locator("button.remove").click()                 # confirm auto-accepted
    expect(page.locator("#jobs td.empty")).to_have_text("No downloads yet.")
    assert server.store.list() == []


def test_playlist_flow_with_bar_and_cancel_remaining(page, server, fakes):
    expect = _expect()
    fakes["mode"] = "playlist"
    check(page, "https://www.youtube.com/playlist?list=PL1")
    box = page.locator("#plbox")
    expect(box).to_be_visible()
    expect(page.locator("#pllabel")).to_contain_text("Trending (3 videos)")
    expect(page.locator("#pllimit")).to_be_disabled()
    page.check("#plcheck")
    expect(page.locator("#pllimit")).to_be_enabled()
    expect(page.locator("#pllimit")).to_have_value("3")
    page.click("#download")
    expect(page.locator("#msg")).to_have_text("Queued 3 videos")
    expect(page.locator("#msg")).to_have_class(re.compile(r"\bok\b"))
    expect(page.locator("#jobs tr")).to_have_count(3)
    bar = page.locator("#playlists .pl")
    expect(bar).to_be_visible()
    expect(bar).to_contain_text("0 of 3 done")
    expect(bar.locator("button.plcancel")).to_be_visible()
    expect(bar.locator("button.plpause")).to_be_visible()
    expect(bar.locator("button.plremove")).to_have_count(0)

    done = server.store.list()[-1]
    server.store.update(done.id, status=DONE, progress=100, filename="x.mp4", size=10)
    expect(bar).to_contain_text("1 of 3 done")
    expect(bar.locator("button.plremove")).to_be_visible()

    bar.locator("button.plcancel").click()
    expect(page.locator("#jobs tr")).to_have_count(1)
    expect(page.locator("#jobs tr .badge")).to_have_text("done")
    assert [j.status for j in server.store.list()] == ["done"]


def test_global_queue_pause_toggle_and_banner(page, server):
    expect = _expect()
    expect(page.locator("#queuebanner")).to_be_hidden()
    expect(page.locator("#queuetoggle")).to_have_text("Pause queue")
    page.click("#queuetoggle")
    expect(page.locator("#queuebanner")).to_be_visible()
    expect(page.locator("#queuetoggle")).to_have_text("Resume queue")
    assert _status(server)["queue_paused"] is True
    page.click("#queuetoggle")
    expect(page.locator("#queuebanner")).to_be_hidden()
    assert _status(server)["queue_paused"] is False


def _status(server):
    import json, urllib.request
    with urllib.request.urlopen(server.base + "/api/status", timeout=5) as r:
        return json.load(r)


def test_footer_status_shows_usage_and_version(page, server):
    import yt_dlp
    expect = _expect()
    expect(page.locator("#status")).to_have_text(re.compile(rf"^0 B in 0 files · yt-dlp {re.escape(yt_dlp.version.__version__)}$"))


def test_url_query_parameter_prefills_and_checks(browser, server):
    expect = _expect()
    pg = browser.new_page()
    try:
        with pg.expect_request(lambda r: r.url.endswith("/api/probe")) as probe:
            pg.goto(server.base + "/?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DjNQXAC9IVRw&x=1")
        assert probe.value.post_data_json == {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}
        expect(pg.locator("#options")).to_be_visible()
        expect(pg.locator("#ptitle")).to_have_text("Me at the zoo")
        expect(pg.locator("#url")).to_have_value("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        assert pg.url == server.base + "/"           # query string removed from the address bar
    finally:
        pg.close()


def test_invalid_url_parameter_fills_the_box_but_does_not_probe(browser, server):
    expect = _expect()
    pg = browser.new_page()
    probes = []
    pg.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    try:
        pg.goto(server.base + "/?url=notaurl")
        expect(pg.locator("#url")).to_have_value("notaurl")
        pg.wait_for_timeout(500)                     # long enough for a stray submit to have fired
        expect(pg.locator("#options")).to_be_hidden()
        assert probes == []
        assert pg.url == server.base + "/"
    finally:
        pg.close()


def test_page_without_url_parameter_does_not_probe(browser, server):
    pg = browser.new_page()
    probes = []
    pg.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    try:
        pg.goto(server.base + "/")
        pg.wait_for_timeout(500)
        assert probes == []
        assert pg.locator("#url").input_value() == ""
    finally:
        pg.close()


def test_keep_survives_clear_all_and_unkeep_reverts(page, server, settings):
    expect = _expect()
    check(page)
    page.click("#download")
    row = page.locator("#jobs tr").first
    job = server.store.list()[0]
    (settings.downloads_dir / "zoo.mp4").write_bytes(b"z")
    server.store.update(job.id, status=DONE, progress=100, filename="zoo.mp4", size=1)
    expect(row.locator("button.keep")).to_have_text("Keep")
    expect(row.locator(".badge.kept")).to_have_count(0)

    row.locator("button.keep").click()
    expect(row.locator(".badge.kept")).to_have_text("kept")
    expect(row.locator("button.unkeep")).to_have_text("Unkeep")

    page.click("#clear")                                   # confirm auto-accepted
    page.wait_for_timeout(300)
    expect(page.locator("#jobs tr")).to_have_count(1)
    expect(row.locator(".badge.kept")).to_have_text("kept")
    assert (settings.downloads_dir / "zoo.mp4").exists()

    row.locator("button.unkeep").click()
    expect(row.locator(".badge.kept")).to_have_count(0)
    expect(row.locator("button.keep")).to_have_text("Keep")
    page.click("#clear")
    expect(page.locator("#jobs td.empty")).to_have_text("No downloads yet.")
    assert not (settings.downloads_dir / "zoo.mp4").exists()
