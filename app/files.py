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
    dirs = [settings.downloads_dir]
    if job.keep and settings.keep_dir is not None:
        dirs.insert(0, settings.keep_dir)
    for directory in dirs:
        # The same containment check as /files/ (a symlink pointing outside is refused), but
        # the unresolved path is returned: callers unlink it, which must hit the link itself.
        if _lookup(directory, job.filename) is not None:
            return directory / job.filename
    return None


def _move(src_dir: Path, dst_dir: Path, filename: Optional[str]) -> Optional[str]:
    """Move filename from src_dir to dst_dir under a free name; return the final name.
    A missing source (already moved, deleted by hand) leaves the name unchanged."""
    if not _valid_name(filename):
        return filename
    src = src_dir / filename
    if src.is_symlink() or not src.is_file():   # a symlink is not a download: never move it
        log.info("move: %s is not a regular file in %s; leaving the job's filename as is", filename, src_dir)
        return filename
    dst_dir.mkdir(parents=True, exist_ok=True)
    final = unique_name(dst_dir, filename)
    # shutil.move copies then deletes when KEEP_DIR is on another filesystem (a separate volume).
    shutil.move(str(src), str(dst_dir / final))
    return final


def move_to_keep(settings: Settings, job: Job) -> Optional[str]:
    """Move the job's file into KEEP_DIR; returns the possibly suffixed name, or job.filename
    unchanged when there is nothing to move (no KEEP_DIR, no file, no filename)."""
    if settings.keep_dir is None:
        return job.filename
    return _move(settings.downloads_dir, settings.keep_dir, job.filename)


def move_back(settings: Settings, job: Job) -> Optional[str]:
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


def _job_dirs(settings: Settings, job: Job) -> list[Path]:
    """Where the job's files may be, in lookup order: KEEP_DIR first for a kept job."""
    dirs = [settings.downloads_dir]
    if job.keep and settings.keep_dir is not None:
        dirs.insert(0, settings.keep_dir)
    return dirs


def sidecar_paths(settings: Settings, job: Job) -> list[Path]:
    """The job's separate subtitle files that exist, beside the video's current location
    (or, with the video gone, wherever file_path would look). Same containment check as
    file_path; unresolved paths so an unlink hits a link itself; missing ones are skipped."""
    video = file_path(settings, job)
    dirs = [video.parent] if video is not None else _job_dirs(settings, job)
    out: list[Path] = []
    for name in job.sidecars or []:
        if not _valid_name(name):
            continue
        for directory in dirs:
            if _lookup(directory, name) is not None:
                out.append(directory / name)
                break
    return out


def move_sidecars(settings: Settings, job: Job, to_keep: bool) -> list[str]:
    """Move the job's sidecars along with its video (into KEEP_DIR, or back); returns their
    final names. A sidecar keeps its name unless that name is taken in the target directory.
    No-op without KEEP_DIR."""
    names = list(job.sidecars or [])
    if settings.keep_dir is None:
        return names
    src, dst = (settings.downloads_dir, settings.keep_dir) if to_keep else (settings.keep_dir, settings.downloads_dir)
    out = []
    for name in names:
        try:
            out.append(_move(src, dst, name))
        except OSError as exc:   # a subtitle file must not block keeping the video
            log.warning("could not move sidecar %s: %s", name, exc)
            out.append(name)
    return out


def delete_files(settings: Settings, job: Job) -> None:
    """Delete the job's video and its sidecars, wherever they currently are."""
    paths = sidecar_paths(settings, job)
    video = file_path(settings, job)
    if video is not None:
        paths.insert(0, video)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete %s: %s", path.name, exc)
