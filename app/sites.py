"""Which "site" a URL belongs to, for per-site download pacing."""
from urllib.parse import urlsplit

# Subdomains that are the same site as the bare domain (mobile pages, TikTok short links).
_PREFIXES = ("www.", "m.", "vm.", "vt.", "mobile.")
_ALIASES = {"youtu.be": "youtube.com"}


def site_of_url(url: str) -> str:
    """Lowercase hostname without one leading www./m./vm./vt./mobile. prefix; youtu.be is
    youtube.com. Anything without a parseable hostname is "other" (all such jobs share one
    pacing bucket, which errs on the side of politeness)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return "other"
    if not host:
        return "other"
    for prefix in _PREFIXES:
        rest = host[len(prefix):]
        # Keep a dot in what remains so "m.com" is not reduced to "com".
        if host.startswith(prefix) and "." in rest:
            host = rest
            break
    return _ALIASES.get(host, host)
