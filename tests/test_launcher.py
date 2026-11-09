import subprocess
from pathlib import Path

import pytest

from app import launcher, tls
from app.launcher import UVICORN, exec_env, prepare_lib, prepare_lib_safely
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
    assert prepare_lib_safely(lib, run=_fake_run) is False   # logs, does not raise
    assert lib.exists()
    _write(lib, "1.0")
    assert prepare_lib_safely(lib, run=_fake_run) is True
    assert prepare_lib_safely(tmp_path / "absent", run=_fake_run) is True


def test_exec_env_drops_pythonpath_only_when_lib_unusable():
    env = {"PYTHONPATH": "/data/lib", "DATA_DIR": "/data"}
    assert exec_env(True, env) == env
    assert exec_env(False, env) == {"DATA_DIR": "/data"}


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
    lib = tmp_path / "lib"
    staging = staging_dir(lib)
    _write(lib, "1.0")
    _write(staging, "2.0")
    (tmp_path / "lib.old").mkdir()
    (tmp_path / "lib.old" / "junk").write_text("x")
    swap_in(staging, lib)
    assert "2.0" in (lib / "yt_dlp" / "version.py").read_text()
    assert not staging.exists() and not (tmp_path / "lib.old").exists()
    broken = quarantine(lib)
    assert broken == tmp_path / "lib.broken" and not lib.exists() and broken.exists()


def test_uvicorn_command_matches_the_old_dockerfile_cmd():
    assert UVICORN == ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080",
                       "--timeout-graceful-shutdown", "3"]


# --- round 8: HTTPS ---------------------------------------------------------------------------

def test_tls_settings_defaults():
    assert launcher.tls_settings({}) == (False, [], Path("/data/config/tls"))


def test_tls_settings_reads_https_hosts_and_dir(tmp_path):
    env = {"HTTPS": "yes", "TLS_HOSTS": "NAS.local, 192.168.1.10", "DATA_DIR": str(tmp_path)}
    assert launcher.tls_settings(env) == (True, ["nas.local", "192.168.1.10"], tmp_path / "config" / "tls")


def test_tls_settings_ignores_unrelated_bad_variables():
    env = {"HTTPS": "1", "MAX_CONCURRENT": "abc", "RATE_LIMIT": "fast", "ACCESS_PIN": "x"}
    assert launcher.tls_settings(env)[0] is True


@pytest.mark.parametrize("env", [{"HTTPS": "maybe"}, {"TLS_HOSTS": "bad_host"}])
def test_tls_settings_rejects_its_own_bad_variables(env):
    with pytest.raises(ValueError):
        launcher.tls_settings(env)


def test_uvicorn_args_plain_http():
    assert launcher.uvicorn_args({}) == UVICORN
    assert launcher.uvicorn_args({"HTTPS": "0"}) is not UVICORN


def test_uvicorn_args_https(tmp_path):
    args = launcher.uvicorn_args({"HTTPS": "1", "DATA_DIR": str(tmp_path)})
    tls_dir = tmp_path / "config" / "tls"
    assert args == UVICORN + ["--ssl-certfile", str(tls_dir / "server.pem"),
                              "--ssl-keyfile", str(tls_dir / "server.key")]
    assert "8080" in args   # same port for both schemes
    assert args[args.index("--timeout-graceful-shutdown") + 1] == "3"   # both schemes


class _Exec(Exception):
    pass


@pytest.fixture
def no_exec(monkeypatch):
    calls = []

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise _Exec()

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(launcher, "prepare_lib_safely", lambda lib: True)
    monkeypatch.setattr(launcher, "prepare_user", lambda env, data_dir, keep_dir: dict(env))
    return calls


def test_main_plain_http_does_not_touch_certs(no_exec, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path)})
    monkeypatch.setattr(tls, "ensure_certs", lambda *a, **k: pytest.fail("ensure_certs called"))
    with pytest.raises(_Exec):
        launcher.main()
    assert no_exec[0][1] == UVICORN
    assert not (tmp_path / "config" / "tls").exists()


def test_main_https_generates_certs_then_execs(no_exec, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.os, "environ",
                        {"DATA_DIR": str(tmp_path), "HTTPS": "1", "TLS_HOSTS": "nas.local"})
    seen = []
    monkeypatch.setattr(tls, "ensure_certs", lambda d, hosts: seen.append((d, hosts)))
    with pytest.raises(_Exec):
        launcher.main()
    tls_dir = tmp_path / "config" / "tls"
    assert seen == [(tls_dir, tls.default_hosts(["nas.local"]))]
    assert "nas.local" in seen[0][1] and "localhost" in seen[0][1]
    assert no_exec[0][1][-4:] == ["--ssl-certfile", str(tls_dir / "server.pem"),
                                  "--ssl-keyfile", str(tls_dir / "server.key")]


def test_main_exits_1_when_certs_fail(no_exec, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), "HTTPS": "1"})

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(tls, "ensure_certs", boom)
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 1
    assert no_exec == []   # never falls back to plain HTTP
    assert "could not prepare the TLS certificate" in caplog.text


@pytest.mark.parametrize("env", [{"HTTPS": "1", "TLS_HOSTS": "bad_host"}, {"HTTPS": "maybe"}])
def test_main_exits_1_on_bad_tls_settings(no_exec, monkeypatch, tmp_path, caplog, env):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), **env})
    monkeypatch.setattr(tls, "ensure_certs", lambda *a, **k: pytest.fail("ensure_certs called"))
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 1
    assert no_exec == []
    assert "invalid HTTPS/TLS_HOSTS setting: " in caplog.text
    assert "could not prepare the TLS certificate" not in caplog.text


def test_uvicorn_args_uses_pre_parsed_settings(tmp_path):
    parsed = (True, [], tmp_path)
    args = launcher.uvicorn_args({"HTTPS": "maybe"}, parsed)   # env not parsed again
    assert args[-4:] == ["--ssl-certfile", str(tmp_path / "server.pem"),
                         "--ssl-keyfile", str(tmp_path / "server.key")]
    assert launcher.uvicorn_args({"HTTPS": "1"}, (False, [], tmp_path)) == UVICORN


def test_main_parses_tls_settings_once(no_exec, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), "HTTPS": "1"})
    monkeypatch.setattr(tls, "ensure_certs", lambda *a, **k: None)
    real, calls = launcher.tls_settings, []
    monkeypatch.setattr(launcher, "tls_settings", lambda env: calls.append(1) or real(env))
    with pytest.raises(_Exec):
        launcher.main()
    assert len(calls) == 1


def test_main_https_real_certs(no_exec, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), "HTTPS": "1"})
    with pytest.raises(_Exec):
        launcher.main()
    tls_dir = tmp_path / "config" / "tls"
    for name in (tls.CA_CERT, tls.CA_KEY, tls.SERVER_CERT, tls.SERVER_KEY):
        assert (tls_dir / name).is_file()


def test_main_prepares_the_user_first_and_execs_with_its_env(no_exec, monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), "KEEP_DIR": str(tmp_path / "k")})
    monkeypatch.setattr(
        launcher, "prepare_user",
        lambda env, data_dir, keep_dir: order.append(("user", data_dir, keep_dir)) or {**env, "HOME": "/h"})
    monkeypatch.setattr(launcher, "prepare_lib_safely", lambda lib: order.append(("lib", lib)) or True)
    with pytest.raises(_Exec):
        launcher.main()
    assert order == [("user", tmp_path, tmp_path / "k"), ("lib", tmp_path / "lib")]
    assert no_exec[0][2]["HOME"] == "/h"


def test_main_passes_none_for_an_empty_keep_dir(no_exec, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), "KEEP_DIR": ""})
    monkeypatch.setattr(launcher, "prepare_user", lambda env, d, k: seen.append(k) or dict(env))
    with pytest.raises(_Exec):
        launcher.main()
    assert seen == [None]


def test_main_exits_1_with_one_line_when_data_dir_is_not_writable(no_exec, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path)})
    monkeypatch.setattr(launcher, "check_writable",
                        lambda p: (_ for _ in ()).throw(PermissionError(f"{p} is not writable by uid 1000")))
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 1
    assert f"{tmp_path} is not writable by uid 1000. The volume mounted at {tmp_path} must be writable" in caplog.text
    assert "set PUID/PGID to the owner of that folder (id -u / id -g)" in caplog.text
    assert "Traceback" not in caplog.text and no_exec == []


def test_main_exits_1_on_bad_puid(no_exec, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path), "PUID": "me"})
    monkeypatch.setattr(launcher, "prepare_user", launcher.dropprivs.prepare_user)
    monkeypatch.setattr(launcher.dropprivs.os, "geteuid", lambda: 0)
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 1
    assert "invalid PUID/PGID: PUID must be a whole number (got 'me')" in caplog.text


def test_main_exits_1_when_the_user_switch_fails(no_exec, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(launcher.os, "environ", {"DATA_DIR": str(tmp_path)})
    monkeypatch.setattr(launcher, "prepare_user",
                        lambda env, d, k: (_ for _ in ()).throw(RuntimeError("cannot switch to uid 1234, gid 1234")))
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 1
    assert "cannot switch to uid 1234, gid 1234" in caplog.text and "must be writable" not in caplog.text
