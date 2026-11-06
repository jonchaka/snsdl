"""Container entrypoint: make sure data/lib is usable, prepare the TLS certificate when
HTTPS is on, then exec uvicorn.

Runs before anything imports yt-dlp, so a broken on-volume copy can never crash-loop
the container: a verified staged update is swapped in, a copy that fails to import is
moved aside and the version baked into the image is used instead.
"""
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from . import tls
from .config import _parse_bool, _parse_hosts
from .libdir import quarantine, staging_dir, swap_in, verified_version, verify_import

log = logging.getLogger("app.launcher")

# --timeout-graceful-shutdown: /api/events never ends on its own, so without it uvicorn would
# wait for that connection forever on SIGTERM and never run the lifespan shutdown (which
# stops running recordings gracefully) before Docker's stop_grace_period kill.
UVICORN = ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080",
           "--timeout-graceful-shutdown", "3"]


def prepare_lib(lib_dir: Path, **verify_kwargs) -> bool:
    """Returns True when lib_dir is safe to have on PYTHONPATH (absent, or imports)."""
    staging = staging_dir(lib_dir)
    if staging.exists():
        if verified_version(staging) and verify_import(staging, **verify_kwargs):
            log.info("activating staged yt-dlp from %s", staging)
            swap_in(staging, lib_dir)
        else:
            log.warning("discarding staged yt-dlp in %s: not verified or does not import", staging)
            shutil.rmtree(staging, ignore_errors=True)
    if lib_dir.exists() and verify_import(lib_dir, **verify_kwargs) is None:
        try:
            broken = quarantine(lib_dir)
        except OSError as exc:
            log.error("yt-dlp in %s does not import and could not be moved aside (%s)", lib_dir, exc)
            return False
        log.warning("yt-dlp in %s does not import; moved to %s, using the image copy", lib_dir, broken)
    return True


def prepare_lib_safely(lib_dir: Path, **verify_kwargs) -> bool:
    """Whatever happens to data/lib, the app must still start (on the image's yt-dlp)."""
    try:
        return prepare_lib(lib_dir, **verify_kwargs)
    except Exception:  # noqa: BLE001 - e.g. a read-only volume; log and carry on
        log.exception("could not prepare %s", lib_dir)
        return False


def exec_env(lib_usable: bool, env: dict) -> dict:
    """If data/lib is broken and could not be moved aside, start without it on the path."""
    if lib_usable:
        return dict(env)
    out = {k: v for k, v in env.items() if k != "PYTHONPATH"}
    log.warning("starting with PYTHONPATH unset so the image's yt-dlp is used")
    return out


def tls_settings(env) -> tuple[bool, list[str], Path]:
    """(https, tls_hosts, tls_dir) parsed like Settings does, but only these: a bad unrelated
    variable must fail in the app (with its own message), not here."""
    return (_parse_bool("HTTPS", env.get("HTTPS", "0")),
            _parse_hosts("TLS_HOSTS", env.get("TLS_HOSTS", "")),
            Path(env.get("DATA_DIR", "/data")) / "config" / "tls")


def uvicorn_args(env, parsed: Optional[tuple[bool, list[str], Path]] = None) -> list[str]:
    """The uvicorn command; with HTTPS on it also serves TLS from tls_dir (same port).
    `parsed` is tls_settings(env) when the caller already has it."""
    https, _, tls_dir = parsed if parsed is not None else tls_settings(env)
    if not https:
        return list(UVICORN)
    return UVICORN + ["--ssl-certfile", str(tls_dir / tls.SERVER_CERT),
                      "--ssl-keyfile", str(tls_dir / tls.SERVER_KEY)]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    usable = prepare_lib_safely(Path(os.environ.get("DATA_DIR", "/data")) / "lib")
    try:
        parsed = tls_settings(os.environ)
    except ValueError as exc:
        log.error("invalid HTTPS/TLS_HOSTS setting: %s", exc)
        sys.exit(1)
    https, hosts, tls_dir = parsed
    try:
        if https:
            tls.ensure_certs(tls_dir, tls.default_hosts(hosts))
        args = uvicorn_args(os.environ, parsed)
    except Exception:  # noqa: BLE001 - never fall back to plain HTTP silently
        log.exception("could not prepare the TLS certificate")
        sys.exit(1)
    os.execvpe(args[0], args, exec_env(usable, os.environ))


if __name__ == "__main__":
    main()
