"""SIGTERM with an open /api/events stream: uvicorn's graceful-shutdown timeout ends the stream,
so the lifespan shutdown (pool.stop(), which stops recordings gracefully) still runs."""
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_with_an_open_event_stream_runs_the_lifespan_shutdown(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("PIN", "HTTPS")}
    env.update(DATA_DIR=str(tmp_path / "data"), PYTHONPATH=str(ROOT), UPDATE_CRON="",
               SUBSCRIPTION_CRON="")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
         "--port", str(port), "--timeout-graceful-shutdown", "3"],
        cwd=str(tmp_path), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 5
        while True:
            if proc.poll() is not None:
                pytest.skip(f"uvicorn could not start: {proc.stderr.read()[-500:]}")
            try:
                urllib.request.urlopen(f"{base}/api/auth", timeout=1).read()
                break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.skip("uvicorn did not answer in time")
                time.sleep(0.1)

        got_line = threading.Event()

        def listen():
            try:
                with urllib.request.urlopen(f"{base}/api/events", timeout=15) as resp:
                    resp.readline()
                    got_line.set()
                    while resp.read(1):   # keep the stream open until the server ends it
                        pass
            except Exception:   # noqa: BLE001 - the server closes the stream mid-read (IncompleteRead, OSError)
                pass

        threading.Thread(target=listen, daemon=True).start()
        assert got_line.wait(5), "the event stream sent nothing"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("uvicorn did not exit within 10 s of SIGTERM with an open event stream")
        assert time.monotonic() - started < 10
        assert "Application shutdown complete." in err
        # uvicorn re-raises the signal after a clean shutdown on newer versions
        assert proc.returncode in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
