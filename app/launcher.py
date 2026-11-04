"""Container entrypoint: make sure data/lib is usable, then exec uvicorn.

Runs before anything imports yt-dlp, so a broken on-volume copy can never crash-loop
the container: a verified staged update is swapped in, a copy that fails to import is
moved aside and the version baked into the image is used instead.
"""
import logging
import os
import shutil
import sys
from pathlib import Path

from .libdir import quarantine, staging_dir, swap_in, verified_version, verify_import

log = logging.getLogger("app.launcher")

UVICORN = ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]


def prepare_lib(lib_dir: Path, **verify_kwargs) -> None:
    staging = staging_dir(lib_dir)
    if staging.exists():
        if verified_version(staging) and verify_import(staging, **verify_kwargs):
            log.info("activating staged yt-dlp from %s", staging)
            swap_in(staging, lib_dir)
        else:
            log.warning("discarding staged yt-dlp in %s: not verified or does not import", staging)
            shutil.rmtree(staging, ignore_errors=True)
    if lib_dir.exists() and verify_import(lib_dir, **verify_kwargs) is None:
        broken = quarantine(lib_dir)
        log.warning("yt-dlp in %s does not import; moved to %s, using the image copy", lib_dir, broken)


def prepare_lib_safely(lib_dir: Path, **verify_kwargs) -> None:
    """Whatever happens to data/lib, the app must still start (on the image's yt-dlp)."""
    try:
        prepare_lib(lib_dir, **verify_kwargs)
    except Exception:  # noqa: BLE001 - e.g. a read-only volume; log and carry on
        log.exception("could not prepare %s; starting with whatever is importable", lib_dir)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prepare_lib_safely(Path(os.environ.get("DATA_DIR", "/data")) / "lib")
    os.execvp(UVICORN[0], UVICORN)


if __name__ == "__main__":
    main()
