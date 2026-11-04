"""Keep yt-dlp current without rebuilding the image.

pip installs the newest yt-dlp into settings.lib_dir (on PYTHONPATH in the image, so it
shadows the baked-in copy). After a verified install whose version differs from the one
this process imported, the app drains the workers and exits; Docker's restart policy
brings it back on the new code.

Safety rails: the install is verified by importing it in a subprocess before any restart
is considered; a broken install is discarded; a restart is only requested when lib_dir is
actually on sys.path (otherwise exiting would just reload the same version, forever); and
startup never triggers a restart by itself.
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

log = logging.getLogger(__name__)

PACKAGE = "yt-dlp"
PIP_TIMEOUT = 600
VERIFY_TIMEOUT = 60
_VERSION_RE = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.M)
_VERIFY_CODE = "import yt_dlp.version as v; print(v.__version__)"


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


def verify_command(python: str) -> list[str]:
    return [python, "-c", _VERIFY_CODE]


def lib_on_path(lib_dir: Path, path: Optional[list] = None) -> bool:
    entries = sys.path if path is None else path
    return any(entry and Path(entry).resolve() == lib_dir.resolve() for entry in entries)


def prune_stale_dist_info(lib_dir: Path, keep_version: Optional[str]) -> None:
    """pip --target --upgrade leaves old yt_dlp-<ver>.dist-info folders behind; drop them."""
    for d in lib_dir.glob("yt_dlp-*.dist-info"):
        if keep_version is None or d.name != f"yt_dlp-{keep_version}.dist-info":
            shutil.rmtree(d, ignore_errors=True)


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
        self._lock = threading.Lock()          # one pip run at a time
        self._restart_task: Optional[asyncio.Task] = None
        self.status = UpdateStatus(running_version(), None, False, None, None)

    # -- inspection ---------------------------------------------------------

    def check(self) -> UpdateStatus:
        """Recompute installed/running from disk. Never sets restart_pending (startup is
        not allowed to trigger a restart); flags the case where lib_dir holds a version
        this process is not using."""
        installed = installed_version(self.settings.lib_dir)
        running = running_version()
        self.status.running = running
        self.status.installed = installed
        if installed is not None and installed != running and not self.status.restart_pending:
            self.status.error = (f"yt-dlp {installed} in {self.settings.lib_dir} is not the version "
                                 f"in use ({running}); is {self.settings.lib_dir} on PYTHONPATH?")
        return self.status

    # -- update -------------------------------------------------------------

    def update(self) -> UpdateStatus:
        """Blocking: pip into lib_dir, verify it imports, then decide whether a restart is
        needed. Never raises; failures land in .error and never leave a broken lib_dir."""
        with self._lock:
            self.status.last_checked = datetime.now(timezone.utc).isoformat()
            self.status.error = None
            try:
                self._run(pip_command(self._python, self.settings.lib_dir),
                          check=True, capture_output=True, text=True, timeout=PIP_TIMEOUT)
            except subprocess.CalledProcessError as exc:
                return self._fail(f"pip failed: {((exc.stderr or exc.output or '').strip() or exc)}")
            except subprocess.TimeoutExpired:
                return self._fail(f"pip timed out after {PIP_TIMEOUT}s", discard=True)
            except OSError as exc:
                return self._fail(f"could not run pip: {exc}")

            installed = installed_version(self.settings.lib_dir)
            verified = self._verify()
            if installed is None or verified != installed:
                return self._fail(f"installed yt-dlp does not import cleanly "
                                  f"(version file {installed!r}, import reported {verified!r}); discarded",
                                  discard=True)
            prune_stale_dist_info(self.settings.lib_dir, installed)
            self.status.installed = installed
            self.status.running = running_version()
            if installed == self.status.running:
                self.status.restart_pending = False
            elif not lib_on_path(self.settings.lib_dir):
                self.status.restart_pending = False
                self.status.error = (f"yt-dlp {installed} installed in {self.settings.lib_dir}, but that "
                                     f"directory is not on PYTHONPATH so a restart would not load it")
            else:
                self.status.restart_pending = True
                log.info("yt-dlp %s installed (running %s); restart pending", installed, self.status.running)
            return self.status

    def _verify(self) -> Optional[str]:
        """Import the installed copy in a fresh interpreter with lib_dir first on the path."""
        env = {**os.environ, "PYTHONPATH": str(self.settings.lib_dir)}
        try:
            proc = self._run(verify_command(self._python), check=True, capture_output=True, text=True,
                             timeout=VERIFY_TIMEOUT, env=env)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("yt-dlp verify failed: %s", exc)
            return None
        out = (proc.stdout or "").strip()
        return out or None

    def _fail(self, message: str, discard: bool = False) -> UpdateStatus:
        self.status.error = message[-300:]
        self.status.restart_pending = False
        log.warning("yt-dlp update: %s", self.status.error)
        if discard:
            shutil.rmtree(self.settings.lib_dir, ignore_errors=True)
            self.status.installed = None
        return self.status

    # -- restart ------------------------------------------------------------

    def schedule_restart(self) -> Optional[asyncio.Task]:
        """Start (once) the task that drains the workers and exits. Call from the event loop."""
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
        """If an update is pending: stop starting new jobs, wait for running downloads, exit."""
        if not self.status.restart_pending:
            return
        self.pool.hold()
        while any(job.status == DOWNLOADING for job in self.store.list()):
            await asyncio.sleep(poll)
        log.info("restarting to load yt-dlp %s", self.status.installed)
        self._exit(0)
