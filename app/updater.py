"""Keep yt-dlp current without rebuilding the image.

pip installs the newest yt-dlp into settings.lib_dir (on PYTHONPATH in the image, so it
shadows the baked-in copy). If the installed version differs from the one this process
imported, the app drains the workers and exits; Docker's restart policy brings it back
on the new code.
"""
import asyncio
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from .config import Settings
from .jobs import DOWNLOADING, JobStore

log = logging.getLogger(__name__)

PACKAGE = "yt-dlp"
PIP_TIMEOUT = 600
_VERSION_RE = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.M)


def running_version() -> str:
    return yt_dlp.version.__version__


def installed_version(lib_dir: Path) -> Optional[str]:
    """Version of the yt-dlp in lib_dir, read without importing it; None if absent/unparsable."""
    try:
        text = (lib_dir / "yt_dlp" / "version.py").read_text()
    except OSError:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def pip_command(python: str, lib_dir: Path) -> list[str]:
    return [python, "-m", "pip", "install", "--upgrade", "--no-deps", "--no-cache-dir",
            "--disable-pip-version-check", "--quiet", "--target", str(lib_dir), PACKAGE]


@dataclass
class UpdateStatus:
    running: str
    installed: Optional[str]
    restart_pending: bool
    last_checked: Optional[str]
    error: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


class Updater:
    def __init__(self, settings: Settings, pool, store: JobStore, *,
                 run: Callable = subprocess.run, exit_fn: Callable[[int], None] = os._exit,
                 python: str = sys.executable):
        self.settings = settings
        self.pool = pool
        self.store = store
        self._run = run
        self._exit = exit_fn
        self._python = python
        self.status = UpdateStatus(running_version(), None, False, None, None)

    def check(self) -> UpdateStatus:
        """Recompute installed/running/pending from disk. No network."""
        installed = installed_version(self.settings.lib_dir)
        running = running_version()
        self.status.running = running
        self.status.installed = installed
        self.status.restart_pending = installed is not None and installed != running
        return self.status

    def update(self) -> UpdateStatus:
        """Blocking: run pip into lib_dir, then check(). Never raises; failures land in .error."""
        cmd = pip_command(self._python, self.settings.lib_dir)
        self.status.last_checked = datetime.now(timezone.utc).isoformat()
        try:
            self._run(cmd, check=True, capture_output=True, text=True, timeout=PIP_TIMEOUT)
            self.status.error = None
        except subprocess.CalledProcessError as exc:
            self.status.error = f"pip failed: {((exc.stderr or exc.output or '').strip() or exc)}"[-300:]
            log.warning("yt-dlp update failed: %s", self.status.error)
        except subprocess.TimeoutExpired:
            self.status.error = f"pip timed out after {PIP_TIMEOUT}s"
            log.warning(self.status.error)
        except OSError as exc:
            self.status.error = f"could not run pip: {exc}"[-300:]
            log.warning(self.status.error)
        self.check()
        if self.status.restart_pending:
            log.info("yt-dlp %s installed (running %s); restart pending",
                     self.status.installed, self.status.running)
        return self.status

    async def restart_when_idle(self, poll: float = 5.0) -> None:
        """If an update is pending: stop starting new jobs, wait for running downloads, exit."""
        if not self.status.restart_pending:
            return
        self.pool.hold()
        while any(job.status == DOWNLOADING for job in self.store.list()):
            await asyncio.sleep(poll)
        log.info("restarting to load yt-dlp %s", self.status.installed)
        self._exit(0)
