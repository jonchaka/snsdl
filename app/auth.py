"""Optional access PIN (ACCESS_PIN): an HMAC cookie checked by a pure ASGI middleware.

The cookie value is HMAC-SHA256(secret, pin), so it changes when either the PIN or the per-install
secret (data/config/secret) changes, and it never contains the PIN itself."""
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from http.cookies import SimpleCookie
from typing import Callable

from .config import Settings

COOKIE = "sns_auth"
SECRET_BYTES = 32


def secret_path(settings: Settings):
    return settings.data_dir / "config" / "secret"


def secret_for(settings: Settings) -> bytes:
    """32 random bytes, created once (mode 0600) and read back on every later call."""
    path = secret_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        data = path.read_bytes()
        if len(data) == SECRET_BYTES:
            return data
        raise RuntimeError(f"{path} is damaged; delete it to create a new one (everyone signs in again)")
    secret = secrets.token_bytes(SECRET_BYTES)
    with os.fdopen(fd, "wb") as f:
        f.write(secret)
    return secret


def token(secret: bytes, pin: str) -> str:
    return hmac.new(secret, pin.encode(), hashlib.sha256).hexdigest()


def is_exempt(path: str) -> bool:
    """The page and its static assets, plus /api/auth itself; everything else under /api/ and
    /files/ needs the cookie."""
    return (path == "/" or path.startswith("/api/auth")
            or (not path.startswith("/api/") and not path.startswith("/files/")))


EXEMPT = is_exempt


def cookie_ok(cookie_header: str, expected: str) -> bool:
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:   # a malformed Cookie header simply carries no valid token
        return False
    morsel = jar.get(COOKIE)
    return morsel is not None and hmac.compare_digest(morsel.value.encode(), expected.encode())


class FailureLimiter:
    """Wrong-PIN attempts per client address in a sliding window (in memory)."""

    def __init__(self, limit: int = 5, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._fails: dict[str, deque] = {}

    def _recent(self, addr: str) -> deque:
        q = self._fails.setdefault(addr, deque())
        cutoff = self.clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def blocked(self, addr: str) -> bool:
        """True when the address has used up its `limit` failures in the window: the next
        attempt is refused without comparing."""
        q = self._recent(addr)
        if not q:
            self._fails.pop(addr, None)
        return len(q) >= self.limit

    def hit(self, addr: str) -> bool:
        """Record a failure; True when the address is now over the limit."""
        q = self._recent(addr)
        q.append(self.clock())
        return len(q) > self.limit

    def clear(self, addr: str) -> None:
        self._fails.pop(addr, None)


_UNAUTHORIZED = json.dumps({"detail": "PIN required"}).encode()


class AuthMiddleware:
    """Pure ASGI: http requests outside the exempt paths need the sns_auth cookie."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.expected = token(secret_for(settings), settings.access_pin or "")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        cookie = b"; ".join(v for k, v in scope.get("headers", []) if k == b"cookie").decode("latin-1")
        if cookie and cookie_ok(cookie, self.expected):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(_UNAUTHORIZED)).encode())]})
        await send({"type": "http.response.body", "body": _UNAUTHORIZED})
