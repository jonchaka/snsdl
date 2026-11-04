"""Helpers for the on-volume yt-dlp directory (data/lib). Standard library only: the
launcher runs these before anything else is imported."""
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

VERIFY_TIMEOUT = 60
_VERIFY_CODE = "import yt_dlp.version as v; print(v.__version__)"


def verify_command(python: str) -> list[str]:
    return [python, "-c", _VERIFY_CODE]


def verify_import(lib_dir: Path, *, python: str = sys.executable,
                  run: Callable = subprocess.run, env: Optional[dict] = None) -> Optional[str]:
    """Import yt-dlp from lib_dir in a fresh interpreter; return its version or None."""
    import os
    child_env = {**(os.environ if env is None else env), "PYTHONPATH": str(lib_dir)}
    try:
        proc = run(verify_command(python), check=True, capture_output=True, text=True,
                   timeout=VERIFY_TIMEOUT, env=child_env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("yt-dlp in %s does not import: %s", lib_dir, exc)
        return None
    out = (proc.stdout or "").strip()
    return out or None


def staging_dir(lib_dir: Path) -> Path:
    return lib_dir.with_name(lib_dir.name + ".staging")


def swap_in(staging: Path, lib_dir: Path) -> None:
    """Replace lib_dir with staging. Not atomic, but the window is two renames."""
    old = lib_dir.with_name(lib_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if lib_dir.exists():
        lib_dir.rename(old)
    staging.rename(lib_dir)
    shutil.rmtree(old, ignore_errors=True)


def quarantine(lib_dir: Path) -> Path:
    """Move a broken lib_dir aside so the image's own yt-dlp is used."""
    broken = lib_dir.with_name(lib_dir.name + ".broken")
    shutil.rmtree(broken, ignore_errors=True)
    lib_dir.rename(broken)
    return broken
