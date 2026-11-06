"""Backup and restore of the small state files: subscriptions, history, the queue pause and the
cookie files (the user's logins: a backup must be kept private).

A backup is a stored (uncompressed) zip with a `manifest.json`. Restoring validates every member
before writing anything, then replaces the files (temp + os.replace) and reloads the stores.
Downloads, jobs and thumbnails are not part of it."""
from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import cookies as cookies_mod
from .config import Settings
from .history import Entry
from .subscriptions import Subscription

log = logging.getLogger(__name__)

APP = "sns-downloader"
VERSION = 1
MANIFEST = "manifest.json"
SUBSCRIPTIONS = "subscriptions.json"
HISTORY = "history.json"
STATE = "state.json"
JSON_FILES = (SUBSCRIPTIONS, HISTORY, STATE)   # always in a backup, relative to data_dir
LEGACY_COOKIES = "config/cookies.txt"          # settings.cookies_file
KEPT_COOKIES_DIR = "config/cookies/"           # data_dir/config/cookies/<id>.txt
BACKUP_FILES = JSON_FILES + (LEGACY_COOKIES,)  # plus the kept cookie files present
MAX_BACKUP = 20 * 1024 * 1024    # whole zip, both ways
MAX_MEMBER = 5 * 1024 * 1024     # one file inside it
MAX_MEMBERS = 200
STATE_KEYS = {"queue_paused"}


class BackupError(ValueError):
    """The upload is not a usable backup; str(exc) says why in one line."""


class BackupTooLarge(Exception):
    """The files to back up exceed MAX_BACKUP."""


def _kept_cookie_dir(settings: Settings) -> Path:
    return settings.data_dir / "config" / "cookies"


def _kept_cookie_name(arcname: str) -> Optional[str]:
    """'config/cookies/<id>.txt' -> '<id>.txt' for a valid entry id, else None."""
    if not arcname.startswith(KEPT_COOKIES_DIR):
        return None
    name = arcname[len(KEPT_COOKIES_DIR):]
    stem, dot, ext = name.rpartition(".")
    if dot and ext == "txt" and cookies_mod.ID_PATTERN.match(stem):
        return name
    return None


def _target(settings: Settings, arcname: str) -> Optional[Path]:
    """Where a member is restored to, or None when it is not an allowed path."""
    if arcname in JSON_FILES:
        return settings.data_dir / arcname
    if arcname == LEGACY_COOKIES:
        return settings.cookies_file
    name = _kept_cookie_name(arcname)
    return _kept_cookie_dir(settings) / name if name else None


def _default_json(arcname: str) -> bytes:
    return b'{"queue_paused": false}' if arcname == STATE else b"[]"


def make_backup(settings: Settings) -> bytes:
    """The backup zip. A missing store file is backed up as its empty default so a restore
    always replaces all three."""
    members: list[tuple[str, bytes]] = []
    total = 0

    def add(arcname: str, data: bytes) -> None:
        nonlocal total
        total += len(data)
        if total > MAX_BACKUP or len(data) > MAX_MEMBER:
            raise BackupTooLarge(f"the files to back up are larger than {MAX_BACKUP // (1024 * 1024)} MB")
        members.append((arcname, data))

    for arcname in JSON_FILES:
        try:
            add(arcname, (settings.data_dir / arcname).read_bytes())
        except FileNotFoundError:
            add(arcname, _default_json(arcname))
    if settings.cookies_file.is_file():
        add(LEGACY_COOKIES, settings.cookies_file.read_bytes())
    for path in sorted(cookies_mod.CookieStore(settings).kept_files()):
        try:
            add(KEPT_COOKIES_DIR + path.name, path.read_bytes())
        except OSError as exc:   # deleted meanwhile
            log.info("backup: skipping cookie file %s: %s", path.name, exc)
    manifest = {"app": APP, "version": VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": [name for name, _ in members]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(MANIFEST, json.dumps(manifest, indent=2))
        for arcname, data in members:
            zf.writestr(arcname, data)
    return buf.getvalue()


# -- restore ---------------------------------------------------------------------------------

def _read_members(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_BACKUP:
        raise BackupError(f"the backup is larger than {MAX_BACKUP // (1024 * 1024)} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise BackupError("not a zip file") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise BackupError(f"more than {MAX_MEMBERS} files in the backup")
        out: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            if name in out:
                raise BackupError(f"{name[:80]!r} appears twice")
            if name != MANIFEST and not _target_name_ok(name):
                raise BackupError(f"unexpected file {name[:80]!r} in the backup")
            if info.file_size > MAX_MEMBER:
                raise BackupError(f"{name} is larger than {MAX_MEMBER // (1024 * 1024)} MB")
            try:
                with zf.open(info) as fh:
                    content = fh.read(MAX_MEMBER + 1)   # never trust the header's size alone
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, OSError,
                    EOFError) as exc:
                raise BackupError(f"{name} cannot be read: {exc}"[:200]) from None
            if len(content) > MAX_MEMBER:
                raise BackupError(f"{name} is larger than {MAX_MEMBER // (1024 * 1024)} MB")
            out[name] = content
    return out


def _target_name_ok(name: str) -> bool:
    """An allowed member path (also rules out '../', absolute paths and directories)."""
    return name in BACKUP_FILES or _kept_cookie_name(name) is not None


def _json(name: str, content: bytes):
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError(f"{name} is not valid JSON") from None


def _bad_field(obj) -> Optional[str]:
    """The first field whose value has another type than its default (str, bool, int, list;
    fields defaulting to None are not checked), else None."""
    for f in dataclasses.fields(obj):
        if f.default is not dataclasses.MISSING:
            default = f.default
        elif f.default_factory is not dataclasses.MISSING:
            default = f.default_factory()
        else:
            if not isinstance(getattr(obj, f.name), str):   # format, quality: required strings
                return f.name
            continue
        if default is None:
            continue
        if type(getattr(obj, f.name)) is not type(default):
            return f.name
    return None


def _check_list(name: str, value, cls) -> int:
    """value is a list of objects `cls.from_dict` accepts, with string ids (unique) and urls."""
    if not isinstance(value, list):
        raise BackupError(f"{name} must be a list")
    ids = set()
    for number, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise BackupError(f"{name}: entry {number} is not an object")
        try:
            obj = cls.from_dict(item)
        except (TypeError, ValueError):
            raise BackupError(f"{name}: entry {number} is missing required fields") from None
        if not isinstance(obj.id, str) or not obj.id or not isinstance(obj.url, str):
            raise BackupError(f"{name}: entry {number} has a bad id or url")
        bad = _bad_field(obj)
        if bad:
            raise BackupError(f"{name}: entry {number} has a bad {bad}")
        if obj.id in ids:
            raise BackupError(f"{name}: entry {number} repeats id {obj.id[:40]!r}")
        ids.add(obj.id)
    return len(value)


def _check_state(value) -> None:
    if not isinstance(value, dict) or set(value) - STATE_KEYS:
        raise BackupError(f"{STATE} must be an object with only {', '.join(sorted(STATE_KEYS))}")
    if not isinstance(value.get("queue_paused", False), bool):
        raise BackupError(f"{STATE}: queue_paused must be true or false")


def _validate(members: dict[str, bytes]) -> dict:
    if MANIFEST not in members:
        raise BackupError("no manifest.json: not an sns-downloader backup")
    manifest = _json(MANIFEST, members[MANIFEST])
    if not isinstance(manifest, dict) or manifest.get("app") != APP:
        raise BackupError("manifest.json is not an sns-downloader manifest")
    if manifest.get("version") != VERSION:
        raise BackupError(f"unsupported backup version {str(manifest.get('version'))[:20]}")
    files = manifest.get("files")
    if not isinstance(files, list) or sorted(map(str, files)) != sorted(n for n in members if n != MANIFEST):
        raise BackupError("manifest.json does not list the files in the backup")
    for name in JSON_FILES:
        if name not in members:
            raise BackupError(f"the backup has no {name}")
    counts = {"subscriptions": _check_list(SUBSCRIPTIONS, _json(SUBSCRIPTIONS, members[SUBSCRIPTIONS]), Subscription),
              "history": _check_list(HISTORY, _json(HISTORY, members[HISTORY]), Entry),
              "cookies": 0}
    _check_state(_json(STATE, members[STATE]))
    for name, content in members.items():
        if name in JSON_FILES or name == MANIFEST:
            continue
        try:
            cookies_mod.parse_netscape(content.decode("utf-8"))
        except UnicodeDecodeError:
            raise BackupError(f"{name}: not a text file") from None
        except cookies_mod.CookieFormatError as exc:
            raise BackupError(f"{name}: {exc}") from None
        counts["cookies"] += 1
    return counts


def _write_atomic(path: Path, content: bytes, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.restore.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600 if private else 0o644)
    try:
        if private:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def restore_backup(settings: Settings, data: bytes, subs, history, app_state, pool=None) -> dict:
    """Replace subscriptions, history, the queue pause and the cookie files with the backup's.
    Raises BackupError (nothing written) when anything in it does not validate. The kept cookie
    folder is emptied first so cookies deleted since the backup do not come back; the legacy
    cookies.txt is replaced only when the backup has one. Returns the counts."""
    members = _read_members(data)
    counts = _validate(members)
    for old in cookies_mod.CookieStore(settings).kept_files():
        old.unlink(missing_ok=True)
    for name, content in members.items():
        if name == MANIFEST:
            continue
        target = _target(settings, name)
        if name in JSON_FILES:
            _write_atomic(target, content)
        else:
            text = content.decode("utf-8")
            _write_atomic(target, cookies_mod.with_header(text).encode("utf-8"), private=True)
    log.info("restored a backup: %d subscriptions, %d history entries, %d cookie files",
             counts["subscriptions"], counts["history"], counts["cookies"])
    subs.load()
    history.load()
    app_state.load()
    if pool is not None:
        pool.pause() if app_state.queue_paused else pool.resume()
    return counts
