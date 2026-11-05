import json
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
    expect(page.locator("#quality option")).to_have_text(["Original · ~12 MB"])
    expect(page.locator("#quality")).to_have_value("best")
    page.check("input[name=fmt][value=mp4]")
    expect(page.locator("#quality")).to_have_value("1080")
    expect(page.locator("#subbox")).to_be_visible()
    expect(page.locator("#subs option")).to_have_text(["None", "English", "English (Original) (auto)"])
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
    import json, urllib.request
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
    row = page.locator("#jobs tr.job").first
    job = server.store.list()[0]
    server.store.update(job.id, status=FAILED, error="HTTP Error 403", log="[youtube] abc: Downloading\nERROR: HTTP Error 403")
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
    page.wait_for_function("(() => { const i = document.querySelector('img.rthumb'); return i && i.complete && i.naturalWidth > 0; })()")
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
        "format": "mp4", "quality": "720", "subtitles": "en"}
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
    job = server.store.add(url, title=title)
    (settings.downloads_dir / filename).write_bytes(b"z")
    server.store.update(job.id, status=DONE, progress=100, filename=filename, size=1)
    return job


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
    page.wait_for_function("(() => { const i = document.querySelector('#qrdlg img'); return i.complete && i.naturalWidth > 0; })()")
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
    for _ in range(n):
        with pg.expect_response(lambda r: r.url.endswith("/api/jobs")):
            pass


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
