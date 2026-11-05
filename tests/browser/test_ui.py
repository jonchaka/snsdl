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
