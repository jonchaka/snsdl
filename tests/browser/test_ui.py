import json
import re

import pytest

from app.jobs import DONE, DOWNLOADING, FAILED, QUEUED

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
    expect(page.locator("#quality option")).to_have_text(["Original · ~12 MB"])
    expect(page.locator("#quality")).to_have_value("best")
    page.check("input[name=fmt][value=mp4]")
    expect(page.locator("#quality")).to_have_value("1080")
    expect(page.locator("#subbox")).to_be_visible()
    expect(page.locator("#subs option")).to_have_text(
        ["None", "English", "German", "English (Original) (auto)", "Japanese (auto)"])
    page.check("input[name=fmt][value=mp3]")
    expect(page.locator("#subbox")).to_be_hidden()
    page.check("input[name=fmt][value=mp4]")
    page.select_option("#subs", "auto:en-orig")
    assert page.locator("#quality option").all_text_contents() == ["2160p", "1080p · ~180 MB", "720p"]
    expect(page.locator("#plbox")).to_be_hidden()        # not a playlist: box really hidden
    page.click("#download")
    expect(page.locator("#options")).to_be_hidden()
    expect(page.locator("#url")).to_have_value("")
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".badge")).to_have_text("queued")
    expect(row.locator("button.pause")).to_be_visible()
    expect(row.locator("button.remove")).to_have_text("Cancel")
    expect(row.locator(".fmt")).to_have_text("MP4 1080p · EN auto subs")
    assert server.store.list()[0].subtitles == "auto:en-orig"
    assert [j.status for j in server.store.list()] == ["queued"]


def test_pause_resume_retry_and_cancel_on_a_row(page, server):
    expect = _expect()
    check(page)
    page.click("#download")
    row = page.locator("#jobs tr.job").first
    row.locator("button.pause").click()
    expect(row.locator(".badge")).to_have_text("paused")
    expect(row.locator("button.resume")).to_be_visible()
    row.locator("button.resume").click()
    expect(row.locator(".badge")).to_have_text("queued")

    job = server.store.list()[0]
    server.store.update(job.id, status=DOWNLOADING, progress=42, speed=1258291, eta=45)
    expect(row.locator(".progress-text")).to_have_text(re.compile(r"42% · 1\.2 MB/s · 0:45"))
    server.store.update(job.id, status=FAILED, error="Private video")
    expect(row.locator(".badge")).to_have_text("failed")
    expect(row.locator(".error")).to_have_text("Private video")
    row.locator("button.retry").click()
    expect(row.locator(".badge")).to_have_text("queued")

    row.locator("button.remove").click()                 # confirm auto-accepted
    expect(page.locator("#jobs td.empty strong")).to_have_text("No downloads yet")
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
    expect(page.locator("#jobs tr.job")).to_have_count(3)
    bar = page.locator("#jobs tr.pl")
    expect(bar).to_be_visible()
    expect(bar).to_contain_text("0 of 3 done")
    expect(bar.locator("button.plcancel")).to_be_visible()
    expect(bar.locator("button.plpause")).to_be_visible()
    expect(bar.locator("button.plremove")).to_have_count(0)
    expect(bar.locator("a.plzip")).to_have_count(0)

    done = server.store.list()[-1]
    server.store.update(done.id, status=DONE, progress=100, filename="x.mp4", size=10)
    expect(bar).to_contain_text("1 of 3 done")
    expect(bar.locator("button.plremove")).to_be_visible()
    expect(bar.locator("a.plzip")).to_have_text("Download all (1)")
    expect(bar.locator("a.plzip")).to_have_attribute("href", f"/api/playlists/{done.playlist_id}/zip")

    bar.locator("button.plcancel").click()
    expect(page.locator("#jobs tr.job")).to_have_count(1)
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
    import json
    import urllib.request
    with urllib.request.urlopen(server.base + "/api/status", timeout=5) as r:
        return json.load(r)


def test_footer_status_shows_usage_and_version(page, server):
    import yt_dlp
    expect = _expect()
    expect(page.locator("#status")).to_have_text(
        re.compile(rf"^0 B in 0 files · yt-dlp {re.escape(yt_dlp.version.__version__)}( · impersonat\w+ \w+)?$"))


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
    pg.on("request", lambda r: probes.append(r) if r.url.endswith(("/api/probe", "/api/search")) else None)
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


def test_shared_text_link_prefills_and_checks(browser, server):
    expect = _expect()
    pg = browser.new_page()
    probes = []
    pg.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    try:
        with pg.expect_request(lambda r: r.url.endswith("/api/probe")) as probe:
            pg.goto(server.base + "/?text=Look%20at%20this%20https%3A%2F%2Fexample.com%2Fv%2F42%20now")
        assert probe.value.post_data_json == {"url": "https://example.com/v/42"}
        expect(pg.locator("#url")).to_have_value("https://example.com/v/42")
        pg.wait_for_timeout(300)
        assert len(probes) == 1
        assert pg.url == server.base + "/"
    finally:
        pg.close()


def test_shared_link_trailing_punctuation_is_stripped(browser, server):
    from urllib.parse import quote
    expect = _expect()
    cases = [("See (https://example.com/v/7).", "https://example.com/v/7"),
             ("https://example.com/v/8。見て", "https://example.com/v/8")]
    for text, want in cases:
        pg = browser.new_page()
        try:
            with pg.expect_request(lambda r: r.url.endswith("/api/probe")) as probe:
                pg.goto(server.base + "/?text=" + quote(text, safe=""))
            assert probe.value.post_data_json == {"url": want}
            expect(pg.locator("#url")).to_have_value(want)
        finally:
            pg.close()


def test_shared_text_without_a_link_does_nothing(browser, server):
    pg = browser.new_page()
    probes = []
    pg.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    try:
        pg.goto(server.base + "/?title=Funny%20clip&text=no%20link%20here")
        pg.wait_for_timeout(500)
        assert probes == []
        assert pg.locator("#url").input_value() == ""
        assert pg.url == server.base + "/"
    finally:
        pg.close()


def test_shared_title_link_is_used_when_text_has_none(browser, server):
    expect = _expect()
    pg = browser.new_page()
    try:
        with pg.expect_request(lambda r: r.url.endswith("/api/probe")) as probe:
            pg.goto(server.base + "/?title=https%3A%2F%2Fexample.com%2Fa&text=no%20link")
        assert probe.value.post_data_json == {"url": "https://example.com/a"}
        expect(pg.locator("#url")).to_have_value("https://example.com/a")
    finally:
        pg.close()


def test_manifest_link_and_service_worker_controls_after_reload(browser, server):
    context = browser.new_context()
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        assert pg.locator('link[rel="manifest"]').get_attribute("href") == "/manifest.webmanifest"
        assert pg.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)") is True
        pg.reload()
        assert pg.evaluate("!!navigator.serviceWorker.controller") is True
        pg.evaluate("navigator.serviceWorker.getRegistrations()"
                    ".then(rs => Promise.all(rs.map(r => r.unregister())))")
    finally:
        context.close()


def test_keep_survives_clear_all_and_unkeep_reverts(page, server, settings):
    expect = _expect()
    check(page)
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(1)   # the POST has landed before the store is read
    row = page.locator("#jobs tr.job").first
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
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    expect(row.locator(".badge.kept")).to_have_text("kept")
    assert (settings.downloads_dir / "zoo.mp4").exists()

    row.locator("button.unkeep").click()
    expect(row.locator(".badge.kept")).to_have_count(0)
    expect(row.locator("button.keep")).to_have_text("Keep")
    page.click("#clear")
    expect(page.locator("#jobs td.empty strong")).to_have_text("No downloads yet")
    assert not (settings.downloads_dir / "zoo.mp4").exists()


COOKIES = (".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tSECRET\n"
           "www.facebook.com\tFALSE\t/\tTRUE\t0\tc_user\tSECRET2\n")


def test_failed_login_wall_row_offers_cookies_and_use_once_retries_it(page, server, settings):
    expect = _expect()
    expect(page.locator("#cookiesbtn")).to_be_hidden()           # nothing stored yet
    check(page)
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(1)   # the POST has landed before the store is read
    row = page.locator("#jobs tr.job").first
    job = server.store.list()[0]
    server.store.update(job.id, status=FAILED, error="You need to log in to access this content", needs_login=True)
    expect(row.locator("button.addcookies")).to_have_text("Add cookies")
    expect(row.locator("button.learnhow")).to_have_text("Learn how")

    row.locator("button.learnhow").click()
    dlg = page.locator("#cookiedlg")
    expect(dlg).to_be_visible()
    expect(dlg).to_contain_text("Get cookies.txt LOCALLY")
    expect(dlg).to_contain_text("Cookies are your login")
    page.click("#cookiecancel")
    expect(dlg).to_be_hidden()

    row.locator("button.addcookies").click()
    expect(dlg).to_be_visible()
    expect(page.locator("input[name=ckeep][value=once]")).to_be_checked()
    page.click("#cookiesave")                                    # empty: validation message, dialog stays
    expect(page.locator("#cookieerr")).to_contain_text("Paste the cookies")
    page.fill("#cookietext", "<html>not cookies</html>")
    page.click("#cookiesave")
    expect(page.locator("#cookieerr")).to_contain_text("7 tab-separated")
    page.fill("#cookietext", COOKIES)
    page.click("#cookiesave")
    expect(dlg).to_be_hidden()
    expect(page.locator("#msg")).to_have_text("Cookies added; retrying the download…")
    expect(row.locator(".badge").first).to_have_text("queued")
    assert (settings.tmp_dir / f"cookies-once-{job.id}.txt").read_text() == "# Netscape HTTP Cookie File\n" + COOKIES
    expect(page.locator("#cookiesbtn")).to_be_hidden()           # use-once is not a stored cookie


def test_keep_cookies_show_in_the_top_menu_and_can_be_deleted(page, server, settings):
    expect = _expect()
    check(page)
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(1)   # the POST has landed before the store is read
    job = server.store.list()[0]
    server.store.update(job.id, status=FAILED, error="Private video", needs_login=True)
    page.locator("#jobs tr.job").first.locator("button.addcookies").click()
    page.check("input[name=ckeep][value=keep]")
    page.fill("#cookietext", COOKIES)
    page.click("#cookiesave")
    expect(page.locator("#msg")).to_have_text("Cookies added; retrying the download…")

    btn = page.locator("#cookiesbtn")
    expect(btn).to_have_text("Cookies (1)")
    expect(page.locator("#cookiespanel")).to_be_hidden()
    btn.click()
    panel = page.locator("#cookiespanel")
    expect(panel).to_be_visible()
    expect(panel.locator(".ck .sites").first).to_have_text("facebook.com, instagram.com")
    expect(panel.locator(".ck .meta").first).to_contain_text("2 cookies")
    assert "SECRET" not in page.content()

    panel.locator("button.ckdelete").click()                     # confirm auto-accepted
    expect(btn).to_be_hidden()
    expect(panel).to_be_hidden()
    assert server.store is not None and list((settings.data_dir / "config" / "cookies").glob("*.txt")) == []


def test_probe_login_wall_offers_cookies_and_rechecks_after_saving(page, server, fakes):
    expect = _expect()
    fakes["mode"] = "login"
    page.fill("#url", "https://www.instagram.com/p/abc/")
    page.click("#check")
    expect(page.locator("#msg")).to_contain_text("You need to log in")
    expect(page.locator("#msgaddcookies")).to_be_visible()
    expect(page.locator("#msglearnhow")).to_be_visible()
    page.click("#msgaddcookies")
    expect(page.locator("#cookiedlg")).to_be_visible()
    expect(page.locator("input[name=ckeep][value=once]")).to_be_disabled()   # no job yet
    expect(page.locator("input[name=ckeep][value=keep]")).to_be_checked()
    fakes["mode"] = "single"                                     # with cookies the probe now succeeds
    page.fill("#cookietext", COOKIES)
    page.click("#cookiesave")
    expect(page.locator("#options")).to_be_visible()             # Check re-ran automatically
    expect(page.locator("#cookiesbtn")).to_have_text("Cookies (1)")


def test_clip_entry_sends_seconds_and_the_row_shows_the_range(page, server):
    expect = _expect()
    check(page)
    expect(page.locator("#pduration")).to_have_text("12:34")
    expect(page.locator("#clipbox")).to_be_visible()
    expect(page.locator("#clipstart")).to_have_attribute("placeholder", "0:00")
    assert page.locator("#clipstart").get_attribute("inputmode") is None   # phone keypads have no ':'
    page.fill("#clipstart", "2:30")
    page.fill("#clipend", "1:00")
    page.click("#download")
    expect(page.locator("#msg")).to_contain_text("after the start")
    assert server.store.list() == []
    page.fill("#clipstart", "60")
    page.fill("#clipend", "2:30")
    page.click("#download")
    expect(page.locator("#options")).to_be_hidden()
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".fmt")).to_have_text("MP4 1080p · 1:00–2:30")
    job = server.store.list()[0]
    assert (job.clip_start, job.clip_end) == (60, 150)

    check(page)                                           # a start past the video's end is an error; open end
    page.fill("#clipstart", "1:00:00")
    page.click("#download")
    expect(page.locator("#msg")).to_contain_text("before the end of the video")
    page.fill("#clipstart", "12:00")
    page.check("input[name=fmt][value=mp3]")
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(2)
    expect(page.locator("#jobs tr.job").first.locator(".fmt")).to_have_text("MP3 best · 12:00–end")
    newest = server.store.list()[0]
    assert (newest.clip_start, newest.clip_end) == (720, None)
    server.store.update(newest.id, status=DOWNLOADING, progress=0)
    expect(page.locator("#jobs tr.job").first.locator(".progress-text")).to_have_text("Clipping…")


def test_clip_row_hidden_for_whole_playlist(page, server, fakes):
    expect = _expect()
    fakes["mode"] = "playlist"
    check(page, "https://www.youtube.com/playlist?list=PL1")
    expect(page.locator("#clipbox")).to_be_visible()
    page.check("#plcheck")
    expect(page.locator("#clipbox")).to_be_hidden()
    page.uncheck("#plcheck")
    expect(page.locator("#clipbox")).to_be_visible()


def test_several_links_queue_as_a_batch_without_probing(page, server):
    expect = _expect()
    probes = []
    page.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    page.fill("#url", "https://a.example/1\nhttps://b.example/2 https://a.example/1\n  https://c.example/3")
    page.click("#check")
    expect(page.locator("#options")).to_be_visible()
    expect(page.locator("#ptitle")).to_have_text("3 links")
    assert page.locator("#quality option").all_text_contents() == [
        "Best available", "Up to 2160p", "Up to 1440p", "Up to 1080p", "Up to 720p", "Up to 480p", "Up to 360p"]
    expect(page.locator("#subbox")).to_be_hidden()
    expect(page.locator("#clipbox")).to_be_hidden()
    expect(page.locator("#plbox")).to_be_hidden()
    expect(page.locator("#pthumb")).to_be_hidden()
    page.check("input[name=fmt][value=mp3]")
    expect(page.locator("#quality option")).to_have_text(["Best (VBR)", "320 kbps", "192 kbps"])
    page.check("input[name=fmt][value=mp4]")
    page.select_option("#quality", "720")
    with page.expect_request(lambda r: r.url.endswith("/api/jobs/batch")) as req:
        page.click("#download")
    assert req.value.post_data_json == {"urls": ["https://a.example/1", "https://b.example/2", "https://c.example/3"],
                                        "format": "mp4", "quality": "720"}
    expect(page.locator("#msg")).to_have_text("Queued 3 links")
    expect(page.locator("#jobs tr.job")).to_have_count(3)
    assert probes == []
    page.fill("#url", "https://a.example/1 https://d.example/4")
    page.click("#check")
    page.select_option("#quality", "720")
    page.click("#download")
    expect(page.locator("#msg")).to_have_text("Queued 1 link, 1 already in progress")


def test_enter_submits_and_shift_enter_adds_a_line(page, server):
    expect = _expect()
    box = page.locator("#url")
    assert box.evaluate("el => el.tagName") == "TEXTAREA"
    box.click()
    page.keyboard.type("https://a.example/1")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("https://b.example/2")
    expect(page.locator("#options")).to_be_hidden()      # typing never runs Check
    assert box.input_value() == "https://a.example/1\nhttps://b.example/2"
    page.keyboard.press("Enter")
    expect(page.locator("#ptitle")).to_have_text("2 links")


def test_paste_into_an_empty_box_runs_check(page, server):
    expect = _expect()
    paste = """(el, text) => {
        const dt = new DataTransfer();
        dt.setData('text/plain', text);
        el.focus();
        el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
        el.value = el.value + text;   // synthetic paste events do not insert text themselves
        el.dispatchEvent(new Event('input', {bubbles: true}));
    }"""
    with page.expect_request(lambda r: r.url.endswith("/api/probe")) as probe:
        page.locator("#url").evaluate(paste, "https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert probe.value.post_data_json == {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}
    expect(page.locator("#ptitle")).to_have_text("Me at the zoo")
    probes = []
    page.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    page.locator("#url").evaluate(paste, " https://b.example/2")    # box not empty: no Check
    page.locator("#url").fill("")
    page.locator("#url").evaluate(paste, "just some words")          # no URL: no Check
    page.wait_for_timeout(500)
    assert probes == []


def test_details_expander_shows_the_failure_log(page, server):
    expect = _expect()
    check(page)
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(1)   # the POST has landed before the store is read
    row = page.locator("#jobs tr.job").first
    job = server.store.list()[0]
    server.store.update(job.id, status=FAILED, error="HTTP Error 403",
                        log="[youtube] abc: Downloading\nERROR: HTTP Error 403")
    details = row.locator("button.details")
    expect(details).to_have_text("Details")
    expect(row.locator("pre.log")).to_have_count(0)
    details.click()
    expect(row.locator("pre.log")).to_have_text("[youtube] abc: Downloading\nERROR: HTTP Error 403")
    expect(details).to_have_attribute("aria-expanded", "true")
    expect(details).to_be_focused()                       # the redraw hands focus back to the toggle
    page.wait_for_timeout(2500)                           # survives a poll re-render
    expect(row.locator("pre.log")).to_be_visible()
    row.locator("pre.log").evaluate("el => { el.dataset.mark = '1'; getSelection().selectAllChildren(el); }")
    page.wait_for_timeout(2500)                           # a poll while text is selected leaves the table alone
    assert row.locator("pre.log").evaluate("el => el.dataset.mark === '1' && !getSelection().isCollapsed")
    page.evaluate("getSelection().removeAllRanges()")
    details.click()
    expect(row.locator("pre.log")).to_have_count(0)
    details.click()
    expect(row.locator("pre.log")).to_be_visible()
    row.locator("button.retry").click()
    expect(row.locator(".badge")).to_have_text("queued")
    expect(row.locator("pre.log")).to_have_count(0)
    server.store.update(job.id, status=FAILED, error="Again", log="second failure")
    row.locator("button.details").click()
    expect(row.locator("pre.log")).to_have_text("second failure")   # re-fetched, not the cached text


def test_next_button_marks_the_row_with_a_badge(page, server):
    expect = _expect()
    check(page)
    page.click("#download")
    row = page.locator("#jobs tr.job").first
    expect(row.locator("button.next")).to_have_text("Next")
    expect(row.locator(".badge.next")).to_have_count(0)
    row.locator("button.next").click()
    expect(row.locator(".badge.next")).to_have_text("next")
    assert server.store.list()[0].priority is True
    row.locator("button.pause").click()
    expect(row.locator(".badge.next")).to_have_count(0)
    expect(row.locator("button.next")).to_have_count(0)


def test_low_disk_banner_free_space_and_507_message(page, server, monkeypatch):
    from app import worker as worker_mod
    expect = _expect()
    expect(page.locator("#diskfree")).to_have_text(re.compile(r"^Free: [\d.]+ [KMGT]?B$"))
    expect(page.locator("#diskbanner")).to_be_hidden()
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: True)
    banner = page.locator("#diskbanner")
    expect(banner).to_have_text(re.compile(
        r"^Low disk space — [\d.]+ [KMGT]?B free; downloads wait until 1\.0 GB is free\.$"))
    check(page)
    page.click("#download")
    expect(page.locator("#msg")).to_have_text(re.compile(
        r"^Only [\d.]+ [KMGT]?B free on the download disk; downloads need at least 1\.0 GB$"))
    expect(page.locator("#options")).to_be_visible()
    assert server.store.list() == []
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: False)
    expect(banner).to_be_hidden()


def test_batch_download_shows_the_507_detail_when_disk_is_low(page, server, monkeypatch):
    from app import worker as worker_mod
    expect = _expect()
    monkeypatch.setattr(worker_mod, "disk_low", lambda settings: True)
    page.fill("#url", "https://a.example/1 https://b.example/2")
    page.click("#check")
    expect(page.locator("#ptitle")).to_have_text("2 links")
    page.click("#download")
    expect(page.locator("#msg")).to_have_text(re.compile(
        r"^Only [\d.]+ [KMGT]?B free on the download disk; downloads need at least 1\.0 GB$"))
    expect(page.locator("#options")).to_be_visible()
    assert server.store.list() == []


def test_footer_shows_the_proxy_host_without_credentials(browser, server, settings):
    expect = _expect()
    settings.proxy = "socks5://user:pw@proxy.lan:1080"   # the live Settings object the app serves
    pg = browser.new_page()
    try:
        pg.goto(server.base + "/")
        expect(pg.locator("#proxyinfo")).to_have_text("via proxy proxy.lan:1080")
        assert "pw" not in pg.locator("footer").inner_text()
    finally:
        pg.close()


def test_thumbnails_render_in_step_two_and_on_the_row(page, server):
    expect = _expect()
    check(page)
    thumb = page.locator("#pthumb")
    expect(thumb).to_be_visible()
    page.wait_for_function("document.querySelector('#pthumb').complete")
    assert thumb.evaluate("el => el.naturalWidth") == 1
    page.click("#download")
    img = page.locator("#jobs tr.job img.rthumb")
    expect(img).to_have_count(1)
    assert server.store.list()[0].thumb
    page.wait_for_function("(() => { const i = document.querySelector('img.rthumb');"
                           " return i && i.complete && i.naturalWidth > 0; })()")
    assert img.evaluate("el => el.naturalWidth") == 1
    assert img.get_attribute("alt") == ""
    assert img.get_attribute("loading") == "lazy"


def test_last_choices_are_remembered_after_a_reload(page, server):
    expect = _expect()
    check(page)
    page.select_option("#quality", "720")
    page.select_option("#subs", "en")
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    assert json.loads(page.evaluate("localStorage.getItem('sns.prefs')")) == {
        "format": "mp4", "quality": "720", "subtitles": "en",
        "sponsorblock": False, "compat": False, "subtitles_mode": "embed"}
    page.reload()
    check(page, "https://www.youtube.com/watch?v=other")
    expect(page.locator("#quality")).to_have_value("720")
    expect(page.locator("#subs")).to_have_value("en")
    page.evaluate("localStorage.setItem('sns.prefs', JSON.stringify({format: 'mp4', quality: '1440', subtitles: ''}))")
    check(page, "https://www.youtube.com/watch?v=third")
    expect(page.locator("#quality")).to_have_value("1080")   # nearest lower height on offer
    expect(page.locator("#subs")).to_have_value("")
    page.evaluate("localStorage.setItem('sns.prefs', JSON.stringify({format: 'mp3', quality: '320', subtitles: 'en'}))")
    page.fill("#url", "https://a.example/1 https://b.example/2")
    page.click("#check")
    expect(page.locator("input[name=fmt][value=mp3]")).to_be_checked()
    expect(page.locator("#quality")).to_have_value("320")


# ---- round 7 ----

def _done_job(server, settings, title="Me at the zoo", filename="zoo.mp4", url="https://www.youtube.com/watch?v=jNQXAC9IVRw"):
    # One store write: the page's event stream snapshots the store on every save, and a job
    # seeded as queued then updated to done can be caught in between (its row would show
    # Cancel instead of Remove for up to the coalesce window).
    (settings.downloads_dir / filename).write_bytes(b"z")
    return server.store.add_many([{"url": url, "title": title, "status": DONE, "progress": 100,
                                   "filename": filename, "size": 1}])[0]


def _done_images_job(server, settings, n, title="Four photos of a cat",
                     url="https://www.instagram.com/p/Cat123/", ext="jpg"):
    """A finished photo post of n files seeded with one store write (see _done_job)."""
    names = [f"cat-{i:02d}.{ext}" for i in range(1, n + 1)]
    for name in names:
        (settings.downloads_dir / name).write_bytes(b"i")
    return server.store.add_many([{"url": url, "title": title, "status": DONE, "progress": 100,
                                   "format": "images", "quality": "best", "filename": names[0],
                                   "sidecars": names[1:], "size": n}])[0]


def _api_jobs(server):
    import urllib.request
    with urllib.request.urlopen(server.base + "/api/jobs", timeout=5) as r:
        return json.load(r)


def test_exact_cut_and_chapter_preset_fill_the_clip(page, server, fakes):
    expect = _expect()
    check(page)
    chapter = page.locator("#clipchapter")
    expect(chapter).to_be_visible()
    expect(chapter.locator("option")).to_have_text(["Chapter…", "Intro (0:00–1:05)", "Main part (1:05–6:40)"])
    expect(page.locator("#clipexact")).not_to_be_checked()
    hint = page.locator("#exacthint")
    expect(hint).to_be_hidden()
    page.check("#clipexact")                              # no start or end: say it does nothing yet
    expect(hint).to_have_text("Exact cut applies once you set a start or end")
    page.fill("#clipstart", "1:05.5")                     # fractional seconds parse
    expect(hint).to_be_hidden()
    page.fill("#clipstart", "")
    page.uncheck("#clipexact")
    expect(hint).to_be_hidden()
    page.select_option("#clipchapter", index=2)
    expect(page.locator("#clipstart")).to_have_value("1:05.5")   # fraction kept
    expect(page.locator("#clipend")).to_have_value("6:40")
    page.fill("#clipend", "6:00")                         # editing a box resets the preset
    expect(chapter).to_have_value("")
    page.select_option("#clipchapter", index=2)
    expect(page.locator("#clipend")).to_have_value("6:40")
    page.check("#clipexact")
    with page.expect_request(lambda r: r.url.endswith("/api/jobs") and r.method == "POST") as req:
        page.click("#download")
    body = req.value.post_data_json
    assert (body["clip_start"], body["clip_end"], body["clip_exact"]) == (65.5, 400, True)
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".fmt")).to_have_text("MP4 1080p · 1:05–6:40 exact")
    assert server.store.list()[0].clip_exact is True

    check(page, "https://www.youtube.com/watch?v=other")  # unchecked: no clip_exact in the payload
    expect(page.locator("#clipexact")).not_to_be_checked()
    expect(chapter).to_have_value("")
    page.select_option("#clipchapter", index=1)
    expect(page.locator("#clipstart")).to_have_value("0:00")
    expect(page.locator("#clipend")).to_have_value("1:05.5")
    with page.expect_request(lambda r: r.url.endswith("/api/jobs") and r.method == "POST") as req:
        page.click("#download")
    assert "clip_exact" not in req.value.post_data_json
    assert req.value.post_data_json["clip_end"] == 65.5
    expect(page.locator("#jobs tr.job").first.locator(".fmt")).to_have_text("MP4 1080p · 0:00–1:05")

    fakes["mode"] = "nochapters"
    check(page, "https://www.youtube.com/watch?v=third")
    expect(page.locator("#clipbox")).to_be_visible()
    expect(chapter).to_be_hidden()


def test_retry_row_counts_down_to_the_automatic_retry(page, server):
    from datetime import datetime, timedelta, timezone
    expect = _expect()
    job = server.store.add("https://www.youtube.com/watch?v=jNQXAC9IVRw", title="Me at the zoo")
    at = (datetime.now(timezone.utc) + timedelta(seconds=25)).isoformat()
    server.store.update(job.id, retry_at=at, error="retrying: HTTP Error 503")
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".progress-text")).to_have_text(re.compile(r"^Retrying in (1\d|2[0-5]) s · HTTP Error 503$"))
    first = int(re.search(r"\d+", row.locator(".progress-text").inner_text()).group())
    page.wait_for_timeout(2500)                           # recomputed on the next poll
    later = int(re.search(r"\d+", row.locator(".progress-text").inner_text()).group())
    assert later < first
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    server.store.update(job.id, retry_at=past)
    expect(row.locator(".progress-text")).to_have_text("Retrying in 0 s · HTTP Error 503")


def test_filter_hides_rows_and_keeps_matching_playlist_headers(page, server, fakes):
    expect = _expect()
    fakes["mode"] = "playlist"
    check(page, "https://www.youtube.com/playlist?list=PL1")
    page.check("#plcheck")
    page.click("#download")
    expect(page.locator("#jobs tr.job")).to_have_count(3)
    server.store.add("https://vimeo.com/123", title="Me at the zoo")
    expect(page.locator("#jobs tr.job")).to_have_count(4)
    box = page.locator("#filter")
    expect(box).to_have_attribute("type", "search")
    expect(box).to_have_attribute("placeholder", "Filter downloads")
    assert box.get_attribute("aria-label")
    box.fill("zoo")
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    expect(page.locator("#jobs tr.pl")).to_have_count(0)
    box.fill("VIDEO 2")
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    expect(page.locator("#jobs tr.pl")).to_have_count(1)
    expect(page.locator("#jobs tr.job .title")).to_have_text("Video 2")
    box.fill("vimeo.com")                                 # site
    expect(page.locator("#jobs tr.job .title")).to_have_text("Me at the zoo")
    box.fill("1080p")                                     # format label
    expect(page.locator("#jobs tr.job")).to_have_count(3)
    box.fill("mp4 best")
    expect(page.locator("#jobs tr.job .title")).to_have_text("Me at the zoo")
    box.fill("no such thing")
    expect(page.locator("#jobs tr.job")).to_have_count(0)
    expect(page.locator("#jobs td.empty strong")).to_have_text("No matches")
    page.wait_for_timeout(2500)                           # a poll keeps the filter applied
    expect(page.locator("#jobs td.empty strong")).to_have_text("No matches")
    box.fill("")
    expect(page.locator("#jobs tr.job")).to_have_count(4)
    expect(page.locator("#jobs tr.pl")).to_have_count(1)


def test_selection_bar_bulk_keep_and_bulk_remove(page, server, settings):
    expect = _expect()
    a = _done_job(server, settings, "Alpha", "a.mp4")
    b = _done_job(server, settings, "Beta", "b.mp4")
    expect(page.locator("#jobs tr.job")).to_have_count(2)
    bar = page.locator("#bulkbar")
    expect(bar).to_be_hidden()
    page.locator("input.sel[aria-label='Select Alpha']").check()
    expect(bar).to_be_visible()
    expect(bar).to_contain_text("1 selected")
    page.wait_for_timeout(2500)                           # selection survives a poll re-render
    expect(page.locator("input.sel[aria-label='Select Alpha']")).to_be_checked()
    page.click("#bulkall")
    expect(bar).to_contain_text("2 selected")
    for label in ["Select all shown", "Keep", "Unkeep", "Remove", "Download all", "Clear selection"]:
        expect(bar.get_by_role("button", name=label, exact=True)).to_be_visible()
    with page.expect_request(lambda r: r.url.endswith("/api/jobs/bulk")) as req:
        page.click("#bulkkeep")
    assert req.value.post_data_json["action"] == "keep"
    assert sorted(req.value.post_data_json["ids"]) == sorted([a.id, b.id])
    expect(page.locator("#jobs .badge.kept")).to_have_count(2)
    assert all(j["keep"] for j in _api_jobs(server))
    page.click("#bulkunkeep")
    expect(page.locator("#jobs .badge.kept")).to_have_count(0)
    with page.expect_download() as dl:
        page.click("#bulkzip")
    assert dl.value.url.startswith(server.base + "/api/zip?ids=")
    page.click("#bulkclear")
    expect(bar).to_be_hidden()
    expect(page.locator("input.sel:checked")).to_have_count(0)

    page.locator("input.sel[aria-label='Select Beta']").check()
    page.click("#bulkall")
    expect(bar).to_contain_text("2 selected")
    page.click("#bulkremove")
    expect(page.locator("#jobs tr.job")).to_have_count(0)     # hidden at once
    expect(bar).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("Removed 2 downloads")
    assert len(server.store.list()) == 2                       # nothing sent yet
    page.wait_for_timeout(7000)
    assert server.store.list() == []


def test_bulk_actions_skip_selected_rows_the_filter_hides(page, server, settings):
    expect = _expect()
    a = _done_job(server, settings, "Alpha", "a.mp4")
    _done_job(server, settings, "Beta", "b.mp4")
    expect(page.locator("#jobs tr.job")).to_have_count(2)
    page.locator("input.sel[aria-label='Select Alpha']").check()
    page.click("#bulkall")
    bar = page.locator("#bulkcount")
    expect(bar).to_have_text("2 selected")
    page.fill("#filter", "alpha")
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    expect(bar).to_have_text("1 selected (1 hidden)")
    with page.expect_request(lambda r: r.url.endswith("/api/jobs/bulk")) as req:
        page.click("#bulkkeep")
    assert req.value.post_data_json["ids"] == [a.id]
    page.fill("#filter", "")
    expect(page.locator("#jobs .badge.kept")).to_have_count(1)
    assert {j["title"]: j["keep"] for j in _api_jobs(server)} == {"Alpha": True, "Beta": False}


def test_second_remove_joins_the_toast_and_undo_restores_both(page, server, settings):
    expect = _expect()
    _done_job(server, settings, "Alpha", "a.mp4")
    _done_job(server, settings, "Beta", "b.mp4")
    rows = page.locator("#jobs tr.job")
    expect(rows).to_have_count(2)
    toast = page.locator("#toast")
    assert (toast.get_attribute("role"), toast.get_attribute("aria-live")) == ("status", "polite")
    rows.filter(has_text="Alpha").locator("button.remove").click()
    expect(toast).to_contain_text("Removed Alpha")
    expect(page.locator("#toastundo")).not_to_be_focused()     # a mouse click leaves focus alone
    rows.filter(has_text="Beta").locator("button.remove").focus()
    page.keyboard.press("Enter")                               # by keyboard: focus moves to Undo
    expect(rows).to_have_count(0)
    expect(toast).to_contain_text("Removed 2 downloads")
    expect(page.locator("#toastundo")).to_be_focused()
    page.wait_for_timeout(7000)                                # held while Undo has focus
    assert len(server.store.list()) == 2
    page.keyboard.press("Enter")
    expect(rows).to_have_count(2)
    expect(toast).to_be_hidden()
    page.wait_for_timeout(6000)
    assert len(server.store.list()) == 2


def test_remove_waits_for_the_undo_window(page, server, settings):
    expect = _expect()
    _done_job(server, settings)
    row = page.locator("#jobs tr.job")
    expect(row).to_have_count(1)
    row.locator("button.remove").click()
    expect(row).to_have_count(0)
    toast = page.locator("#toast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("Removed Me at the zoo")
    page.wait_for_timeout(3000)
    assert len(server.store.list()) == 1                       # still there inside the window
    expect(row).to_have_count(0)                               # and still hidden across polls
    page.wait_for_timeout(4000)
    assert server.store.list() == []
    expect(toast).to_be_hidden()


def test_undo_keeps_the_download(page, server, settings):
    expect = _expect()
    _done_job(server, settings)
    row = page.locator("#jobs tr.job")
    expect(row).to_have_count(1)
    row.locator("button.remove").click()
    expect(row).to_have_count(0)
    page.click("#toastundo")
    expect(row).to_have_count(1)
    expect(page.locator("#toast")).to_be_hidden()
    page.wait_for_timeout(6000)
    assert len(server.store.list()) == 1
    expect(row).to_have_count(1)


def test_qr_dialog_shows_the_code_and_the_link(page, server, settings):
    expect = _expect()
    job = _done_job(server, settings, filename="zoo clip.mp4")
    btn = page.locator("#jobs tr.job button.qr")
    expect(btn).to_be_visible()
    btn.click()
    dlg = page.locator("#qrdlg")
    expect(dlg).to_be_visible()
    img = dlg.locator("img")
    assert img.get_attribute("alt") == "QR code for the download link"
    assert img.get_attribute("src") == f"/api/jobs/{job.id}/qr.svg"
    page.wait_for_function("(() => { const i = document.querySelector('#qrdlg img');"
                           " return i.complete && i.naturalWidth > 0; })()")
    expect(dlg).to_contain_text(server.base + "/files/zoo%20clip.mp4")
    dlg.get_by_role("button", name="Close").click()
    expect(dlg).to_be_hidden()


def test_footer_pacing_text_only_when_not_default_and_no_lock_without_pin(browser, server, settings):
    expect = _expect()
    pg = browser.new_page()
    try:
        pg.goto(server.base + "/")
        expect(pg.locator("#status")).not_to_be_empty()
        expect(pg.locator("#pacing")).to_be_empty()
        expect(pg.locator("#lock")).to_be_hidden()
        settings.max_per_site, settings.site_gap = 2, 5
        expect(pg.locator("#pacing")).to_have_text("2 per site · 5 s gap")
    finally:
        pg.close()


def test_pin_gate_unlocks_and_lock_reopens(browser, pin_server):
    expect = _expect()
    pin_server.store.add("https://www.youtube.com/watch?v=jNQXAC9IVRw", title="Me at the zoo")
    pg = browser.new_page()
    try:
        pg.goto(pin_server.base + "/")
        dlg = pg.locator("#pindlg")
        expect(dlg).to_be_visible()
        pin = pg.locator("#pin")
        assert (pin.get_attribute("type"), pin.get_attribute("inputmode"), pin.get_attribute("autocomplete")) == (
            "password", "numeric", "one-time-code")
        pg.keyboard.press("Escape")                         # not dismissable
        pg.wait_for_timeout(300)
        expect(dlg).to_be_visible()
        expect(pg.locator("#jobs tr.job")).to_have_count(0)
        pin.fill("0000")
        pg.click("#pinunlock")
        expect(pg.locator("#pinerr")).to_have_text("Wrong PIN")
        expect(dlg).to_be_visible()
        pin.fill("1234")
        pg.click("#pinunlock")
        expect(dlg).to_be_hidden()
        expect(pg.locator("#jobs tr.job .title")).to_have_text("Me at the zoo")
        lock = pg.locator("#lock")
        expect(lock).to_be_visible()
        lock.click()
        expect(dlg).to_be_visible()
        expect(pg.locator("#pin")).to_have_value("")
        expect(pg.locator("#jobs tr.job")).to_have_count(0)        # nothing readable behind the dialog
        expect(pg.locator("#summary")).to_be_empty()
        pg.wait_for_timeout(2500)                                  # and no poll brings it back
        expect(pg.locator("#jobs tr.job")).to_have_count(0)
        pin.fill("1234")
        pg.click("#pinunlock")
        expect(dlg).to_be_hidden()
        expect(pg.locator("#jobs tr.job .title")).to_have_text("Me at the zoo")

        hit = {"n": 0}                                             # a 401 mid-session reopens the dialog

        def once_401(route):
            if hit["n"] == 0 and route.request.method == "GET":
                hit["n"] += 1
                route.fulfill(status=401, content_type="application/json", body='{"detail": "PIN required"}')
            else:
                route.continue_()
        pg.route("**/api/jobs", once_401)
        pg.evaluate("window.__events.dispatchEvent(new Event('error'))")   # the stream drops: the poll takes over
        expect(dlg).to_be_visible()
        expect(pg.locator("#jobs tr.job")).to_have_count(0)
        pin.fill("1234")
        pg.click("#pinunlock")
        expect(dlg).to_be_hidden()
        expect(pg.locator("#jobs tr.job .title")).to_have_text("Me at the zoo")
        lock.click()
        expect(dlg).to_be_visible()
        pg.reload()                                         # the cookie is gone: still locked
        expect(pg.locator("#pindlg")).to_be_visible()
    finally:
        pg.close()


# ---- round 8: certificate footer hints, finish notifications ----

def test_https_page_shows_the_certificate_link(browser, https_server):
    expect = _expect()
    context = browser.new_context(ignore_https_errors=True)
    pg = context.new_page()
    try:
        assert https_server.base.startswith("https://127.0.0.1:")
        pg.goto(https_server.base + "/")
        expect(pg.locator("#certlink")).to_be_visible()
        assert pg.locator("#certlink").get_attribute("href").endswith("/cert.pem")
        expect(pg.locator("#status")).not_to_be_empty()          # status rendered
        expect(pg.locator("#certwarn")).to_be_hidden()           # 127.0.0.1 is in the SAN
    finally:
        context.close()


def test_plain_http_page_hides_the_certificate_link(page, server):
    expect = _expect()
    expect(page.locator("#status")).not_to_be_empty()
    expect(page.locator("#certlink")).to_be_hidden()
    expect(page.locator("#certwarn")).to_be_hidden()


def _notify_context(browser, permission="granted", hidden=False):
    """A fresh context (no service worker controls its first load) whose Notification is a
    recording stub; showNotification is recorded too, so a controlling worker cannot hide a
    notification from the test."""
    context = browser.new_context()
    context.grant_permissions(["notifications"])
    context.add_init_script("""
      window.__notes = window.__notes || [];
      class StubNotification {
        static permission = '%s';
        static requestPermission() { return Promise.resolve(StubNotification.permission); }
        constructor(title, options) { window.__notes.push({title, options}); }
        close() {}
      }
      window.Notification = StubNotification;
      if (window.ServiceWorkerRegistration) ServiceWorkerRegistration.prototype.showNotification =
        function (title, options) { window.__notes.push({title, options}); return Promise.resolve(); };
    """ % permission)
    if hidden:
        context.add_init_script(
            "Object.defineProperty(Document.prototype, 'visibilityState', {get: () => 'hidden', configurable: true});")
    return context


def _transition_to_done(pg, server):
    expect = _expect()
    job = server.store.add("https://www.youtube.com/watch?v=jNQXAC9IVRw", title="Me at the zoo")
    expect(pg.locator("#summary")).to_have_text("1 active · 0 done · 0 failed")
    server.store.update(job.id, status=DONE, progress=100, filename="zoo.mp4", size=1)
    expect(pg.locator("#summary")).to_have_text("0 active · 1 done · 0 failed")
    return job


def _next_polls(pg, n=1):
    """n more list redraws (the fallback poll's body: the event stream only sends changes)."""
    for _ in range(n):
        pg.evaluate("refresh()")


def _turn_bell_on(pg):
    expect = _expect()
    bell = pg.locator("#notify")
    expect(bell).to_be_visible()
    expect(bell).to_have_attribute("aria-pressed", "false")
    bell.click()
    expect(bell).to_have_attribute("aria-pressed", "true")
    expect(pg.locator("#notifytext")).to_have_text("Notifications on")
    assert pg.evaluate("JSON.parse(localStorage.getItem('sns.prefs')).notify") is True


def test_bell_notifies_once_when_a_hidden_page_sees_a_job_finish(browser, server):
    context = _notify_context(browser, hidden=True)
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        _turn_bell_on(pg)
        _transition_to_done(pg, server)
        pg.wait_for_function("window.__notes.length === 1", timeout=T)
        note = pg.evaluate("window.__notes[0]")
        assert note["title"] == "Download finished"
        assert note["options"]["body"] == "Me at the zoo"
        assert note["options"]["icon"] == "/icons/icon-192.png"
        assert note["options"]["tag"].startswith("sns-")
        _next_polls(pg, 2)                                       # more polls: no repeat
        assert pg.evaluate("window.__notes.length") == 1
    finally:
        context.close()


def test_bell_stays_quiet_while_the_page_is_visible(browser, server):
    context = _notify_context(browser)
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        _turn_bell_on(pg)
        _transition_to_done(pg, server)
        _next_polls(pg)
        assert pg.evaluate("window.__notes.length") == 0
    finally:
        context.close()


def test_bell_off_sends_nothing(browser, server):
    context = _notify_context(browser, hidden=True)
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        _expect()(pg.locator("#notify")).to_have_attribute("aria-pressed", "false")
        _transition_to_done(pg, server)
        _next_polls(pg)
        assert pg.evaluate("window.__notes.length") == 0
    finally:
        context.close()


def test_bell_is_disabled_when_notifications_are_blocked(browser, server):
    expect = _expect()
    context = _notify_context(browser, permission="denied")
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        bell = pg.locator("#notify")
        expect(bell).to_be_visible()
        expect(bell).to_be_disabled()
        expect(bell).to_have_attribute("title", "Notifications are blocked in the browser settings")
        expect(bell).to_have_attribute("aria-pressed", "false")
    finally:
        context.close()


def test_bell_explains_that_notifications_need_https(browser, server):
    expect = _expect()
    context = _notify_context(browser, permission="denied")
    context.add_init_script(
        "Object.defineProperty(window, 'isSecureContext', {get: () => false, configurable: true});")
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        bell = pg.locator("#notify")
        expect(bell).to_be_visible()
        expect(bell).to_be_disabled()
        expect(bell).to_have_attribute("title", "Notifications need HTTPS (or localhost)")
        expect(bell).to_have_attribute("aria-pressed", "false")
    finally:
        context.close()


def test_bell_renders_off_when_permission_is_revoked_while_open(browser, server):
    expect = _expect()
    context = _notify_context(browser)
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        _turn_bell_on(pg)
        pg.evaluate("Notification.permission = 'default'")       # revoked in the site settings
        expect(pg.locator("#notify")).to_have_attribute("aria-pressed", "false", timeout=T)
        expect(pg.locator("#notifytext")).to_have_text("Notify when done")
    finally:
        context.close()


# ---- round 9 ----

def _post_body(page, path, action):
    with page.expect_request(lambda r: r.url.endswith(path) and r.method == "POST") as req:
        action()
    return req.value.post_data_json


def test_subscriptions_panel_add_check_now_pause_and_remove(page, server, fakes):
    expect = _expect()
    panel = page.locator("#subscriptions")
    expect(panel.locator("summary")).to_have_text("Subscriptions (0)")
    panel.locator("summary").click()
    page.fill("#suburl", "https://www.youtube.com/@lofi")
    expect(page.locator("#subquality option")).to_have_text(["Best", "1080p", "720p", "480p", "360p"])
    page.select_option("#subformat", "mp3")
    expect(page.locator("#subquality option")).to_have_text(["Best (VBR)", "320 kbps", "192 kbps"])
    page.select_option("#subquality", "320")
    page.fill("#sublimit", "5")
    expect(page.locator("#subexisting")).not_to_be_checked()
    body = _post_body(page, "/api/subscriptions", lambda: page.click("#subform button[type=submit]"))
    assert body == {"url": "https://www.youtube.com/@lofi", "format": "mp3", "quality": "320",
                    "limit": 5, "include_existing": False, "backfill": False}
    row = page.locator("#sublist .subrow")
    expect(row).to_have_count(1)
    expect(row.locator(".subtitle")).to_have_text("Lofi channel")      # title from the first check
    expect(row.locator(".subfmt")).to_have_text("MP3 320 kbps")
    expect(row.locator(".substate")).to_have_text("last checked just now · 0 queued")
    expect(panel.locator("summary")).to_have_text("Subscriptions (1)")
    expect(page.locator("#suburl")).to_have_value("")
    assert server.store.list() == []                                  # first check only seeds

    fakes["sub_round"] = 2                                            # three new uploads
    row.locator("button.subcheck").click()
    expect(row.locator(".substate")).to_have_text("last checked just now · 3 queued")
    expect(page.locator("#jobs tr.job")).to_have_count(3)
    expect(page.locator("#jobs tr.pl .pltitle")).to_have_text("Lofi channel")
    sub = server.app.state.subscriptions.list()[0]
    assert {(j.subscription_id, j.format, j.quality) for j in server.store.list()} == {(sub.id, "mp3", "320")}

    row.locator("button.subtoggle").click()
    expect(row.locator("button.subtoggle")).to_have_text("Resume")
    expect(row.locator(".substate")).to_contain_text("paused")
    assert server.app.state.subscriptions.get(sub.id).enabled is False
    row.locator("button.subtoggle").click()
    expect(row.locator("button.subtoggle")).to_have_text("Pause")

    row.locator("button.subremove").click()                          # confirm auto-accepted
    expect(row).to_have_count(0)
    expect(panel.locator("summary")).to_have_text("Subscriptions (0)")
    assert server.app.state.subscriptions.list() == []
    expect(page.locator("#jobs tr.job")).to_have_count(3)             # its jobs stay


def test_subscription_error_shows_on_the_row(page, server, monkeypatch):
    from app import probe as probe_mod
    from app import subscriptions as subs_mod
    expect = _expect()

    def broken(url, settings):
        raise probe_mod.ProbeError("Unsupported URL: https://example.com/x")
    monkeypatch.setattr(subs_mod, "expander", broken)
    page.locator("#subscriptions summary").click()
    page.fill("#suburl", "https://example.com/x")
    page.click("#subform button[type=submit]")
    row = page.locator("#sublist .subrow")
    expect(row.locator(".subtitle")).to_have_text("https://example.com/x")
    expect(row.locator(".substate")).to_have_text("error: Unsupported URL: https://example.com/x")


NEXT_START = r"next start in (\d+ h( \d+ min)?|\d+ min)"


def test_closed_window_banner_waits_and_download_now(browser, window_server):
    expect = _expect()
    job = window_server.store.add("https://www.youtube.com/watch?v=night", title="Night job")
    pg = browser.new_page()
    try:
        pg.goto(window_server.base + "/")
        banner = pg.locator("#windowbanner")
        expect(banner).to_have_text(re.compile(rf"^Downloads run 01:00–02:00 · {NEXT_START} · queued jobs wait$"))
        expect(pg.locator("footer .windowinfo")).to_have_text(
            re.compile(rf"^Downloads run 01:00–02:00 · {NEXT_START}$"))
        row = pg.locator("#jobs tr.job").first
        expect(row.locator(".progress-text")).to_have_text("Waits for 01:00")
        expect(row.locator("button.now")).to_have_text("Download now")
        expect(row.locator(".badge.now")).to_have_count(0)
        with pg.expect_request(lambda r: r.url.endswith(f"/api/jobs/{job.id}/now") and r.method == "POST") as req:
            row.locator("button.now").click()
        assert req.value.headers.get("x-requested-with") == "sns-downloader"
        expect(row.locator(".badge.now")).to_have_text("now")
        expect(row.locator("button.now")).to_have_count(0)
        expect(banner).to_be_hidden()                                 # nothing left waiting
        assert window_server.store.get(job.id).now is True
    finally:
        pg.close()


def test_sponsorblock_checkbox_youtube_only_disabled_by_a_clip(page, server):
    expect = _expect()
    expect(page.locator("#windowbanner")).to_be_hidden()
    expect(page.locator("footer .windowinfo")).to_have_text("")
    check(page)
    box = page.locator("#sponsorblock")
    expect(box).to_be_visible()
    expect(box).not_to_be_checked()
    box.check()
    page.fill("#clipstart", "1:00")
    expect(box).to_be_disabled()
    expect(box).not_to_be_checked()
    page.fill("#clipstart", "")
    expect(box).to_be_enabled()
    expect(box).to_be_checked()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["sponsorblock"] is True
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".badge.noads")).to_have_text("no ads")
    assert server.store.list()[0].sponsorblock is True
    assert json.loads(page.evaluate("localStorage.getItem('sns.prefs')"))["sponsorblock"] is True

    check(page, "https://vimeo.com/12345")                            # another site: no checkbox
    expect(box).to_be_hidden()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["sponsorblock"] is False
    expect(page.locator("#jobs tr.job")).to_have_count(2)
    expect(page.locator("#jobs tr.job").first.locator(".badge.noads")).to_have_count(0)

    check(page, "https://m.youtube.com/watch?v=again")                # prefs kept through the other site
    expect(box).to_be_checked()
    page.fill("#clipend", "2:00")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["sponsorblock"] is False and body["clip_end"] == 120


def test_status_defaults_apply_until_a_choice_is_remembered(browser, server, settings):
    expect = _expect()
    settings.sponsorblock = True
    settings.prefer_h264 = True
    pg = browser.new_page()
    try:
        pg.goto(server.base + "/")
        expect(pg.locator("#status")).not_to_be_empty()
        check(pg)
        expect(pg.locator("#sponsorblock")).to_be_checked()
        expect(pg.locator("#compat")).to_be_checked()
        pg.uncheck("#sponsorblock")
        pg.uncheck("#compat")
        pg.click("#download")
        expect(pg.locator("#jobs tr.job")).to_have_count(1)
        pg.reload()
        expect(pg.locator("#status")).not_to_be_empty()
        check(pg, "https://www.youtube.com/watch?v=other")
        expect(pg.locator("#sponsorblock")).not_to_be_checked()
        expect(pg.locator("#compat")).not_to_be_checked()
    finally:
        pg.close()


def test_compat_and_subtitle_file_mode_reach_the_job_and_persist(page, server):
    expect = _expect()
    check(page)
    compat = page.locator("#compat")
    expect(compat).to_be_visible()
    expect(compat).not_to_be_checked()
    expect(page.locator("#submode-embed")).to_be_checked()
    page.select_option("#subs", "en")
    page.check("#submode-file")
    compat.check()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert (body["compat"], body["subtitles"], body["subtitles_mode"]) == (True, "en", "file")
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".badge.h264")).to_have_text("h264")
    job = server.store.list()[0]
    assert (job.compat, job.subtitles_mode) == (True, "file")
    prefs = json.loads(page.evaluate("localStorage.getItem('sns.prefs')"))
    assert (prefs["compat"], prefs["subtitles_mode"]) == (True, "file")

    page.reload()
    check(page, "https://www.youtube.com/watch?v=other")
    expect(compat).to_be_checked()
    expect(page.locator("#submode-file")).to_be_checked()
    page.check("input[name=fmt][value=mp3]")
    expect(compat).to_be_hidden()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["compat"] is False and "subtitles" not in body
    expect(page.locator("#jobs tr.job")).to_have_count(2)


def test_done_row_links_each_subtitle_file(page, server, settings):
    expect = _expect()
    job = _done_job(server, settings)
    (settings.downloads_dir / "zoo.en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    server.store.update(job.id, sidecars=["zoo.en.srt"], note="Sponsor segments could not be fetched; downloaded uncut")
    link = page.locator("#jobs tr.job a.srt")
    expect(link).to_have_text(".srt")
    expect(link).to_have_attribute("href", "/files/zoo.en.srt")
    expect(page.locator("#jobs tr.job .note")).to_have_text("Sponsor segments could not be fetched; downloaded uncut")


def test_search_lists_results_and_use_checks_the_pick(page, server, fakes):
    expect = _expect()
    probes = []
    page.on("request", lambda r: probes.append(r) if r.url.endswith("/api/probe") else None)
    page.fill("#url", "lofi beats")
    body = _post_body(page, "/api/search", lambda: page.click("#check"))
    assert body == {"q": "lofi beats"}
    results = page.locator("#search .result")
    expect(results).to_have_count(2)
    expect(results.first.locator(".rtitle")).to_have_text("Lofi beats to study to")
    expect(results.first.locator(".rmeta")).to_have_text("Lofi Girl · 1:00:00")
    expect(results.nth(1).locator(".rmeta")).to_have_text("Chillhop · 12:34")
    expect(page.locator("#options")).to_be_hidden()
    assert probes == [] and fakes["searches"] == ["lofi beats"]
    body = _post_body(page, "/api/probe", lambda: results.nth(1).locator("button.use").click())
    assert body == {"url": "https://www.youtube.com/watch?v=lofi2"}
    expect(page.locator("#url")).to_have_value("https://www.youtube.com/watch?v=lofi2")
    expect(page.locator("#options")).to_be_visible()
    expect(page.locator("#search")).to_be_hidden()

    page.fill("#url", "zoo")                                          # one bare word searches too
    page.click("#check")
    expect(results).to_have_count(2)
    page.click("#search button.clear")
    expect(page.locator("#search")).to_be_hidden()
    page.fill("#url", "example.com")                                  # looks like a host: no search
    page.click("#check")
    expect(page.locator("#msg")).to_have_text("Paste a link that starts with http:// or https://")
    assert fakes["searches"] == ["lofi beats", "zoo"]


# ---- round 9 review fixes ----

def test_check_now_409_shows_in_panel_and_button_stays_disabled_while_in_flight(page, server):
    expect = _expect()
    page.locator("#subscriptions summary").click()
    page.fill("#suburl", "https://www.youtube.com/@lofi")
    page.click("#subform button[type=submit]")
    row = page.locator("#sublist .subrow")
    expect(row).to_have_count(1)
    expect(row.locator("button.subtoggle")).to_have_attribute("data-enabled", "1")
    held = []
    page.route("**/api/subscriptions/*/check", lambda route: held.append(route))
    row.locator("button.subcheck").click()
    expect(row.locator("button.subcheck")).to_be_disabled()
    page.wait_for_timeout(2600)                                       # a poll redraw keeps it disabled
    expect(row.locator("button.subcheck")).to_be_disabled()
    assert len(held) == 1
    held[0].fulfill(status=409, content_type="application/json", body='{"detail": "busy"}')
    expect(page.locator("#subscriptions .submsg")).to_have_text("A check is already running; try again in a moment")
    expect(row.locator("button.subcheck")).to_be_enabled()
    expect(row.locator("button.subcheck")).to_be_focused()

    row.locator("button.subtoggle").click()                           # Pause, chosen by data-enabled
    expect(row.locator("button.subtoggle")).to_have_attribute("data-enabled", "0")
    expect(row.locator("button.subtoggle")).to_have_text("Resume")
    expect(row.locator("button.subtoggle")).to_be_focused()
    sub = server.app.state.subscriptions.list()[0]
    assert server.app.state.subscriptions.get(sub.id).enabled is False


def test_several_pasted_urls_never_search(page, server, fakes):
    expect = _expect()
    searches = []
    page.on("request", lambda r: searches.append(r) if r.url.endswith("/api/search") else None)
    page.fill("#url", "https://www.youtube.com/watch?v=a1\nhttps://www.youtube.com/watch?v=b2 lofi beats")
    page.click("#check")
    expect(page.locator("#options")).to_be_visible()
    expect(page.locator("#ptitle")).to_have_text("2 links")
    expect(page.locator("#search")).to_be_hidden()
    assert searches == [] and fakes["searches"] == []


def test_youtube_subdomains_show_sponsorblock(page, server):
    expect = _expect()
    box = page.locator("#sponsorblock")
    for url in ("https://m.youtube.com/watch?v=mob", "https://music.youtube.com/watch?v=mus",
                "https://WWW.YouTube.com./watch?v=dot", "https://youtu.be/short"):
        check(page, url)
        expect(box).to_be_visible()
    check(page, "https://notyoutube.com/watch?v=x")
    expect(box).to_be_hidden()


def test_search_query_normalised_results_announced_and_use_focuses_download(page, server):
    expect = _expect()
    page.fill("#url", "  lofi \t  beats  ")
    body = _post_body(page, "/api/search", lambda: page.click("#check"))
    assert body == {"q": "lofi beats"}
    head = page.locator("#search .searchhead")
    expect(head).to_have_attribute("aria-live", "polite")
    expect(head.locator(".searchcount")).to_have_text("2 YouTube results")
    use = page.locator("#search .result button.use")
    expect(use.first).to_have_attribute("aria-label", "Use Lofi beats to study to")
    use.first.click()
    expect(page.locator("#options")).to_be_visible()
    expect(page.locator("#download")).to_be_focused()


def test_defaults_apply_when_the_probe_beats_the_first_status(browser, server, settings):
    expect = _expect()
    settings.sponsorblock = True
    settings.prefer_h264 = True
    pg = browser.new_page()
    held = []

    def hold(route):                                        # the event stream (and its status) never arrives
        held.append(route)
    try:
        pg.route("**/api/events", hold)
        pg.goto(server.base + "/")
        check(pg)
        expect(pg.locator("#sponsorblock")).to_be_checked()
        expect(pg.locator("#compat")).to_be_checked()
    finally:
        for r in held:
            r.continue_()
        pg.close()


# ---- round 10 ----

ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_job_list_updates_through_the_event_stream_without_polling(page, server):
    expect = _expect()
    expect(page.locator("#jobs td.empty strong")).to_have_text("No downloads yet")
    page.wait_for_function("window.__events && window.__events.readyState === EventSource.OPEN", timeout=T)
    polls = []
    page.on("request", lambda r: polls.append(r.url) if "/api/jobs" in r.url or "/api/status" in r.url else None)
    server.store.add(ZOO, title="Me at the zoo")
    expect(page.locator("#jobs tr.job .title")).to_have_text("Me at the zoo", timeout=3000)
    page.wait_for_timeout(3000)
    assert polls == []
    assert page.evaluate("window.__events.readyState") == 1


def test_live_probe_hides_clip_controls_and_sends_live(page, server, fakes):
    expect = _expect()
    fakes["mode"] = "live"
    check(page)
    expect(page.locator("#livebadge")).to_have_text("LIVE")
    expect(page.locator("#livebadge")).to_have_class("badge live")
    expect(page.locator("#pduration")).to_be_empty()
    for sel in ("#clipbox", ".sbrow", "#subbox", "#splitbox"):
        expect(page.locator(sel)).to_be_hidden()
    expect(page.locator("#livehint")).to_have_text(
        "Records from now until you press Stop or the stream ends (at most 12 h)")
    expect(page.locator("#filename")).to_be_visible()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["live"] is True
    assert not {"clip_start", "clip_end", "sponsorblock", "subtitles", "split"} & set(body)
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    job = server.store.list()[0]
    assert job.live is True

    fakes["mode"] = "single"                                           # an ordinary video: no badge
    check(page)
    expect(page.locator("#livebadge")).to_be_hidden()
    expect(page.locator("#livehint")).to_be_hidden()
    expect(page.locator("#pduration")).to_have_text("12:34")


def test_live_row_records_and_stop_shows_stopping(page, server):
    expect = _expect()
    job = server.store.add(ZOO, title="Live show", quality="720", live=True)
    server.store.update(job.id, status=DOWNLOADING)
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".badge.live")).to_have_text("live")
    expect(row.locator(".progress-text")).to_have_text("Recording · starting…")
    server.store.update(job.id, duration=3723, size=1234567)
    expect(row.locator(".progress-text")).to_have_text("Recording · 1:02:03 · 1.2 MB")
    expect(row.locator("button.pause")).to_have_count(0)
    expect(row.locator("button.next")).to_have_count(0)
    expect(row.locator("button.remove")).to_have_text("Cancel")
    stop = row.locator("button.stop")
    expect(stop).to_have_text("Stop")
    with page.expect_request(lambda r: r.url.endswith(f"/api/jobs/{job.id}/stop") and r.method == "POST") as req:
        stop.click()
    assert req.value.headers.get("x-requested-with") == "sns-downloader"
    expect(row.locator(".progress-text")).to_have_text("Stopping…")
    expect(row.locator("button.stop")).to_be_disabled()
    assert server.store.get(job.id).stop_requested is True
    server.store.update(job.id, status=DONE, filename="show.mp4", progress=100)
    expect(row.locator(".fmt")).to_have_text("LIVE · 1:02:03 · MP4 720p")
    expect(row.locator("button.stop")).to_have_count(0)


def test_split_checkbox_disables_the_clip_and_sends_split(page, server, fakes):
    expect = _expect()
    check(page)
    box = page.locator("#splitbox")
    expect(box).to_be_visible()
    expect(box).to_have_text("One file per chapter (2 chapters)")
    page.check("#submode-file")
    page.fill("#clipstart", "0:10")
    expect(page.locator("#split")).to_be_disabled()                  # a clip rules splitting out
    page.fill("#clipstart", "")
    expect(page.locator("#split")).to_be_enabled()
    page.check("#split")
    for sel in ("#clipstart", "#clipend", "#clipchapter", "#clipexact", "#submode-file"):
        expect(page.locator(sel)).to_be_disabled()
    expect(page.locator("#submode-embed")).to_be_checked()
    page.select_option("#subs", "en")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["split"] is True
    assert body["subtitles_mode"] == "embed"
    assert "clip_start" not in body and "clip_end" not in body
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    assert server.store.list()[0].split is True
    assert "split" not in page.evaluate("JSON.parse(localStorage.getItem('sns.prefs'))")

    fakes["mode"] = "nochapters"
    check(page)
    expect(box).to_be_hidden()
    fakes["mode"] = "playlist"                                        # queueing the playlist hides it
    check(page)
    expect(box).to_be_visible()
    expect(page.locator("#clipstart")).to_be_enabled()
    page.check("#plcheck")
    expect(box).to_be_hidden()
    expect(page.locator("#filename")).to_be_hidden()


def test_file_name_is_sent_trimmed_and_hidden_for_batches(page, server):
    expect = _expect()
    check(page)
    name = page.locator("#filename")
    expect(name).to_have_attribute("placeholder", "Me at the zoo")
    expect(name).to_have_attribute("maxlength", "150")
    name.fill("  My zoo clip  ")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["filename"] == "My zoo clip"
    expect(page.locator("#jobs tr.job")).to_have_count(1)
    assert "zoo" in server.store.list()[0].custom_name                # made safe by the server
    assert "filename" not in page.evaluate("JSON.parse(localStorage.getItem('sns.prefs'))")
    check(page)
    expect(name).to_have_value("")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert "filename" not in body
    expect(page.locator("#url")).to_have_value("")                   # the download finished its round trip
    page.fill("#url", "https://a.example/1 https://b.example/2")
    page.click("#check")
    expect(page.locator("#options")).to_be_visible()
    expect(name).to_be_hidden()


def _record_history(server, settings, **kw):
    job = _done_job(server, settings, **kw)
    done = server.store.get(job.id)
    server.app.state.history.record([done])
    return done


def test_previous_download_shows_after_check(page, server, settings):
    expect = _expect()
    check(page)
    expect(page.locator("#previous")).to_be_hidden()
    _record_history(server, settings)
    check(page)
    expect(page.locator("#previous")).to_have_text("Downloaded before · just now · MP4 best")
    expect(page.locator("#previous")).to_have_class("muted hint")


def test_history_panel_lists_downloads_again_removes_clears_and_filters(page, server, settings):
    expect = _expect()
    _record_history(server, settings)
    _record_history(server, settings, title="Lofi beats", filename="lofi.mp4",
                    url="https://www.youtube.com/watch?v=lofi1")
    panel = page.locator("#history")
    expect(panel.locator("summary")).to_have_text("History (2)", timeout=8000)   # from the status frame
    expect(page.locator("#historylist")).to_be_hidden()
    panel.locator("summary").click()
    rows = page.locator("#historylist .hrow")
    expect(rows).to_have_count(2)
    expect(rows.first.locator(".htitle")).to_have_text("Lofi beats")
    expect(rows.first.locator(".meta")).to_have_text("just now · MP4 best · 1 B")

    page.fill("#historyfilter", "zoo")
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".htitle")).to_have_text("Me at the zoo")
    page.fill("#historyfilter", "youtube.com")                      # the site matches too
    expect(rows).to_have_count(2)
    page.fill("#historyfilter", "")

    body = _post_body(page, "/api/jobs", lambda: rows.first.locator("button.again").click())
    assert body == {"url": "https://www.youtube.com/watch?v=lofi1", "format": "mp4", "quality": "best",
                    "subtitles_mode": "embed", "compat": False, "sponsorblock": False,
                    "clip_exact": False, "split": False}
    expect(page.locator("#history .histmsg")).to_be_empty()

    with page.expect_request(lambda r: "/api/history/" in r.url and r.method == "DELETE"):
        rows.first.locator("button.hremove").click()
    expect(rows).to_have_count(1)
    expect(panel.locator("summary")).to_have_text("History (1)")
    assert server.app.state.history.count() == 1

    page.click("#historyclear")                                     # confirm auto-accepted
    expect(rows).to_have_count(0)
    expect(page.locator("#historylist")).to_have_text(
        "Nothing here yet. Finished downloads are listed here even after they are cleaned up.")
    expect(panel.locator("summary")).to_have_text("History (0)")
    assert server.app.state.history.count() == 0


def test_auto_keep_checkbox_hidden_without_keep_dir(page, server):
    expect = _expect()
    page.locator("#subscriptions summary").click()
    page.wait_for_function("window.__events && window.__events.readyState === EventSource.OPEN", timeout=T)
    page.wait_for_timeout(300)
    expect(page.locator("#subkeep")).to_be_hidden()
    page.fill("#suburl", "https://www.youtube.com/@lofi")
    body = _post_body(page, "/api/subscriptions", lambda: page.click("#subform button[type=submit]"))
    assert "auto_keep" not in body


def test_auto_keep_checkbox_with_keep_dir_reaches_the_subscription(browser, keep_server):
    expect = _expect()
    pg = browser.new_page()
    try:
        pg.goto(keep_server.base + "/")
        pg.locator("#subscriptions summary").click()
        expect(pg.locator("#subkeep")).to_be_visible()
        pg.fill("#suburl", "https://www.youtube.com/@lofi")
        pg.check("#subkeep")
        body = _post_body(pg, "/api/subscriptions", lambda: pg.click("#subform button[type=submit]"))
        assert body["auto_keep"] is True
        row = pg.locator("#sublist .subrow")
        expect(row.locator(".substate")).to_have_text("last checked just now · 0 queued · kept automatically")
        expect(pg.locator("#subkeep")).not_to_be_checked()
        assert keep_server.app.state.subscriptions.list()[0].auto_keep is True
    finally:
        pg.close()


def test_feed_button_copies_the_absolute_link(browser, server):
    expect = _expect()
    sub = server.app.state.subscriptions.add("https://www.youtube.com/@lofi", "mp3", "best")
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        pg.locator("#subscriptions summary").click()
        feed = pg.locator(f'#sublist button.subfeed[data-id="{sub.id}"]')
        expect(feed).to_have_text("Feed")
        url = feed.get_attribute("data-url")
        assert re.fullmatch(re.escape(server.base) + rf"/feed/{sub.id}/[0-9a-f]{{32}}\.xml", url)
        feed.click()
        expect(pg.locator("#toast")).to_be_visible()
        expect(pg.locator("#toasttext")).to_have_text("Feed link copied")
        expect(pg.locator("#toastundo")).to_be_hidden()
        assert pg.evaluate("navigator.clipboard.readText()") == url
        expect(pg.locator("#toast")).to_be_hidden(timeout=T)
    finally:
        context.close()


def test_feed_button_shows_the_link_without_clipboard_and_uses_public_url(browser, server, settings):
    expect = _expect()
    settings.public_url = "https://media.example"
    sub = server.app.state.subscriptions.add("https://www.youtube.com/@lofi", "mp3", "best")
    context = browser.new_context()
    context.add_init_script(
        "Object.defineProperty(navigator, 'clipboard', {get: () => undefined, configurable: true});")
    pg = context.new_page()
    try:
        pg.goto(server.base + "/")
        expect(pg.locator("#status")).to_contain_text("yt-dlp")        # the status (public_url) is in
        pg.locator("#subscriptions summary").click()
        feed = pg.locator("#sublist button.subfeed")
        url = feed.get_attribute("data-url")
        assert url.startswith(f"https://media.example/feed/{sub.id}/")
        feed.click()
        expect(pg.locator("#sublist .subfeedurl")).to_have_text(url)
        expect(pg.locator("#toast")).to_be_hidden()
    finally:
        context.close()


def test_footer_shows_retention_and_active_recordings(browser, server, settings):
    expect = _expect()
    settings.retention_days = 3
    pool = server.app.state.pool
    pool._recordings["fake"] = None                                 # one recording running
    pg = browser.new_page()
    try:
        pg.goto(server.base + "/")
        expect(pg.locator("#retention")).to_have_text("Files are deleted 3 days after download unless kept.")
        expect(pg.locator("#recinfo")).to_have_text("1 recording")
        settings.retention_days = 1
        pool._recordings.clear()
        pg.reload()
        expect(pg.locator("#retention")).to_have_text("Files are deleted 1 day after download unless kept.")
        expect(pg.locator("#recinfo")).to_be_empty()
        settings.retention_days = None
        pg.reload()
        expect(pg.locator("#retention")).to_have_text("Files are deleted automatically every night unless kept.")
    finally:
        pool._recordings.clear()
        pg.close()


def test_kept_file_in_a_media_folder_links_under_its_folder(page, server, settings):
    expect = _expect()
    job = _done_job(server, settings, filename="zoo #1.mp4")
    server.store.update(job.id, keep=True, subdir="Zoo Channel", sidecars=["zoo #1.en.srt"])
    row = page.locator("#jobs tr.job").first
    expect(row.locator("a.btn").first).to_have_attribute("href", "/files/Zoo%20Channel/zoo%20%231.mp4")
    expect(row.locator("a.srt")).to_have_attribute("href", "/files/Zoo%20Channel/zoo%20%231.en.srt")
    row.locator("button.qr").click()
    expect(page.locator("#qrurl")).to_have_text(server.base + "/files/Zoo%20Channel/zoo%20%231.mp4")


# ---- round 10 fix wave ----

def _unlock(pg):
    pg.locator("#pin").fill("1234")
    pg.click("#pinunlock")
    _expect()(pg.locator("#pindlg")).to_be_hidden()


def test_lock_hides_history_and_subscriptions_and_unlock_reloads_them(browser, pin_server, settings):
    expect = _expect()
    _record_history(pin_server, settings)
    pin_server.app.state.subscriptions.add("https://www.youtube.com/@lofi", "mp3", "best")
    pg = browser.new_page()
    try:
        pg.goto(pin_server.base + "/")
        _unlock(pg)
        pg.locator("#history summary").click()
        pg.locator("#subscriptions summary").click()
        expect(pg.locator("#historylist .hrow")).to_have_count(1)
        expect(pg.locator("#sublist .subrow")).to_have_count(1)
        pg.click("#lock")
        expect(pg.locator("#pindlg")).to_be_visible()
        assert pg.locator("#historylist").inner_html() == ""
        assert pg.locator("#sublist").inner_html() == ""
        pg.wait_for_timeout(500)
        assert pg.locator("#historylist").inner_html() == ""
        _unlock(pg)
        expect(pg.locator("#historylist .hrow .htitle")).to_have_text("Me at the zoo")
        expect(pg.locator("#sublist .subrow")).to_have_count(1)
    finally:
        pg.close()


def test_lock_while_a_panel_response_is_being_read_renders_nothing(browser, pin_server, settings):
    # The window is between the response headers (fetch resolves; `locked` is still false) and
    # the body (`await r.json()`): a delayed route would only delay the headers, so the body read
    # itself is slowed down to make the race deterministic.
    expect = _expect()
    _record_history(pin_server, settings)
    pin_server.app.state.subscriptions.add("https://www.youtube.com/@lofi", "mp3", "best")
    pg = browser.new_page()
    try:
        pg.goto(pin_server.base + "/")
        _unlock(pg)
        pg.evaluate("""() => {
            window.__slowReads = 0;
            const json = Response.prototype.json;
            Response.prototype.json = function () {
                if (!/\\/api\\/(history|subscriptions)/.test(this.url)) return json.call(this);
                window.__slowReads++;
                return new Promise(ok => setTimeout(ok, 700)).then(() => json.call(this));
            };
        }""")
        pg.locator("#history summary").click()
        pg.locator("#subscriptions summary").click()
        pg.wait_for_function("window.__slowReads >= 2", timeout=T)   # both bodies being read
        pg.click("#lock")
        expect(pg.locator("#pindlg")).to_be_visible()
        pg.wait_for_timeout(1200)                                     # the reads finish while locked
        assert pg.locator("#historylist").inner_html() == ""
        assert pg.locator("#sublist").inner_html() == ""
    finally:
        pg.close()


def test_stream_error_with_a_lapsed_pin_opens_the_dialog(browser, pin_server):
    expect = _expect()
    pg = browser.new_page()
    try:
        pg.goto(pin_server.base + "/")
        _unlock(pg)
        pg.wait_for_function("window.__events && window.__events.readyState === EventSource.OPEN", timeout=T)

        def auth(route):
            if route.request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body='{"required": true, "ok": false}')
            else:
                route.continue_()
        pg.route("**/api/auth", auth)                       # /api/jobs still answers: only the auth check locks
        pg.evaluate("window.__events.dispatchEvent(new Event('error'))")
        expect(pg.locator("#pindlg")).to_be_visible()
        expect(pg.locator("#jobs tr.job")).to_have_count(0)
    finally:
        pg.close()


def test_closed_stream_reconnects(page, server):
    page.wait_for_function("window.__events && window.__events.readyState === EventSource.OPEN", timeout=T)
    page.evaluate("window.__reconnectDelay = 200")
    with page.expect_request(lambda r: r.url.endswith("/api/events"), timeout=T):
        page.evaluate("window.__events.close(); window.__events.dispatchEvent(new Event('error'))")
    page.wait_for_function("window.__events && window.__events.readyState === EventSource.OPEN", timeout=T)


def test_split_keeps_the_saved_subtitle_mode(page, server):
    expect = _expect()
    page.evaluate("localStorage.setItem('sns.prefs', JSON.stringify("
                  "{format: 'mp4', quality: '1080', subtitles: 'en', subtitles_mode: 'file'}))")
    page.reload()
    check(page)
    expect(page.locator("#submode-file")).to_be_checked()        # the saved preference
    page.check("#split")
    expect(page.locator("#submode-embed")).to_be_checked()
    page.uncheck("#split")                                       # unticking gives the choice back
    expect(page.locator("#submode-file")).to_be_checked()
    expect(page.locator("#submode-file")).to_be_enabled()
    page.check("#split")
    page.select_option("#subs", "en")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert (body["split"], body["subtitles_mode"]) == (True, "embed")
    expect(page.locator("#url")).to_have_value("")
    page.reload()
    check(page, "https://www.youtube.com/watch?v=other")
    expect(page.locator("#submode-file")).to_be_checked()


def test_url_box_placeholder_mentions_photo_posts(page, server):
    _expect()(page.locator("#url")).to_have_attribute(
        "placeholder", "TikTok, YouTube, Instagram or Facebook video, playlist or photo post URL, "
                       "or words to search YouTube")


def test_images_probe_shows_the_count_and_only_the_file_name(page, server, fakes):
    expect = _expect()
    check(page)                                          # a video first: its controls show
    expect(page.locator("#pcount")).to_be_hidden()
    expect(page.locator("#pduration")).to_have_text("12:34")
    fakes["mode"] = "images"
    check(page, "https://www.instagram.com/p/Cat123/")
    expect(page.locator("#ptitle")).to_have_text("Four photos of a cat")
    expect(page.locator("#pcount")).to_have_text("3 photos · 1 video")
    expect(page.locator("#pduration")).to_be_hidden()
    expect(page.locator("#pthumb")).to_be_hidden()      # no thumbnail key
    fmt = page.locator("fieldset.seg", has=page.locator("input[name=fmt]"))
    for el in (fmt, page.locator("#quality"), page.locator(".compatbox"), page.locator("#subbox"),
               page.locator(".sbrow"), page.locator("#clipbox"), page.locator("#splitbox"), page.locator("#plbox")):
        expect(el).to_be_hidden()
    name = page.locator("#filename")
    expect(name).to_be_visible()
    expect(name).to_have_attribute("placeholder", "Four photos of a cat")
    name.fill("  Cat post ")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body == {"url": "https://www.instagram.com/p/Cat123/", "format": "images", "quality": "best",
                    "filename": "Cat post"}
    expect(page.locator("#url")).to_have_value("")
    expect(page.locator("#pcount")).to_be_hidden()      # hideOptions resets it
    fakes["mode"] = "single"                             # a video afterwards gets every control back
    check(page)
    expect(page.locator("#pcount")).to_be_hidden()
    expect(fmt).to_be_visible()
    expect(page.locator("#quality")).to_be_visible()
    expect(page.locator("#clipbox")).to_be_visible()


def test_done_photo_post_row_offers_the_zip_and_no_subtitle_links(page, server, settings):
    expect = _expect()
    job = _done_images_job(server, settings, 3)
    row = page.locator("#jobs tr.job")
    expect(row).to_have_count(1)
    expect(row.locator(".badge.photos")).to_have_text("photos")
    expect(row.locator(".fmt")).to_have_text("3 photos")
    link = row.locator("a.btn", has_text="Download zip")
    expect(link).to_have_attribute("href", f"/api/zip?ids={job.id}")
    expect(row.locator("a.srt")).to_have_count(0)
    expect(row.locator("button.qr")).to_have_count(1)
    page.fill("#filter", "photos")
    expect(row).to_be_visible()


def test_single_file_photo_post_links_to_the_file(page, server, settings):
    expect = _expect()
    _done_images_job(server, settings, 1)
    row = page.locator("#jobs tr.job")
    expect(row.locator(".fmt")).to_have_text("1 photo")
    link = row.locator("a.btn", has_text="Download")
    expect(link).to_have_attribute("href", "/files/cat-01.jpg")
    expect(row.locator("a.btn", has_text="Download zip")).to_have_count(0)


def test_subscription_backfill_is_sent_and_the_row_shows_the_catch_up(page, server, fakes):
    expect = _expect()
    page.locator("#subscriptions summary").click()
    page.fill("#suburl", "https://www.youtube.com/@lofi")
    page.check("#subbackfill")
    body = _post_body(page, "/api/subscriptions", lambda: page.click("#subform button[type=submit]"))
    assert body["backfill"] is True
    expect(page.locator("#sublist .subrow")).to_have_count(1)
    expect(page.locator("#subbackfill")).not_to_be_checked()
    store = server.app.state.subscriptions
    sub = store.add("https://www.youtube.com/@cats", "mp4", "best", backfill=True)
    store.update(sub.id, last_remaining=37, last_checked=store.get(sub.id).added_at)
    page.reload()
    page.locator("#subscriptions summary").click()
    row = page.locator(f'#sublist .subrow[data-id="{sub.id}"]')
    expect(row.locator(".substate")).to_contain_text("37 more to catch up")
    expect(row.locator(".meta")).to_contain_text("catching up")


def test_cookie_dialog_mentions_photo_posts(page, server):
    assert "Instagram and X photo posts need your login cookies too." in page.locator("#cookiedlg").inner_text()


# ---- round 12 ----

def test_play_opens_the_inline_video_player_and_escape_stops_it(page, server, settings):
    expect = _expect()
    job = _done_job(server, settings)
    row = page.locator("#jobs tr.job")
    play = row.locator("button.play")
    expect(play).to_have_text("Play")
    # Play sits before the Download link.
    assert row.locator("td.actions > *").first.evaluate("el => el.classList.contains('play')")
    play.click()
    dlg = page.locator("#player")
    expect(dlg).to_be_visible()
    expect(page.locator("#player-title")).to_have_text("Me at the zoo")
    video = dlg.locator("video")
    expect(video).to_have_count(1)
    assert video.get_attribute("src") == "/files/zoo.mp4?inline=1"
    for attr in ("controls", "autoplay", "playsinline"):
        assert video.get_attribute(attr) is not None, attr
    expect(page.locator("#playerclose")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    expect(dlg.locator("video, audio, img")).to_have_count(0)
    expect(page.locator(f'button.play[data-id="{job.id}"]')).to_be_focused()
    play.click()                                          # reused, and the Close button works too
    expect(dlg.locator("video")).to_have_count(1)
    page.click("#playerclose")
    expect(dlg).to_be_hidden()
    expect(dlg.locator("video")).to_have_count(0)


def test_play_on_a_photo_post_shows_every_image(page, server, settings):
    expect = _expect()
    _done_images_job(server, settings, 3)
    page.locator("#jobs tr.job button.play").click()
    imgs = page.locator("#player .media.gallery img")
    expect(imgs).to_have_count(3)
    assert [imgs.nth(i).get_attribute("src") for i in range(3)] == [
        f"/files/cat-0{i}.jpg?inline=1" for i in (1, 2, 3)]
    assert imgs.first.get_attribute("loading") == "lazy"
    assert imgs.first.get_attribute("alt") == ""
    assert page.locator("#player .media").evaluate("el => getComputedStyle(el).overflowY") == "auto"
    page.keyboard.press("Escape")
    expect(page.locator("#player img")).to_have_count(0)


def test_audio_row_plays_with_an_audio_element_and_other_files_get_no_play(page, server, settings):
    expect = _expect()
    (settings.downloads_dir / "song.mp3").write_bytes(b"a")
    (settings.downloads_dir / "doc.mkv").write_bytes(b"m")
    server.store.add_many([
        {"url": "https://www.youtube.com/watch?v=a1", "title": "A song", "status": DONE, "progress": 100,
         "format": "mp3", "quality": "best", "filename": "song.mp3", "size": 1},
        {"url": "https://www.youtube.com/watch?v=a2", "title": "A film", "status": DONE, "progress": 100,
         "filename": "doc.mkv", "size": 1}])
    expect(page.locator("#jobs tr.job")).to_have_count(2)
    expect(page.locator("#jobs tr.job", has_text="A film").locator("button.play")).to_have_count(0)
    page.locator("#jobs tr.job", has_text="A song").locator("button.play").click()
    expect(page.locator("#player audio")).to_have_count(1)
    expect(page.locator("#player video")).to_have_count(0)
    assert page.locator("#player audio").get_attribute("src") == "/files/song.mp3?inline=1"


def test_more_languages_adds_tracks_of_the_same_kind(page, server):
    expect = _expect()
    check(page)
    more = page.locator("button.morelangs")
    expect(more).to_be_hidden()                           # no primary yet
    page.select_option("#subs", "en")
    expect(more).to_be_visible()
    expect(more).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#sublangs")).to_be_hidden()
    more.click()
    expect(more).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#sublangs label")).to_have_text(["German"])
    page.check("#sublangs input[value=de]")
    page.select_option("#subs", "auto:en-orig")           # the other kind: cleared, auto tracks only
    expect(page.locator("#sublangs label")).to_have_text(["Japanese"])
    expect(page.locator("#sublangs input:checked")).to_have_count(0)
    page.select_option("#subs", "en")
    expect(page.locator("#sublangs label")).to_have_text(["German"])
    page.check("#sublangs input[value=de]")
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["subtitles"] == "en,de"
    row = page.locator("#jobs tr.job").first
    expect(row.locator(".fmt")).to_have_text("MP4 1080p · EN+DE subs")
    assert json.loads(page.evaluate("localStorage.getItem('sns.prefs')"))["subtitles"] == "en,de"
    # The next probe pre-ticks what is still offered and drops the rest.
    page.evaluate("localStorage.setItem('sns.prefs', JSON.stringify({format: 'mp4', quality: '1080',"
                  " subtitles: 'auto:en-orig,auto:fr,auto:ja'}))")
    check(page, "https://www.youtube.com/watch?v=other")
    expect(page.locator("#subs")).to_have_value("auto:en-orig")
    expect(page.locator("#sublangs")).to_be_visible()
    expect(page.locator("#sublangs input:checked")).to_have_count(1)
    expect(page.locator("#sublangs input[value='auto:ja']")).to_be_checked()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["subtitles"] == "auto:en-orig,auto:ja"
    expect(page.locator("#jobs tr.job").first.locator(".fmt")).to_have_text("MP4 1080p · EN+JA auto subs")


def test_more_languages_collapsed_clears_the_extra_ticks(page, server):
    expect = _expect()
    check(page)
    page.select_option("#subs", "en")
    more = page.locator("button.morelangs")
    more.click()
    page.check("#sublangs input[value=de]")
    more.click()                                          # collapse: the hidden ticks go
    expect(page.locator("#sublangs")).to_be_hidden()
    expect(more).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#sublangs input:checked")).to_have_count(0)
    more.click()                                          # reopened: still unticked
    expect(page.locator("#sublangs input[value=de]")).not_to_be_checked()
    page.check("#sublangs input[value=de]")
    more.click()
    body = _post_body(page, "/api/jobs", lambda: page.click("#download"))
    assert body["subtitles"] == "en"                      # no hidden language in the payload


def test_telegram_test_button_is_hidden_without_telegram(page, server):
    page.wait_for_function("document.querySelector('#status').textContent !== ''")
    _expect()(page.locator("#telegramtest")).to_be_hidden()


def test_telegram_test_button_posts_and_shows_the_result(browser, telegram_server):
    expect = _expect()
    pg = browser.new_page()
    try:
        calls = []

        def answer(route):
            calls.append(route.request.headers.get("x-requested-with"))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": calls[-1] is not None}))
        pg.route("**/api/telegram/test", answer)
        pg.goto(telegram_server.base + "/")
        btn = pg.locator("#telegramtest")
        expect(btn).to_be_visible()
        expect(btn).to_have_text("Send test message")
        btn.click()
        expect(pg.locator("#msg")).to_have_text("Telegram message sent")
        assert calls == ["sns-downloader"]
        pg.unroute("**/api/telegram/test")
        pg.route("**/api/telegram/test", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok": false}'))
        btn.click()
        expect(pg.locator("#msg")).to_have_text("Telegram failed (see server log)")
    finally:
        pg.close()


def test_backup_link_and_restore_flow(page, server):
    import io
    import zipfile
    expect = _expect()
    link = page.locator("a#backup")
    expect(link).to_have_text("Backup")
    assert link.get_attribute("href") == "/api/backup"
    assert link.get_attribute("download") is not None
    expect(page.locator("#restore")).to_have_text("Restore…")
    expect(page.locator(".backuphint")).to_have_text("The backup contains your login cookies: keep it private.")
    assert page.locator("#restorefile").get_attribute("accept") == ".zip"
    posted = []

    def answer(route):
        posted.append((route.request.headers, route.request.post_data_buffer))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"subscriptions": 3, "history": 120, "cookies": 2}))
    page.route("**/api/restore", answer)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "sns-downloader", "version": 1, "files": []}))
    with page.expect_request(lambda r: "/api/history" in r.url), \
            page.expect_request(lambda r: r.url.endswith("/api/subscriptions")):   # both panels reload
        page.set_input_files("#restorefile", files=[{"name": "sns-backup.zip", "mimeType": "application/zip",
                                                     "buffer": buf.getvalue()}])
    expect(page.locator("#msg")).to_have_text("Restored 3 subscriptions, 120 history entries, 2 cookie files")
    headers, data = posted[0]
    assert headers["x-requested-with"] == "sns-downloader"
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="sns-backup.zip"' in data
    assert buf.getvalue() in data
    page.unroute("**/api/restore")
    page.route("**/api/restore", lambda route: route.fulfill(
        status=422, content_type="application/json", body='{"detail": "not a backup: manifest.json missing"}'))
    page.set_input_files("#restorefile", files=[{"name": "x.zip", "mimeType": "application/zip", "buffer": b"PK"}])
    expect(page.locator("#msg")).to_have_text("not a backup: manifest.json missing")


# ---- round 13: layout, touch targets, sheets, options groups ----

def _box(page, sel):
    return page.locator(sel).first.bounding_box()


def test_wide_window_puts_the_panels_in_a_side_rail(wide_page):
    page = wide_page
    _expect()(page.locator("#subscriptions")).to_be_visible()
    subs, hist, lst = _box(page, "#subscriptions"), _box(page, "#history"), _box(page, "section.card.list")
    hero = _box(page, "section.card.hero")
    assert subs["x"] >= lst["x"] + lst["width"]                      # the rail is right of the list
    assert abs(subs["width"] - 320) <= 13                            # 320 px rail (cards inset by the shadow gutter)
    assert hist["x"] == subs["x"] and hist["y"] > subs["y"]          # cookies, subscriptions, history stack
    assert abs(subs["y"] - hero["y"]) <= 4                           # the rail starts beside the paste card
    assert 0 <= lst["y"] - (hero["y"] + hero["height"]) <= 24        # the list follows the paste card directly
    page.locator("#subscriptions > summary").click()                 # an open panel does not push the list down
    page.locator("#history > summary").click()
    assert abs(_box(page, "section.card.list")["y"] - lst["y"]) <= 1
    page.set_viewport_size({"width": 1024, "height": 800})
    page.wait_for_function("document.querySelector('#subscriptions').getBoundingClientRect().width > 400")
    subs, hero = _box(page, "#subscriptions"), _box(page, "section.card.hero")
    assert subs["y"] >= hero["y"] + hero["height"]                   # one column: below the paste card


def test_wide_window_titles_fill_the_list_column(wide_page, server, settings):
    page = wide_page
    _done_job(server, settings, title="A rather long video title that needs room to show " * 2)
    title = page.locator("#jobs tr.job .title").first
    _expect()(title).to_be_visible()
    box = title.bounding_box()
    line = title.evaluate("t => parseFloat(getComputedStyle(t).lineHeight)")
    assert box["width"] >= 300
    assert box["height"] <= 2 * line + 1
    assert title.evaluate("t => t.scrollWidth > t.clientWidth")      # truncated with an ellipsis, not wrapped


def test_touch_targets_are_44px_and_8px_apart(touch_page, server, settings):
    page = touch_page
    _done_job(server, settings)
    row = page.locator("#jobs tr.job").first
    _expect()(row.locator("button.more")).to_be_visible()          # a phone row: Download + More
    boxes = [b for b in (el.bounding_box() for el in row.locator("td.actions > *").all()) if b]
    assert len(boxes) >= 2
    for el in row.locator("button.small").all():
        assert el.bounding_box()["height"] >= 44
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            dx = max(b["x"] - (a["x"] + a["width"]), a["x"] - (b["x"] + b["width"]))
            dy = max(b["y"] - (a["y"] + a["height"]), a["y"] - (b["y"] + b["height"]))
            assert max(dx, dy) >= 7.5, (a, b)
    sel = row.locator("input.sel").bounding_box()
    assert sel["height"] >= 20                                       # 22 px visual inside a 44 px label area


def test_dialogs_are_bottom_sheets_on_a_phone_and_centred_on_desktop(touch_page, page, server, settings):
    expect = _expect()
    _done_job(server, settings)
    for pg, phone in ((touch_page, True), (page, False)):
        if phone:                                                    # QR lives in the row's action sheet
            pg.locator("#jobs tr.job button.more").click()
            pg.locator("#actionsheet button.qr").click()
        else:
            pg.locator("#jobs tr.job button.qr").click()
        dlg = pg.locator("#qrdlg")
        expect(dlg).to_be_visible()
        pg.wait_for_timeout(300)                                     # the slide-up animation
        assert dlg.evaluate("d => getComputedStyle(d).position") == "fixed"
        box, vp = dlg.bounding_box(), pg.viewport_size
        if phone:
            assert abs(box["y"] + box["height"] - vp["height"]) <= 2
            assert abs(box["width"] - 375) <= 1 and box["x"] == 0
        else:
            assert box["x"] > 0 and abs(box["y"] + box["height"] - vp["height"]) > 2
        pg.locator("#qrclose").click()
        expect(dlg).to_be_hidden()


def test_options_are_grouped_and_empty_groups_hide(page, server, fakes):
    expect = _expect()
    check(page)
    for name in ("output", "subtitles", "clip", "extras"):
        expect(page.locator(f"#group-{name} > legend")).to_be_visible()
    expect(page.locator("#group-output > legend")).to_have_text("Output")
    page.check("input[name=fmt][value=mp3]")                         # no subtitles for audio
    expect(page.locator("#group-subtitles")).to_be_hidden()
    page.check("input[name=fmt][value=mp4]")
    expect(page.locator("#group-subtitles")).to_be_visible()
    fakes["mode"] = "images"
    check(page, "https://www.instagram.com/p/Cat123/")
    for name in ("output", "subtitles", "clip"):
        expect(page.locator(f"#group-{name}")).to_be_hidden()
        assert page.locator(f"#group-{name}").get_attribute("hidden") is not None
        assert page.locator(f"#group-{name}").evaluate("g => getComputedStyle(g).display") == "none"
    expect(page.locator("#group-extras > legend")).to_have_text("Extras")
    expect(page.locator("#filename")).to_be_visible()
    fakes["mode"] = "single"
    check(page)
    expect(page.locator("#group-output")).to_be_visible()


def test_download_row_stays_in_view_while_scrolling_the_options_on_a_phone(touch_page, server):
    page = touch_page
    check(page)
    dl = page.locator("#download")
    assert dl.evaluate("b => getComputedStyle(b.parentElement).position") == "sticky"
    top = page.locator("#options").evaluate("o => o.getBoundingClientRect().top + window.scrollY")
    page.evaluate(f"window.scrollTo(0, {top + 40})")
    page.wait_for_function("document.querySelector('#options').getBoundingClientRect().top < 0")
    box = dl.bounding_box()
    assert 0 <= box["y"] and box["y"] + box["height"] <= page.viewport_size["height"]


def test_header_is_sticky_and_gains_a_border_once_scrolled(touch_page, server):
    page = touch_page
    check(page)
    header = page.locator("header.topbar")
    assert "scrolled" not in (header.get_attribute("class") or "")
    page.evaluate("window.scrollTo(0, 400)")
    _expect()(header).to_have_class(re.compile(r"\bscrolled\b"))
    assert header.bounding_box()["y"] == 0                           # stuck to the top
    page.evaluate("window.scrollTo(0, 0)")
    _expect()(header).not_to_have_class(re.compile(r"\bscrolled\b"))


def test_theme_color_has_a_dark_variant_and_the_more_icon_and_sheet_exist(page):
    metas = page.locator("meta[name=theme-color]")
    assert metas.count() == 2
    assert metas.nth(1).get_attribute("media") == "(prefers-color-scheme: dark)"
    assert page.locator("symbol#i-more").count() == 1
    assert page.locator("dialog#actionsheet").get_attribute("aria-labelledby") == "actionsheet-title"
    assert page.locator("#actionsheet .sheetlist").count() == 1


def test_phone_done_row_shows_download_and_more_and_the_sheet_holds_the_rest(touch_page, server, settings):
    expect = _expect()
    page = touch_page
    for name in ("zoo.mp4", "zoo.en.srt", "zoo.de.srt"):
        (settings.downloads_dir / name).write_bytes(b"z")
    server.store.add_many([{"url": "https://www.youtube.com/watch?v=z", "title": "Me at the zoo", "status": DONE,
                            "progress": 100, "filename": "zoo.mp4", "size": 1,
                            "sidecars": ["zoo.en.srt", "zoo.de.srt"]}])
    row = page.locator("#jobs tr.job").first
    acts = row.locator("td.actions > *")
    expect(acts).to_have_count(2)
    expect(acts.nth(0)).to_have_text("Download")
    expect(acts.nth(0)).to_have_js_property("tagName", "A")
    more = acts.nth(1)
    expect(more).to_have_class(re.compile(r"\bmore\b"))
    expect(more).to_have_attribute("aria-haspopup", "dialog")
    expect(more).to_have_attribute("aria-label", "More actions for Me at the zoo")
    more.click()
    sheet = page.locator("#actionsheet")
    expect(sheet).to_have_attribute("open", "")
    expect(page.locator("#actionsheet-title")).to_have_text("Me at the zoo")
    expect(sheet.locator(".sheetlist > *")).to_have_text(["Play", "en.srt", "de.srt", "QR", "Keep", "Remove"])
    expect(sheet.locator(".sheetlist a.srt")).to_have_count(2)
    sheet.locator("button.keep").click()
    expect(sheet).not_to_have_attribute("open", "")
    expect(row.locator(".badge.kept")).to_be_visible()
    expect(row.locator("button.more")).to_be_focused()
    row.locator("button.more").click()                               # rebuilt from the new list
    expect(sheet.locator(".sheetlist button.unkeep")).to_be_visible()
    page.click("#actionsheetclose")
    expect(sheet).not_to_have_attribute("open", "")
    expect(row.locator("button.more")).to_be_focused()


def test_phone_failed_and_queued_rows_pick_their_primary_action(touch_page, server):
    expect = _expect()
    page = touch_page
    server.store.add_many([
        {"url": "https://www.youtube.com/watch?v=q", "title": "Queued one", "status": QUEUED},
        {"url": "https://www.youtube.com/watch?v=f", "title": "Failed one", "status": FAILED, "error": "Private video"},
    ])
    sheet = page.locator("#actionsheet")
    failed = page.locator("#jobs tr.job[data-status=failed]")
    expect(failed.locator("td.actions > *")).to_have_text(["Retry", "More"])
    failed.locator("button.more").click()
    expect(sheet.locator(".sheetlist > *")).to_have_text(["Details", "Remove"])
    page.keyboard.press("Escape")
    expect(sheet).not_to_have_attribute("open", "")
    expect(failed.locator("button.more")).to_be_focused()
    queued = page.locator("#jobs tr.job[data-status=queued]")
    expect(queued.locator("td.actions > *")).to_have_text(["Pause", "More"])
    queued.locator("button.more").click()
    expect(sheet.locator(".sheetlist > *")).to_have_text(["Next", "Cancel"])
    sheet.locator("button.next").click()
    expect(sheet).not_to_have_attribute("open", "")
    expect(queued.locator(".badge.next")).to_be_visible()


def test_desktop_rows_keep_every_action_inline(page, server, settings):
    expect = _expect()
    _done_job(server, settings)
    row = page.locator("#jobs tr.job").first
    expect(row.locator("td.actions > *")).to_have_text(["Play", "Download", "QR", "Keep", "Remove"])
    expect(page.locator("#jobs button.more")).to_have_count(0)


def test_skeleton_rows_show_until_the_first_job_list(browser, server):
    expect = _expect()
    server.store.add_many([{"url": "https://www.youtube.com/watch?v=q", "title": "Queued one", "status": QUEUED}])
    pg = browser.new_page()
    held = []
    pg.route("**/api/events", lambda route: held.append(route))      # stall the first frame
    pg.route("**/api/jobs", lambda route: held.append(route))
    pg.goto(server.base + "/", wait_until="domcontentloaded")
    try:
        expect(pg.locator("#jobs tr.skeleton")).to_have_count(3)
        expect(pg.locator("table.jobs")).to_have_attribute("aria-busy", "true")
        expect(pg.locator("#jobs tr.skeleton").first).to_have_attribute("aria-hidden", "true")
        pg.wait_for_timeout(800)
        expect(pg.locator("#jobs tr.skeleton")).to_have_count(3)     # still waiting: nothing arrived
        for route in held:
            route.continue_()
        pg.unroute("**/api/events")
        pg.unroute("**/api/jobs")
        expect(pg.locator("#jobs tr.job")).to_have_count(1)
        expect(pg.locator("#jobs tr.skeleton")).to_have_count(0)
        expect(pg.locator("table.jobs")).not_to_have_attribute("aria-busy", re.compile(".*"))
    finally:
        pg.close()


def test_empty_state_button_focuses_the_link_box(page, server):
    expect = _expect()
    btn = page.locator("#jobs td.empty button.pastehint")
    expect(btn).to_have_text("Paste a link")
    btn.click()
    expect(page.locator("#url")).to_be_focused()


def test_downloading_bar_has_a_moving_stripe_unless_motion_is_reduced(browser, server):
    expect = _expect()
    server.store.add_many([{"url": "https://www.youtube.com/watch?v=d", "title": "Downloading now",
                            "status": DOWNLOADING, "progress": 40}])
    for motion, want in (("no-preference", "stripe"), ("reduce", "none")):
        ctx = browser.new_context(reduced_motion=motion)
        pg = ctx.new_page()
        pg.goto(server.base + "/")
        bar = pg.locator("#jobs tr.job[data-status=downloading] .bar > span")
        expect(bar).to_have_count(1)
        assert bar.evaluate("el => getComputedStyle(el).animationName") == want
        assert bar.evaluate("el => getComputedStyle(el).backgroundImage") != "none"
        ctx.close()
