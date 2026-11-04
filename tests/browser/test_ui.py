import re

import pytest

from app.jobs import DONE, FAILED

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
