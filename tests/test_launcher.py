import subprocess
from pathlib import Path

from app.launcher import UVICORN, prepare_lib, prepare_lib_safely
from app.libdir import mark_verified, staging_dir, swap_in, quarantine, verified_version, verify_import


def _write(lib: Path, version: str, broken: bool = False) -> None:
    pkg = lib / "yt_dlp"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "version.py").write_text(f"__version__ = '{version}'\n")
    (pkg / "__init__.py").write_text("raise ImportError('broken')\n" if broken else "")


def _fake_run(cmd, **kwargs):
    """Pretend to import yt-dlp from the PYTHONPATH dir: fails if its __init__ raises."""
    target = Path(kwargs["env"]["PYTHONPATH"])
    init = target / "yt_dlp" / "__init__.py"
    if not init.exists() or "raise" in init.read_text():
        raise subprocess.CalledProcessError(1, cmd, stderr="ImportError")
    import re
    v = re.search(r"'([^']+)'", (target / "yt_dlp" / "version.py").read_text()).group(1)
    return subprocess.CompletedProcess(cmd, 0, stdout=v + "\n", stderr="")


def test_verify_import_reports_version_or_none(tmp_path):
    lib = tmp_path / "lib"
    assert verify_import(lib, run=_fake_run) is None
    _write(lib, "1.2.3")
    assert verify_import(lib, run=_fake_run) == "1.2.3"
    _write(lib, "1.2.3", broken=True)
    assert verify_import(lib, run=_fake_run) is None


def test_prepare_lib_activates_a_verified_staged_copy(tmp_path):
    lib = tmp_path / "lib"
    _write(lib, "1.0")
    _write(staging_dir(lib), "2.0")
    mark_verified(staging_dir(lib), "2.0")
    prepare_lib(lib, run=_fake_run)
    assert (lib / "yt_dlp" / "version.py").read_text() == "__version__ = '2.0'\n"
    assert not staging_dir(lib).exists() and not (tmp_path / "lib.old").exists()


def test_prepare_lib_discards_a_broken_staged_copy(tmp_path):
    lib = tmp_path / "lib"
    _write(lib, "1.0")
    _write(staging_dir(lib), "2.0", broken=True)
    prepare_lib(lib, run=_fake_run)
    assert "1.0" in (lib / "yt_dlp" / "version.py").read_text()
    assert not staging_dir(lib).exists()


def test_prepare_lib_discards_an_unverified_staged_copy_even_if_it_imports(tmp_path):
    """A half-copied install (pip interrupted) can still import; without the marker it is junk."""
    lib = tmp_path / "lib"
    _write(lib, "1.0")
    _write(staging_dir(lib), "2.0")          # imports fine, but no .verified marker
    prepare_lib(lib, run=_fake_run)
    assert "1.0" in (lib / "yt_dlp" / "version.py").read_text()
    assert not staging_dir(lib).exists()


def test_prepare_lib_safely_never_raises(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    _write(lib, "1.0", broken=True)
    import app.launcher as launcher

    def exploding_quarantine(path):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(launcher, "quarantine", exploding_quarantine)
    prepare_lib_safely(lib, run=_fake_run)   # logs, does not raise
    assert lib.exists()


def test_verified_marker_round_trip(tmp_path):
    st = tmp_path / "lib.staging"
    st.mkdir()
    assert verified_version(st) is None
    mark_verified(st, "2.0")
    assert verified_version(st) == "2.0"


def test_prepare_lib_quarantines_a_broken_live_copy(tmp_path):
    lib = tmp_path / "lib"
    _write(lib, "1.0", broken=True)
    prepare_lib(lib, run=_fake_run)
    assert not lib.exists()
    assert (tmp_path / "lib.broken" / "yt_dlp" / "version.py").exists()


def test_prepare_lib_leaves_good_live_copy_and_tolerates_nothing(tmp_path):
    lib = tmp_path / "lib"
    prepare_lib(lib, run=_fake_run)          # nothing there: no-op
    assert not lib.exists()
    _write(lib, "1.0")
    prepare_lib(lib, run=_fake_run)
    assert "1.0" in (lib / "yt_dlp" / "version.py").read_text()


def test_swap_in_replaces_existing_and_quarantine_moves_aside(tmp_path):
    lib = tmp_path / "lib"; staging = staging_dir(lib)
    _write(lib, "1.0"); _write(staging, "2.0")
    (tmp_path / "lib.old").mkdir(); (tmp_path / "lib.old" / "junk").write_text("x")
    swap_in(staging, lib)
    assert "2.0" in (lib / "yt_dlp" / "version.py").read_text()
    assert not staging.exists() and not (tmp_path / "lib.old").exists()
    broken = quarantine(lib)
    assert broken == tmp_path / "lib.broken" and not lib.exists() and broken.exists()


def test_uvicorn_command_matches_the_old_dockerfile_cmd():
    assert UVICORN == ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
