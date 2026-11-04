"""Small persisted app-wide switches (currently just the global queue pause)."""
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self.queue_paused: bool = False

    def load(self) -> None:
        """Read the file; anything missing or malformed falls back to defaults."""
        self.queue_paused = False
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("state file %s unreadable (%s); using defaults", self._path, exc)
            return
        if isinstance(data, dict):
            self.queue_paused = bool(data.get("queue_paused", False))

    def set_queue_paused(self, value: bool) -> None:
        with self._lock:
            self.queue_paused = bool(value)
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"queue_paused": self.queue_paused}))
        tmp.replace(self._path)
