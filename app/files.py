"""Where a finished job's file lives: the downloads dir, or KEEP_DIR once the user kept it.

Every route that serves, zips or links a file goes through these helpers so the two
directories stay one namespace from the user's point of view."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from .config import Settings
from .jobs import Job

log = logging.getLogger(__name__)


def _valid_name(name: Optional[str]) -> bool:
    """A bare file name: no path separators, NUL, or leading dot (hidden files, '.', '..')."""
    return bool(name) and not any(c in name for c in "/\\\x00") and not name.startswith(".")


def _lookup(directory: Path, name: str) -> Optional[Path]:
    """directory/name resolved, if it is a regular file directly inside directory. Resolving
    first means a symlink pointing outside the directory is refused."""
    base = directory.resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        return None
    return path


def unique_name(directory: Path, name: str) -> str:
    """name, or 'stem (2).ext', 'stem (3).ext'… — the first one not taken in directory.
    lexists so a dangling symlink also counts as taken (a move would clobber it)."""
    if not os.path.lexists(directory / name):
        return name
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    n = 2
    while os.path.lexists(directory / f"{stem} ({n}){suffix}"):
        n += 1
    return f"{stem} ({n}){suffix}"


def file_path(settings: Settings, job: Job) -> Optional[Path]:
    """The job's file: keep_dir/filename for a kept job when it is there, else
    downloads/filename; None when there is no such file."""
    if not _valid_name(job.filename):
        return None
    if job.keep and settings.keep_dir is not None:
        kept = settings.keep_dir / job.filename
        if kept.is_file():
            return kept
    path = settings.downloads_dir / job.filename
    return path if path.is_file() else None


def _move(src_dir: Path, dst_dir: Path, filename: Optional[str]) -> Optional[str]:
    """Move filename from src_dir to dst_dir under a free name; return the final name.
    A missing source (already moved, deleted by hand) leaves the name unchanged."""
    if not _valid_name(filename):
        return filename
    src = src_dir / filename
    if not src.is_file():
        log.info("move: %s is not in %s; leaving the job's filename as is", filename, src_dir)
        return filename
    dst_dir.mkdir(parents=True, exist_ok=True)
    final = unique_name(dst_dir, filename)
    # shutil.move copies then deletes when KEEP_DIR is on another filesystem (a separate volume).
    shutil.move(str(src), str(dst_dir / final))
    return final


def move_to_keep(settings: Settings, job: Job) -> str:
    """Move the job's file into KEEP_DIR (no-op without one); returns the possibly suffixed name."""
    if settings.keep_dir is None:
        return job.filename
    return _move(settings.downloads_dir, settings.keep_dir, job.filename)


def move_back(settings: Settings, job: Job) -> str:
    """Move a kept file back into the downloads dir (no-op without KEEP_DIR)."""
    if settings.keep_dir is None:
        return job.filename
    return _move(settings.keep_dir, settings.downloads_dir, job.filename)


def find_served_file(settings: Settings, name: str) -> Optional[Path]:
    """The file /files/{name} serves: downloads first, then KEEP_DIR; None if neither has it."""
    if not _valid_name(name):
        return None
    dirs = [settings.downloads_dir]
    if settings.keep_dir is not None:
        dirs.append(settings.keep_dir)
    for directory in dirs:
        path = _lookup(directory, name)
        if path is not None:
            return path
    return None
