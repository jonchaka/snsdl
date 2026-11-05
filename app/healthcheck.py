"""Docker health probe: `python -m app.healthcheck` exits 0 when /api/auth answers 200 over
the scheme the server uses (HTTPS when HTTPS is on, certificate not verified), else 1."""
import os
import ssl
import sys
import urllib.request

from .config import _parse_bool

PORT = 8080
TIMEOUT = 4   # below the HEALTHCHECK --timeout of 5 s


def url(env) -> str:
    scheme = "https" if _parse_bool("HTTPS", env.get("HTTPS", "0")) else "http"
    return f"{scheme}://127.0.0.1:{PORT}/api/auth"


def check(env=os.environ, opener=urllib.request.urlopen) -> int:
    try:
        target = url(env)
        kwargs = {"timeout": TIMEOUT}
        if target.startswith("https://"):
            # The certificate names the LAN address, not 127.0.0.1, and may be self-signed.
            kwargs["context"] = ssl._create_unverified_context()
        with opener(target, **kwargs) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:  # noqa: BLE001 - any failure means unhealthy
        return 1


if __name__ == "__main__":
    sys.exit(check())
