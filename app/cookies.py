"""Per-run copies of the user's cookies file.

yt-dlp rewrites the cookie jar it is given when it exits, so two concurrent runs
sharing one file can corrupt it. Each run gets its own copy in tmp_dir, deleted
afterwards; the user's exported file is never modified.
"""
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import Settings


@contextmanager
def cookies_for(settings: Settings, tag: str) -> Iterator[Optional[Path]]:
    """Yield a private copy of settings.cookies_file (or None if there is none)."""
    if not settings.cookies_file.is_file():
        yield None
        return
    # Name must not start with a job id: downloader globs "{job_id}-*" for outputs/partials.
    copy = settings.tmp_dir / f"cookies-{tag}.txt"
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(settings.cookies_file, copy)
        copy.chmod(0o600)   # credentials: never wider than owner-only
        yield copy
    finally:
        copy.unlink(missing_ok=True)
