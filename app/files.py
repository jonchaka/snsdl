"""Where a finished job's file lives: the downloads dir, or KEEP_DIR once the user kept it
(with MEDIA_LAYOUT, a per-uploader folder `KEEP_DIR/<subdir>`).

Every route that serves, zips or links a file goes through these helpers so the two
directories stay one namespace from the user's point of view."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import thumbs
from .config import Settings
from .jobs import DONE, Job

log = logging.getLogger(__name__)

SUBDIR_CHARS = 80                         # longest media-layout folder name
OTHER_SUBDIR = "Other"                    # folder for a job without a usable uploader
_SUBDIR_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_XML_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")   # not allowed in XML 1.0
POSTER_SKIP = {".avif"}                   # media servers do not read AVIF posters
# Serialize keep() per job: the worker's auto-keep and a user pressing Keep can race on one
# job, while different jobs may move at the same time. Locks are never deleted (one per job id
# seen; negligible).
_job_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(job_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _job_locks.get(job_id)
        if lock is None:
            lock = _job_locks[job_id] = threading.Lock()
        return lock


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


def _keep_subdir(settings: Settings, subdir: Optional[str]) -> Optional[Path]:
    """KEEP_DIR, or KEEP_DIR/subdir when subdir is set; None without KEEP_DIR, for a subdir
    that is not one valid path segment, one that resolves outside KEEP_DIR (a symlink), or one
    taken by something that is not a directory (a regular file)."""
    if settings.keep_dir is None:
        return None
    if subdir is None:
        return settings.keep_dir
    if not _valid_name(subdir):
        return None
    path = settings.keep_dir / subdir
    if path.resolve().parent != settings.keep_dir.resolve():
        return None
    if os.path.lexists(path) and not path.is_dir():
        return None
    return path


def media_subdir(job: Job) -> str:
    """The media-layout folder for the job: its uploader made into one safe path segment."""
    name = _SUBDIR_UNSAFE.sub(" ", job.uploader or "")
    name = " ".join(name.split()).strip(" .")[:SUBDIR_CHARS].strip(" .")
    return name or OTHER_SUBDIR


def served_path(job: Job) -> str:
    """The path under /files/ of the job's file: "subdir/name" in media layout, else "name"."""
    return f"{job.subdir}/{job.filename}" if job.subdir else job.filename


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


def _job_dirs(settings: Settings, job: Job) -> list[Path]:
    """Where the job's files may be, in lookup order: KEEP_DIR (or KEEP_DIR/subdir) first
    for a kept job."""
    dirs = [settings.downloads_dir]
    if job.keep:
        kept = _keep_subdir(settings, job.subdir)
        if kept is not None:
            dirs.insert(0, kept)
    return dirs


def file_path(settings: Settings, job: Job) -> Optional[Path]:
    """The job's file: KEEP_DIR[/subdir]/filename for a kept job when it is there, else
    downloads/filename; None when there is no such file."""
    if not _valid_name(job.filename):
        return None
    for directory in _job_dirs(settings, job):
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
    """Move the job's file into KEEP_DIR (KEEP_DIR/job.subdir when set); returns the possibly
    suffixed name, or job.filename unchanged when there is nothing to move (no KEEP_DIR, no
    file, no filename, an unusable subdir)."""
    dst = _keep_subdir(settings, job.subdir)
    if dst is None:
        return job.filename
    return _move(settings.downloads_dir, dst, job.filename)


def move_back(settings: Settings, job: Job) -> Optional[str]:
    """Move a kept file (from KEEP_DIR or KEEP_DIR/job.subdir) back into the downloads dir
    (no-op without KEEP_DIR)."""
    src = _keep_subdir(settings, job.subdir)
    if src is None:
        return job.filename
    return _move(src, settings.downloads_dir, job.filename)


def find_served_file(settings: Settings, path: str) -> Optional[Path]:
    """The file /files/{path} serves. `name`: downloads first, then KEEP_DIR. `subdir/name`:
    KEEP_DIR/subdir only (a media-layout folder). None for anything else."""
    parts = path.split("/")
    if len(parts) == 2:
        subdir, name = parts
        directory = _keep_subdir(settings, subdir) if _valid_name(subdir) else None
        if directory is None or not _valid_name(name):
            return None
        return _lookup(directory, name)
    if len(parts) != 1 or not _valid_name(path):
        return None
    dirs = [settings.downloads_dir]
    if settings.keep_dir is not None:
        dirs.append(settings.keep_dir)
    for directory in dirs:
        found = _lookup(directory, path)
        if found is not None:
            return found
    return None


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
    """Move the job's sidecars along with its video (into KEEP_DIR[/job.subdir], or back);
    returns their final names. A sidecar keeps its name unless that name is taken in the
    target directory. No-op without KEEP_DIR."""
    names = list(job.sidecars or [])
    kept = _keep_subdir(settings, job.subdir)
    if kept is None:
        return names
    src, dst = (settings.downloads_dir, kept) if to_keep else (kept, settings.downloads_dir)
    out = []
    for name in names:
        try:
            out.append(_move(src, dst, name))
        except OSError as exc:   # a subtitle file must not block keeping the video
            log.warning("could not move sidecar %s: %s", name, exc)
            out.append(name)
    return out


# -- media layout extras (.nfo, poster) ------------------------------------------------------

def _nfo_name(filename: str) -> str:
    return f"{Path(filename).stem}.nfo"


def _poster_name(filename: str, suffix: str) -> str:
    return f"{Path(filename).stem}-poster{suffix}"


def _xml_text(value) -> str:
    return _XML_UNSAFE.sub("", str(value))


def _dateadded(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write_nfo(path: Path, job: Job) -> Optional[Path]:
    """Write `<stem>.nfo` (a Kodi/Jellyfin `<movie>` document) beside the video at `path`.
    Elements with an unknown value are left out. Best effort: an OSError is logged and None
    returned; the written path otherwise."""
    root = ET.Element("movie")
    runtime = round(job.duration / 60) if job.duration else None
    for tag, value in (("title", job.title or Path(path).stem), ("plot", job.description),
                       ("premiered", job.upload_date), ("studio", job.uploader),
                       ("runtime", runtime)):
        if value is not None and value != "":
            ET.SubElement(root, tag).text = _xml_text(value)
    if job.url:
        ET.SubElement(root, "uniqueid", {"type": "url", "default": "true"}).text = _xml_text(job.url)
    added = _dateadded(job.finished_at)
    if added:
        ET.SubElement(root, "dateadded").text = added
    nfo = Path(path).with_name(_nfo_name(Path(path).name))
    try:
        ET.ElementTree(root).write(nfo, encoding="utf-8", xml_declaration=True)
    except OSError as exc:
        log.warning("could not write %s: %s", nfo.name, exc)
        return None
    return nfo


def write_poster(path: Path, job: Job, settings: Settings) -> Optional[Path]:
    """Copy the job's thumbnail beside the video at `path` as `<stem>-poster.<ext>` (skipped
    without a thumbnail or for AVIF). Best effort: an OSError is logged and None returned."""
    thumb = thumbs.find(settings.thumbs_dir, job.thumb) if job.thumb else None
    if thumb is None or thumb.suffix.lower() in POSTER_SKIP:
        return None
    poster = Path(path).with_name(_poster_name(Path(path).name, thumb.suffix.lower()))
    try:
        shutil.copyfile(thumb, poster)
    except OSError as exc:
        log.warning("could not write %s: %s", poster.name, exc)
        return None
    return poster


def extras_paths(settings: Settings, job: Job) -> list[Path]:
    """The job's .nfo and poster that exist beside its file. They are only ever written into a
    media-layout folder, so a job without `subdir` has none. Same containment check as
    sidecar_paths; unresolved paths."""
    if not job.subdir or not _valid_name(job.filename):
        return []
    directory = _keep_subdir(settings, job.subdir)
    if directory is None:
        return []
    names = [_nfo_name(job.filename)]
    names += [_poster_name(job.filename, f".{ext}") for ext in thumbs.MEDIA_TYPES
              if f".{ext}" not in POSTER_SKIP]
    return [directory / name for name in names if _lookup(directory, name) is not None]


def remove_empty_subdir(settings: Settings, subdir: Optional[str]) -> None:
    """Remove KEEP_DIR/subdir if it is empty (never KEEP_DIR itself); errors are ignored."""
    if not subdir:
        return
    directory = _keep_subdir(settings, subdir)
    if directory is None:
        return
    try:
        directory.rmdir()
    except OSError:   # not empty, missing, or not a directory
        pass


def delete_files(settings: Settings, job: Job) -> None:
    """Delete the job's video, its sidecars and media-layout extras, wherever they currently
    are (and the media-layout folder once it is empty)."""
    paths = sidecar_paths(settings, job) + extras_paths(settings, job)
    video = file_path(settings, job)
    if video is not None:
        paths.insert(0, video)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete %s: %s", path.name, exc)
    if job.keep and job.subdir:
        remove_empty_subdir(settings, job.subdir)


# -- keep / unkeep ------------------------------------------------------------------------

def keep(settings: Settings, store, job_id: str, value: bool) -> Optional[Job]:
    """Set or clear `keep` on a done job, moving its file and sidecars into or out of KEEP_DIR
    (a no-op without one). With MEDIA_LAYOUT a kept file goes into KEEP_DIR/<media_subdir>
    with an .nfo and a poster beside it; unkeep deletes those and the emptied folder.
    Blocking (a move across filesystems is a full copy). Returns the updated job, or None when
    it is missing, not done, or vanished meanwhile; a job whose `keep` already equals `value`
    is returned unchanged. Any other OSError from the move propagates, with `keep` left as it
    was. Serialized per job (_lock_for) so two calls for one job cannot both move its file;
    calls for different jobs run concurrently."""
    with _lock_for(job_id):
        return _keep_locked(settings, store, job_id, value)


def _keep_locked(settings: Settings, store, job_id: str, value: bool) -> Optional[Job]:
    job = store.get(job_id)
    if job is None or job.status != DONE:
        return None
    if job.keep == value:   # nothing to do (e.g. auto-keep and a user's Keep both asked)
        return job
    layout = settings.media_layout and settings.keep_dir is not None
    old_subdir = job.subdir if job.keep else None
    if value:
        if layout:
            subdir = media_subdir(job)
            if _keep_subdir(settings, subdir) is None:   # e.g. a symlink or a file by that name
                log.warning("keep: folder %r is unusable; keeping %s in KEEP_DIR", subdir, job.id)
                subdir = None
        else:
            subdir = None
        target = replace(job, subdir=subdir)
    else:
        subdir = None
        target = replace(job, subdir=old_subdir)
    new_extras: list[Path] = []
    try:
        if value:
            filename = move_to_keep(settings, target)
            # Sidecars follow the video; one that cannot be moved is logged and left behind.
            sidecars = move_sidecars(settings, target, to_keep=True)
            if layout and subdir:
                moved = replace(target, filename=filename, keep=True)
                video = file_path(settings, moved)
                if video is not None and video.parent != settings.downloads_dir:
                    new_extras = [p for p in (write_nfo(video, moved), write_poster(video, moved, settings)) if p]
        else:
            # Found before the move (they are named after the kept filename) but deleted after
            # it, so a failing move leaves the kept job intact with its extras.
            extras = extras_paths(settings, replace(job, subdir=old_subdir, keep=True))
            filename = move_back(settings, target)
            sidecars = move_sidecars(settings, target, to_keep=False)
            for p in extras:
                try:
                    p.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not delete %s: %s", p.name, exc)
    except FileNotFoundError:   # the file (or its job) went away mid-move
        return None
    if not value:
        remove_empty_subdir(settings, old_subdir)
    updated = store.update(job_id, keep=value, filename=filename, sidecars=sidecars, subdir=subdir)
    if updated is None:   # cleared by Clear all / nightly cleanup in between
        if settings.keep_dir is not None:
            # Its remover never saw the files at the new place: don't leave orphans there.
            dest = _keep_subdir(settings, subdir) if value else settings.downloads_dir
            if dest is not None:
                for name in [filename, *sidecars]:
                    if name and _valid_name(name):
                        (dest / name).unlink(missing_ok=True)
            for p in new_extras:
                p.unlink(missing_ok=True)
            if value:
                remove_empty_subdir(settings, subdir)
        return None
    return updated
