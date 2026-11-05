import pytest

from app.sites import site_of_url


@pytest.mark.parametrize("url, site", [
    ("https://www.youtube.com/watch?v=x", "youtube.com"),
    ("https://youtube.com/watch?v=x", "youtube.com"),
    ("https://m.youtube.com/watch?v=x", "youtube.com"),
    ("https://youtu.be/abc", "youtube.com"),
    ("https://www.youtu.be/abc", "youtube.com"),
    ("https://vm.tiktok.com/ZMabc/", "tiktok.com"),
    ("https://vt.tiktok.com/ZSabc/", "tiktok.com"),
    ("https://www.tiktok.com/@u/video/1", "tiktok.com"),
    ("https://mobile.twitter.com/u/status/1", "twitter.com"),
    ("https://X.COM/u/status/1", "x.com"),
    ("https://www.Instagram.com/reel/abc/", "instagram.com"),
    ("https://user:pw@www.example.com:8443/v", "example.com"),
    ("https://music.youtube.com/watch?v=x", "youtube.com"),     # same servers, same pacing
    ("https://www.youtube.com./watch?v=x", "youtube.com"),     # trailing dot (FQDN)
    ("https://youtu.be./abc", "youtube.com"),
    ("https://tiktok.com./@u/video/1", "tiktok.com"),
    ("https://music.youtube.com./watch?v=x", "youtube.com"),
    ("https://www.m.example.com/v", "m.example.com"),       # only one prefix is stripped
    ("https://m.com/v", "m.com"),                            # never strip down to a bare TLD
])
def test_site_of_url(url, site):
    assert site_of_url(url) == site


@pytest.mark.parametrize("url", ["", "not a url", "https://", "/relative/path", "http://[::1", "https://./x"])
def test_site_of_url_unparseable_is_other(url):
    assert site_of_url(url) == "other"
