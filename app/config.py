import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


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


_RATE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _parse_rate(name: str, raw: str) -> Optional[int]:
    """Bytes per second from '', '500000', '5M', '800k'… (1024-based). Empty means no cap."""
    value = raw.strip().upper().removesuffix("B")
    if not value:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)", value)
    if not match or float(match.group(1)) <= 0:
        raise ValueError(f"{name} must look like 5M, 800K or a number of bytes per second, got {raw!r}")
    return int(float(match.group(1)) * _RATE_UNITS[match.group(2)])


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
        )

    def ensure_dirs(self) -> None:
        for d in (self.downloads_dir, self.tmp_dir, self.cookies_file.parent):
            d.mkdir(parents=True, exist_ok=True)
