import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

from apscheduler.triggers.cron import CronTrigger

from . import window
from .window import Window


def _parse_positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {raw!r}")
    return value


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean like 1/0, got {raw!r}")


_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _parse_size(name: str, raw: str, allow_zero: bool = False) -> int:
    """Bytes from '500000', '5M', '800k', '1.5G'… (1024-based). With allow_zero a literal zero
    ('0', '0M') is accepted (callers use it for "disabled"); anything rounding down to 0 bytes
    such as '0.5' is still rejected, since that is almost certainly a typo."""
    value = raw.strip().upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)B?", value)
    if match and allow_zero and float(match.group(1)) == 0:
        return 0
    size = int(float(match.group(1)) * _SIZE_UNITS[match.group(2)]) if match else 0
    if size < 1:
        zero = "0 (disabled), " if allow_zero else ""
        raise ValueError(f"{name} must look like {zero}5M, 800K, 1G or a number of bytes, got {raw!r}")
    return size


def _parse_rate(name: str, raw: str) -> Optional[int]:
    """Bytes per second from '', '500000', '5M', '800k'… (1024-based). Empty means no cap."""
    if not raw.strip():
        return None
    return _parse_size(name, raw)


def _parse_int_range(name: str, raw: str, low: int, high: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}, got {raw!r}")
    return value


def _parse_nonneg_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f"{name} must be an integer >= 0, got {raw!r}")
    return value


def _parse_nonneg_float(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    # isfinite rejects "nan"/"inf", which float() accepts but no one means as a pause.
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a number of seconds >= 0, got {raw!r}")
    return value


def _parse_optional_days(name: str, raw: str) -> Optional[int]:
    """A number of days >= 1; empty or 0 disables (None)."""
    value = raw.strip()
    if value in ("", "0"):
        return None
    try:
        days = int(value)
    except ValueError:
        days = 0
    if days < 1:
        raise ValueError(f"{name} must be an integer >= 1 (empty or 0 = off), got {raw!r}")
    return days


def _parse_optional_path(raw: str) -> Optional[Path]:
    value = raw.strip()
    return Path(value) if value else None


def _parse_pin(name: str, raw: str) -> Optional[str]:
    """4..64 characters without whitespace; empty disables. The value never appears in the
    error message (it is a secret, and the message ends up in container logs)."""
    if not raw.strip():
        return None
    if not 4 <= len(raw) <= 64 or any(c.isspace() for c in raw):
        raise ValueError(f"{name} must be 4 to 64 characters without spaces (value not shown)")
    return raw


PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")


def _parse_proxy(name: str, raw: str) -> Optional[str]:
    """A proxy URL for yt-dlp such as socks5://user:pw@host:1080; empty disables."""
    value = raw.strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
        ok = parts.scheme.lower() in PROXY_SCHEMES and bool(parts.hostname)
        parts.port   # raises ValueError on a non-numeric / out-of-range port
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a URL like socks5://host:1080 with scheme "
                         f"{', '.join(PROXY_SCHEMES)}, got {raw!r}")
    return value


_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")

_NUMERIC = re.compile(r"^[0-9.]+$")


def _is_ip(value: str) -> bool:
    # A scoped IPv6 address (fe80::1%eth0) is not something a certificate can name.
    if "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _parse_hosts(name: str, raw: str) -> list[str]:
    """Comma-separated DNS names and/or IP addresses: trimmed, lowercased, empty entries
    dropped, deduplicated in order. Each must be an IP address or a plain ASCII hostname."""
    out: list[str] = []
    for entry in raw.split(","):
        value = entry.strip().lower()
        if not value:
            continue
        is_ip = _is_ip(value)
        # Digits and dots only but not an IP (192.168.001.010, 1.2.3.4.5): the hostname
        # grammar allows it, yet no browser would ever match it as a DNS name.
        if not (is_ip or (_HOSTNAME.fullmatch(value) and not _NUMERIC.fullmatch(value))):
            raise ValueError(f"{name} entry {value!r} is not a hostname or IP address")
        if value not in out:
            out.append(value)
    return out


# Same grammar as yt-dlp's ImpersonateTarget.from_str: client[-version][:os[-version]]
_IMPERSONATE_TARGET = re.compile(r"(?:[^:\-\s]+(?:-[^:\-\s]+)?)?(?::(?:[^:\-\s]+(?:-[^:\-\s]+)?)?)?")


def _parse_impersonate(name: str, raw: str) -> Optional[str]:
    """A yt-dlp impersonation target such as 'chrome' or 'safari-17:ios'; empty disables."""
    value = raw.strip()
    if not value:
        return None
    if not _IMPERSONATE_TARGET.fullmatch(value):
        raise ValueError(f"{name} must be a yt-dlp impersonate target like chrome or chrome-120:windows-10, "
                         f"got {raw!r}")
    return value


def _parse_window(name: str, raw: str) -> Optional[Window]:
    try:
        return window.parse_window(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None


def _http_parts(raw: str):
    """urlsplit result if raw is an http/https URL with a host and a valid port, else None."""
    try:
        parts = urlsplit(raw)
        ok = parts.scheme.lower() in ("http", "https") and bool(parts.netloc) and bool(parts.hostname)
        parts.port   # raises ValueError on a non-numeric / out-of-range port
    except ValueError:
        return None
    return parts if ok else None


def _parse_webhook_url(name: str, raw: str) -> Optional[str]:
    """http/https URL with a host; empty disables. The value is not echoed in the error (webhook
    URLs often carry a token)."""
    value = raw.strip()
    if not value:
        return None
    if _http_parts(value) is None:
        raise ValueError(f"{name} must be an http:// or https:// URL with a host (value not shown)")
    return value


def _parse_public_url(name: str, raw: str) -> Optional[str]:
    """An origin such as https://192.168.1.10:8080 (trailing slash stripped); empty -> None."""
    value = raw.strip().rstrip("/")
    if not value:
        return None
    parts = _http_parts(value)
    if (parts is None or parts.path or parts.query or parts.fragment or "?" in value or "#" in value
            or parts.username is not None or "@" in parts.netloc):
        raise ValueError(f"{name} must be an origin like https://192.168.1.10:8080, got {raw!r}")
    return value


_TELEGRAM_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")
_TELEGRAM_CHAT = re.compile(r"-?\d+|@[A-Za-z0-9_]{5,}")


def _parse_telegram_token(name: str, raw: str) -> Optional[str]:
    """A bot token like 123456:ABC-DEF… from BotFather; empty disables. Never echoed (secret)."""
    value = raw.strip()
    if not value:
        return None
    if not _TELEGRAM_TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be the bot token BotFather gives, like 1234:AAH… (value not shown)")
    return value


def _parse_telegram_chat(name: str, raw: str) -> Optional[str]:
    """A numeric chat id (-100… for groups/channels) or @channelname; empty disables."""
    value = raw.strip()
    if not value:
        return None
    if not _TELEGRAM_CHAT.fullmatch(value):
        raise ValueError(f"{name} must be a numeric chat id like -1001234567890 or @channelname (value not shown)")
    return value


def _parse_cron(name: str, raw: str) -> str:
    """A 5-field crontab expression, stripped; empty disables."""
    value = raw.strip()
    if value:
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a crontab expression like '0 */6 * * *', got {raw!r}: {exc}") from None
    return value


@dataclass
class Settings:
    data_dir: Path
    max_concurrent: int
    max_playlist_items: int
    cleanup_cron: str
    tz: str
    cookies_file: Path
    embed_thumbnails: bool = True
    update_cron: str = "0 4 * * *"   # empty string disables the nightly yt-dlp update
    rate_limit: Optional[int] = None  # bytes/second per download; None = uncapped
    impersonate: Optional[str] = "chrome"   # browser TLS/HTTP fingerprint for yt-dlp (needs curl_cffi); None = off
    min_free_space: int = 1024 ** 3   # bytes the download disk must keep free before a job starts; 0 = no check
    proxy: Optional[str] = None       # yt-dlp proxy URL (may carry credentials: never log or return it)
    fragments: int = 1                # concurrent fragment downloads per job (HLS/DASH)
    max_per_site: int = 1             # downloads running at once per site; 0 = unlimited
    site_gap: float = 3.0             # seconds between two downloads from one site (jittered x0.5..x1.5)
    keep_dir: Optional[Path] = None   # kept files move here (outside cleanup's reach); None = off
    access_pin: Optional[str] = None  # PIN the UI/API asks for; None = open (never log or return it)
    https: bool = False               # serve HTTPS with the generated certificate in tls_dir
    tls_hosts: list[str] = field(default_factory=list)   # extra names/IPs the certificate covers
    sponsorblock: bool = False        # page default for "Remove sponsor segments" (YouTube)
    prefer_h264: bool = False         # page default for the plays-everywhere (H.264/AAC) option
    download_window: Optional[Window] = None   # queued jobs only start inside it; None = always
    # POSTed when a job ends; None = off; may carry a token
    webhook_url: Optional[str] = field(default=None, repr=False)
    webhook_secret: Optional[str] = field(default=None, repr=False)   # HMAC key; never log or return it
    public_url: Optional[str] = None           # origin for absolute file links in the webhook
    subscription_cron: str = "0 */6 * * *"     # empty disables scheduled subscription checks
    retention_days: Optional[int] = None       # done/failed jobs older than this are removed; None = off
    live_max_hours: int = 12                   # a live recording stops after this many hours
    media_layout: bool = False                 # kept files go into per-uploader folders under KEEP_DIR
    app_version: str = "dev"                   # set by the image build (APP_VERSION); shown in the footer
    app_commit: str = ""                       # git commit of the build (APP_COMMIT); "" when unknown
    # Telegram delivery of finished files; both or neither. Never log or return them.
    telegram_bot_token: Optional[str] = field(default=None, repr=False)
    telegram_chat_id: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.keep_dir is not None:
            keep = self.keep_dir.resolve()
            for d in (self.downloads_dir, self.tmp_dir):
                if keep.is_relative_to(d.resolve()):
                    # The nightly cleanup empties both, which would delete every kept file.
                    raise ValueError("KEEP_DIR must not be inside the downloads or tmp directory")
        if self.media_layout and self.keep_dir is None:
            raise ValueError("MEDIA_LAYOUT needs KEEP_DIR")
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID go together")

    @property
    def telegram(self) -> bool:
        """Telegram delivery is on (both the bot token and the chat id are set)."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def jobs_file(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def subscriptions_file(self) -> Path:
        return self.data_dir / "subscriptions.json"

    @property
    def history_file(self) -> Path:
        return self.data_dir / "history.json"

    @property
    def webhook_pending_file(self) -> Path:
        return self.data_dir / "config" / "webhook-pending.json"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def proxy_display(self) -> Optional[str]:
        """The proxy URL without user:password, safe to show in the UI."""
        if self.proxy is None:
            return None
        parts = urlsplit(self.proxy)
        host = parts.hostname or ""
        if ":" in host:   # IPv6 literal; hostname drops the brackets
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme.lower()}://{host}{port}"

    @property
    def tls_dir(self) -> Path:
        """CA and server certificate for HTTPS mode; created (0700) by the launcher, not here."""
        return self.data_dir / "config" / "tls"

    @property
    def lib_dir(self) -> Path:
        """Where the nightly update installs yt-dlp; on PYTHONPATH in the image."""
        return self.data_dir / "lib"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        data_dir = Path(env.get("DATA_DIR", "/data"))
        return cls(
            data_dir=data_dir,
            max_concurrent=_parse_positive_int("MAX_CONCURRENT", env.get("MAX_CONCURRENT", "2")),
            max_playlist_items=_parse_positive_int("MAX_PLAYLIST_ITEMS", env.get("MAX_PLAYLIST_ITEMS", "500")),
            cleanup_cron=env.get("CLEANUP_CRON", "0 3 * * *"),
            tz=env.get("TZ", "UTC"),
            cookies_file=Path(env.get("COOKIES_FILE", str(data_dir / "config" / "cookies.txt"))),
            embed_thumbnails=_parse_bool("EMBED_THUMBNAILS", env.get("EMBED_THUMBNAILS", "1")),
            update_cron=env.get("UPDATE_CRON", "0 4 * * *").strip(),
            rate_limit=_parse_rate("RATE_LIMIT", env.get("RATE_LIMIT", "")),
            impersonate=_parse_impersonate("IMPERSONATE", env.get("IMPERSONATE", "chrome")),
            min_free_space=_parse_size("MIN_FREE_SPACE", env.get("MIN_FREE_SPACE") or "1G", allow_zero=True),
            proxy=_parse_proxy("PROXY", env.get("PROXY", "")),
            fragments=_parse_int_range("FRAGMENTS", env.get("FRAGMENTS", "1"), 1, 16),
            max_per_site=_parse_nonneg_int("MAX_PER_SITE", env.get("MAX_PER_SITE", "1")),
            site_gap=_parse_nonneg_float("SITE_GAP", env.get("SITE_GAP", "3")),
            keep_dir=_parse_optional_path(env.get("KEEP_DIR", "")),
            access_pin=_parse_pin("ACCESS_PIN", env.get("ACCESS_PIN", "")),
            https=_parse_bool("HTTPS", env.get("HTTPS", "0")),
            tls_hosts=_parse_hosts("TLS_HOSTS", env.get("TLS_HOSTS", "")),
            sponsorblock=_parse_bool("SPONSORBLOCK", env.get("SPONSORBLOCK", "0")),
            prefer_h264=_parse_bool("PREFER_H264", env.get("PREFER_H264", "0")),
            download_window=_parse_window("DOWNLOAD_WINDOW", env.get("DOWNLOAD_WINDOW", "")),
            webhook_url=_parse_webhook_url("WEBHOOK_URL", env.get("WEBHOOK_URL", "")),
            webhook_secret=env.get("WEBHOOK_SECRET", "").strip() or None,
            public_url=_parse_public_url("PUBLIC_URL", env.get("PUBLIC_URL", "")),
            subscription_cron=_parse_cron("SUBSCRIPTION_CRON", env.get("SUBSCRIPTION_CRON", "0 */6 * * *")),
            retention_days=_parse_optional_days("RETENTION_DAYS", env.get("RETENTION_DAYS", "")),
            live_max_hours=_parse_int_range("LIVE_MAX_HOURS", env.get("LIVE_MAX_HOURS", "12"), 1, 168),
            media_layout=_parse_bool("MEDIA_LAYOUT", env.get("MEDIA_LAYOUT", "0")),
            app_version=env.get("APP_VERSION", "").strip() or "dev",
            app_commit=env.get("APP_COMMIT", "").strip(),
            telegram_bot_token=_parse_telegram_token("TELEGRAM_BOT_TOKEN", env.get("TELEGRAM_BOT_TOKEN", "")),
            telegram_chat_id=_parse_telegram_chat("TELEGRAM_CHAT_ID", env.get("TELEGRAM_CHAT_ID", "")),
        )

    def ensure_dirs(self) -> None:
        for d in (self.downloads_dir, self.tmp_dir, self.thumbs_dir, self.cookies_file.parent):
            d.mkdir(parents=True, exist_ok=True)
        if self.keep_dir is not None:
            self.keep_dir.mkdir(parents=True, exist_ok=True)
