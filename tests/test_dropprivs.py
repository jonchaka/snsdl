import os
from pathlib import Path

import pytest

from app import dropprivs
from app.dropprivs import check_writable, own_tree, parse_ids, prepare_user


def test_parse_ids_defaults_and_pgid_follows_puid():
    assert parse_ids({}) == (1000, 1000)
    assert parse_ids({"PUID": "1026"}) == (1026, 1026)
    assert parse_ids({"PUID": "1026", "PGID": "100"}) == (1026, 100)
    assert parse_ids({"PUID": " 0 ", "PGID": "0"}) == (0, 0)


@pytest.mark.parametrize("env, name", [
    ({"PUID": "abc"}, "PUID"), ({"PUID": "-1"}, "PUID"), ({"PUID": ""}, "PUID"),
    ({"PGID": "1.5"}, "PGID"), ({"PUID": str(2**31)}, "PUID"),
])
def test_parse_ids_rejects_non_numbers(env, name):
    with pytest.raises(ValueError, match=f"{name} must be a whole number"):
        parse_ids(env)


def _tree(root: Path) -> list[Path]:
    (root / "downloads").mkdir()
    (root / "downloads" / "a.mp4").write_bytes(b"x")
    (root / "config").mkdir()
    (root / "jobs.json").write_text("[]")
    return [root, root / "downloads", root / "downloads" / "a.mp4", root / "config", root / "jobs.json"]


def test_own_tree_changes_nothing_when_already_owned(tmp_path):
    _tree(tmp_path)
    calls = []
    st = tmp_path.lstat()
    assert own_tree(tmp_path, st.st_uid, st.st_gid, chown=lambda *a, **k: calls.append((a, k))) == 0
    assert calls == []


def test_own_tree_chowns_every_entry_that_differs_without_following_symlinks(tmp_path):
    entries = _tree(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    calls = []
    uid, gid = tmp_path.lstat().st_uid + 1, tmp_path.lstat().st_gid + 1

    def fake_chown(p, u, g, follow_symlinks=True):
        calls.append((Path(p), u, g, follow_symlinks))

    changed = own_tree(tmp_path, uid, gid, chown=fake_chown)
    assert changed == len(entries) + 1   # the symlink itself, never its target
    assert {c[0] for c in calls} == set(entries) | {tmp_path / "link"}
    assert all(c[1:] == (uid, gid, False) for c in calls)
    assert outside not in {c[0] for c in calls}


def test_own_tree_propagates_the_first_oserror(tmp_path):
    _tree(tmp_path)

    def boom(p, u, g, follow_symlinks=True):
        raise PermissionError(13, "Operation not permitted", str(p))

    with pytest.raises(PermissionError):
        own_tree(tmp_path, 4321, 4321, chown=boom)


def _fakes():
    log = []
    return log, dict(
        geteuid=lambda: 0,
        setgroups=lambda groups: log.append(("setgroups", list(groups))),
        setgid=lambda g: log.append(("setgid", g)),
        setuid=lambda u: log.append(("setuid", u)),
        chown=lambda p, u, g, follow_symlinks=True: log.append(("chown", Path(p), u, g)),
    )


def test_prepare_user_not_root_changes_nothing(tmp_path):
    log, fakes = _fakes()
    fakes["geteuid"] = lambda: 1000
    env = {"PUID": "1234", "HOME": "/home/appuser"}
    out = prepare_user(env, tmp_path / "data", None, **fakes)
    assert out == env and out is not env
    assert log == [] and not (tmp_path / "data").exists()


def test_prepare_user_as_root_owns_dirs_then_drops_in_order(tmp_path):
    log, fakes = _fakes()
    home = tmp_path / "home"
    home.mkdir()
    keep = tmp_path / "keep"
    keep.mkdir()
    data = tmp_path / "data"           # does not exist yet: created
    env = {"PUID": "1234", "PGID": "100", "HOME": str(home), "OTHER": "x"}
    out = prepare_user(env, data, keep, **fakes)
    assert data.is_dir()
    chowned = [e for e in log if e[0] == "chown"]
    assert {e[1] for e in chowned} == {data, keep, home}
    assert all(e[2:] == (1234, 100) for e in chowned)
    assert [e for e in log if e[0] != "chown"] == [("setgroups", [100]), ("setgid", 100), ("setuid", 1234)]
    assert out == {**env, "HOME": str(home)}


def test_prepare_user_skips_a_missing_keep_dir_and_home(tmp_path):
    log, fakes = _fakes()
    data = tmp_path / "data"
    data.mkdir()
    out = prepare_user({"PUID": "1234", "HOME": str(tmp_path / "nohome")}, data, tmp_path / "nokeep", **fakes)
    assert {e[1] for e in log if e[0] == "chown"} == {data}
    assert not (tmp_path / "nokeep").exists()
    assert out["HOME"] == str(tmp_path / "nohome")


def test_prepare_user_puid_zero_stays_root(tmp_path, caplog):
    log, fakes = _fakes()
    data = tmp_path / "data"
    data.mkdir()
    with caplog.at_level("WARNING", logger="app.launcher"):
        prepare_user({"PUID": "0", "HOME": str(tmp_path)}, data, None, **fakes)
    assert [e for e in log if e[0] != "chown"] == []
    assert "running as root" in caplog.text


def test_prepare_user_default_home_when_env_has_none(tmp_path):
    log, fakes = _fakes()
    out = prepare_user({}, tmp_path / "data", None, **fakes)
    assert out["HOME"] == dropprivs.HOME_DEFAULT


def test_prepare_user_bad_puid_raises_before_touching_anything(tmp_path):
    log, fakes = _fakes()
    with pytest.raises(ValueError, match="PUID must be a whole number"):
        prepare_user({"PUID": "me"}, tmp_path / "data", None, **fakes)
    assert log == [] and not (tmp_path / "data").exists()


def test_check_writable_passes_and_creates(tmp_path):
    check_writable(tmp_path / "new")
    assert (tmp_path / "new").is_dir()
    check_writable(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_check_writable_raises_naming_the_path(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(PermissionError, match=f"{ro} is not writable by uid {os.geteuid()}"):
            check_writable(ro)
        with pytest.raises(PermissionError, match="is not writable by uid"):
            check_writable(ro / "sub")
    finally:
        ro.chmod(0o700)
