"""Runtime user for the container. The image starts as root; the launcher calls prepare_user()
first thing: it makes the data directory owned by PUID:PGID where it is not already (the keep
directory itself and the default home too), then drops to that user before uvicorn is exec'd.
A chown that the filesystem refuses (CIFS, NFS root_squash) is a warning, not an error: the
launcher's writability check decides.
Started as a non-root user already (compose `user:` or `docker run --user`) nothing is touched.
"""
import logging
import os
from pathlib import Path
from typing import Callable, Mapping, Optional

log = logging.getLogger("app.launcher")

DEFAULT_UID = 1000
HOME_DEFAULT = "/home/appuser"   # created in the image; yt-dlp keeps its cache under ~/.cache
MAX_ID = 2**31 - 1


def _parse_id(name: str, raw: str) -> int:
    value = raw.strip()
    if not (value.isascii() and value.isdigit()) or int(value) > MAX_ID:
        raise ValueError(f"{name} must be a whole number (got {value!r})")
    return int(value)


def parse_ids(env: Mapping[str, str]) -> tuple[int, int]:
    """(uid, gid) from PUID and PGID; PGID defaults to PUID, PUID to 1000. Blank values count as
    unset (Portainer's variable editor leaves blanks)."""
    uid = _parse_id("PUID", env.get("PUID", "").strip() or str(DEFAULT_UID))
    gid = _parse_id("PGID", env.get("PGID", "").strip() or str(uid))
    return uid, gid


def own_tree(path: Path, uid: int, gid: int, chown: Callable = os.chown) -> int:
    """Make path and everything below it owned by uid:gid where it is not already. Symlinks are
    changed themselves and never followed. Returns the number of entries changed; the first
    OSError propagates."""
    changed = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        here = Path(current)
        for p in ([here] if here == path else []) + [here / d for d in dirs] + [here / f for f in files]:
            st = p.lstat()
            if st.st_uid != uid or st.st_gid != gid:
                chown(p, uid, gid, follow_symlinks=False)
                changed += 1
    return changed


def own_top(path: Path, uid: int, gid: int, chown: Callable = os.chown) -> int:
    """Make the directory itself owned by uid:gid (not its contents: a keep directory is often a
    media library shared with other services). Returns 1 if changed, else 0."""
    st = path.lstat()
    if st.st_uid != uid or st.st_gid != gid:
        chown(path, uid, gid, follow_symlinks=False)
        return 1
    return 0


def _own(label: str, fn: Callable, path: Path, uid: int, gid: int, chown: Callable) -> int:
    """Run an ownership fix; a refusal by the filesystem is logged, not fatal."""
    try:
        return fn(path, uid, gid, chown)
    except OSError as exc:
        log.warning("could not re-own %s %s (errno %s); continuing, the writability check decides",
                    label, path, exc.errno)
        return 0


def prepare_user(env: Mapping[str, str], data_dir: Path, keep_dir: Optional[Path], *,
                 geteuid: Optional[Callable[[], int]] = None, setgroups: Callable = os.setgroups,
                 setgid: Callable = os.setgid, setuid: Callable = os.setuid,
                 chown: Callable = os.chown) -> dict:
    """Returns the environment to exec uvicorn with. As root: re-own data_dir (created if
    missing) recursively, the keep directory itself (if it exists) and /home/appuser (only when
    HOME is unset or that path), then switch to PUID:PGID. PUID=0 stays root and re-owns
    nothing. Not root: a copy of env, untouched. ValueError for a bad PUID/PGID (before anything
    is touched); OSError from mkdir propagates, a refused chown is only logged."""
    out = dict(env)
    euid = (geteuid or os.geteuid)()
    if euid != 0:
        return out
    uid, gid = parse_ids(env)
    home = Path(env.get("HOME") or HOME_DEFAULT)
    if uid == 0:
        log.warning("PUID=0: running as root")
        out["HOME"] = str(home)
        return out
    data_dir.mkdir(parents=True, exist_ok=True)
    changed = _own("data directory", own_tree, data_dir, uid, gid, chown)
    if keep_dir is not None and keep_dir.exists():
        changed += _own("keep directory", own_top, keep_dir, uid, gid, chown)
    if str(home) == HOME_DEFAULT and home.exists():
        changed += _own("home", own_tree, home, uid, gid, chown)
    setgroups([gid])
    setgid(gid)
    setuid(uid)
    log.info("running as %d:%d, %d entries re-owned", uid, gid, changed)
    out["HOME"] = str(home)
    return out


def check_writable(path: Path) -> None:
    """Create path if needed and make sure the current user can write in it; otherwise raise
    PermissionError naming the path and the uid (the launcher turns it into one log line)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not os.access(path, os.W_OK):
        raise PermissionError(f"{path} is not writable by uid {os.geteuid()}")
