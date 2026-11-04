"""Keep yt-dlp current without rebuilding the image.

pip installs the newest yt-dlp into a staging directory next to data/lib. The copy is
import-tested in a subprocess; a broken one is discarded. If it is a different version
from the one this process imported, the app drains the workers, swaps the staged copy
into data/lib (on PYTHONPATH in the image) and exits; Docker's restart policy brings it
back on the new code. The live process never has the files it imported from replaced
under it, and app.launcher repairs data/lib before start-up, so a bad copy cannot
crash-loop the container.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from .config import Settings
from .jobs import DOWNLOADING, JobStore
from .libdir import staging_dir, swap_in, verify_import

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


def pip_command(python: str, target: Path) -> list[str]:
    return [python, "-m", "pip", "install", "--upgrade", "--no-deps", "--no-cache-dir",
            "--disable-pip-version-check", "--quiet", "--target", str(target), PACKAGE]


def lib_on_path(lib_dir: Path, path: Optional[list] = None) -> bool:
    entries = sys.path if path is None else path
    return any(entry and Path(entry).resolve() == lib_dir.resolve() for entry in entries)


@dataclass
class UpdateStatus:
    running: str
    installed: Optional[str]        # version in data/lib, or the staged one awaiting the restart
    restart_pending: bool
    last_checked: Optional[str]
    error: Optional[str]
    note: Optional[str] = None      # informational, not a failure

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
        self._lock = threading.Lock()          # one pip run at a time
        self._restart_task: Optional[asyncio.Task] = None
        self.status = UpdateStatus(running_version(), None, False, None, None)

    @property
    def staging(self) -> Path:
        return staging_dir(self.settings.lib_dir)

    # -- inspection ---------------------------------------------------------

    def check(self) -> UpdateStatus:
        """Recompute installed/running from disk. Never sets restart_pending: start-up is
        not allowed to trigger a restart."""
        installed = installed_version(self.settings.lib_dir)
        running = running_version()
        self.status.running = running
        self.status.installed = installed
        self.status.note = None
        if installed is not None and installed != running and not self.status.restart_pending:
            self.status.note = (f"yt-dlp {installed} in {self.settings.lib_dir} is not the version in use "
                                f"({running}); is {self.settings.lib_dir} on PYTHONPATH?")
        return self.status

    # -- update -------------------------------------------------------------

    def update(self) -> UpdateStatus:
        """Blocking: pip into the staging dir, verify it imports, then decide whether a
        restart is needed. Never raises; data/lib is never touched here."""
        with self._lock:
            self.status.last_checked = datetime.now(timezone.utc).isoformat()
            self.status.error = None
            shutil.rmtree(self.staging, ignore_errors=True)
            try:
                self._run(pip_command(self._python, self.staging),
                          check=True, capture_output=True, text=True, timeout=PIP_TIMEOUT)
            except subprocess.CalledProcessError as exc:
                return self._fail(f"pip failed: {((exc.stderr or exc.output or '').strip() or exc)}")
            except subprocess.TimeoutExpired:
                return self._fail(f"pip timed out after {PIP_TIMEOUT}s")
            except OSError as exc:
                return self._fail(f"could not run pip: {exc}")

            staged = installed_version(self.staging)
            verified = verify_import(self.staging, python=self._python, run=self._run)
            if staged is None or verified != staged:
                return self._fail(f"downloaded yt-dlp does not import cleanly "
                                  f"(version file {staged!r}, import reported {verified!r}); discarded")

            self.status.running = running_version()
            if staged == self.status.running:
                shutil.rmtree(self.staging, ignore_errors=True)   # nothing new; keep data/lib as is
                self.status.installed = installed_version(self.settings.lib_dir) or staged
                self.status.restart_pending = False
            elif not lib_on_path(self.settings.lib_dir):
                shutil.rmtree(self.staging, ignore_errors=True)
                self.status.installed = installed_version(self.settings.lib_dir)
                self.status.restart_pending = False
                self.status.error = (f"yt-dlp {staged} downloaded, but {self.settings.lib_dir} is not on "
                                     f"PYTHONPATH so a restart would not load it; discarded")
            else:
                self.status.installed = staged
                self.status.restart_pending = True
                log.info("yt-dlp %s staged (running %s); restart pending", staged, self.status.running)
            return self.status

    def _fail(self, message: str) -> UpdateStatus:
        shutil.rmtree(self.staging, ignore_errors=True)
        self.status.error = message[-300:]
        self.status.restart_pending = False
        log.warning("yt-dlp update: %s", self.status.error)
        return self.status

    # -- restart ------------------------------------------------------------

    def schedule_restart(self) -> Optional[asyncio.Task]:
        """Start (once) the task that drains the workers, swaps the staged copy in and exits."""
        if not self.status.restart_pending:
            return None
        if self._restart_task is not None and not self._restart_task.done():
            return self._restart_task
        self._restart_task = asyncio.create_task(self.restart_when_idle(), name="yt-dlp-restart")
        self._restart_task.add_done_callback(self._log_task_result)
        return self._restart_task

    @staticmethod
    def _log_task_result(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("restart task failed: %r", task.exception())

    async def restart_when_idle(self, poll: float = 5.0) -> None:
        """If an update is pending: stop starting new jobs, wait for running downloads,
        swap the staged copy into data/lib, exit."""
        if not self.status.restart_pending:
            return
        self.pool.hold()
        while any(job.status == DOWNLOADING for job in self.store.list()):
            await asyncio.sleep(poll)
        try:
            if not self.staging.exists():
                raise FileNotFoundError(f"{self.staging} vanished")
            swap_in(self.staging, self.settings.lib_dir)
        except OSError as exc:
            self.status.error = f"could not activate yt-dlp {self.status.installed}: {exc}"[-300:]
            self.status.restart_pending = False
            log.warning(self.status.error)
            self.pool.release()
            return
        log.info("restarting to load yt-dlp %s", self.status.installed)
        self._exit(0)
