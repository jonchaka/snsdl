import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _parse_positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {raw!r}")
    return value


@dataclass
class Settings:
    data_dir: Path
    max_concurrent: int
    max_playlist_items: int
    cleanup_cron: str
    tz: str
    cookies_file: Path

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def jobs_file(self) -> Path:
        return self.data_dir / "jobs.json"

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
        )

    def ensure_dirs(self) -> None:
        for d in (self.downloads_dir, self.tmp_dir, self.cookies_file.parent):
            d.mkdir(parents=True, exist_ok=True)
