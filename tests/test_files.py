import pytest

from app import files
from app.jobs import DONE, Job


@pytest.fixture
def keep_settings(settings, tmp_path):
    settings.keep_dir = tmp_path / "keep"
    settings.ensure_dirs()
    return settings


def _job(filename="clip.mp4", keep=False) -> Job:
    return Job(id="j1", url="https://e.com/v", status=DONE, filename=filename, keep=keep)


def _write(path, data=b"x"):
    path.write_bytes(data)
    return path


# -- unique_name -----------------------------------------------------------------------

def test_unique_name_free_name_is_unchanged(tmp_path):
    assert files.unique_name(tmp_path, "a.mp4") == "a.mp4"


def test_unique_name_appends_counter_before_extension(tmp_path):
    _write(tmp_path / "a.mp4")
    assert files.unique_name(tmp_path, "a.mp4") == "a (2).mp4"
    _write(tmp_path / "a (2).mp4")
    _write(tmp_path / "a (3).mp4")
    assert files.unique_name(tmp_path, "a.mp4") == "a (4).mp4"


def test_unique_name_without_extension(tmp_path):
    _write(tmp_path / "README")
    assert files.unique_name(tmp_path, "README") == "README (2)"


def test_unique_name_counts_dangling_symlinks_as_taken(tmp_path):
    (tmp_path / "a.mp4").symlink_to(tmp_path / "missing")
    assert files.unique_name(tmp_path, "a.mp4") == "a (2).mp4"


# -- file_path -------------------------------------------------------------------------

def test_file_path_in_downloads(settings):
    _write(settings.downloads_dir / "clip.mp4")
    assert files.file_path(settings, _job()) == settings.downloads_dir / "clip.mp4"


def test_file_path_missing_or_no_filename_is_none(settings):
    assert files.file_path(settings, _job()) is None
    assert files.file_path(settings, _job(filename=None)) is None


def test_file_path_kept_job_prefers_keep_dir(keep_settings):
    s = keep_settings
    _write(s.keep_dir / "clip.mp4", b"kept")
    _write(s.downloads_dir / "clip.mp4", b"other")
    assert files.file_path(s, _job(keep=True)) == s.keep_dir / "clip.mp4"
    # not kept: the keep_dir copy is not this job's file
    assert files.file_path(s, _job(keep=False)) == s.downloads_dir / "clip.mp4"


def test_file_path_kept_job_falls_back_to_downloads(keep_settings):
    s = keep_settings
    _write(s.downloads_dir / "clip.mp4")
    assert files.file_path(s, _job(keep=True)) == s.downloads_dir / "clip.mp4"


def test_file_path_kept_job_without_keep_dir_uses_downloads(settings):
    _write(settings.downloads_dir / "clip.mp4")
    assert files.file_path(settings, _job(keep=True)) == settings.downloads_dir / "clip.mp4"


def test_file_path_rejects_traversal_filename(settings, tmp_path):
    _write(tmp_path / "secret.txt")
    assert files.file_path(settings, _job(filename="../secret.txt")) is None


def test_file_path_refuses_symlinks_pointing_outside(keep_settings, tmp_path):
    s = keep_settings
    outside = _write(tmp_path / "outside.txt")
    (s.downloads_dir / "clip.mp4").symlink_to(outside)
    assert files.file_path(s, _job()) is None
    (s.keep_dir / "clip.mp4").symlink_to(outside)
    assert files.file_path(s, _job(keep=True)) is None


def test_file_path_returns_the_unresolved_path_for_an_inside_symlink(settings):
    # Callers unlink what file_path returns: that must be the link, not the file it names.
    _write(settings.downloads_dir / "real.mp4")
    (settings.downloads_dir / "clip.mp4").symlink_to(settings.downloads_dir / "real.mp4")
    assert files.file_path(settings, _job()) == settings.downloads_dir / "clip.mp4"


# -- move_to_keep / move_back ------------------------------------------------------------

def test_move_without_keep_dir_is_a_no_op(settings):
    _write(settings.downloads_dir / "clip.mp4")
    assert files.move_to_keep(settings, _job()) == "clip.mp4"
    assert files.move_back(settings, _job(keep=True)) == "clip.mp4"
    assert (settings.downloads_dir / "clip.mp4").is_file()


def test_move_to_keep_and_back(keep_settings):
    s = keep_settings
    _write(s.downloads_dir / "clip.mp4", b"data")
    assert files.move_to_keep(s, _job()) == "clip.mp4"
    assert not (s.downloads_dir / "clip.mp4").exists()
    assert (s.keep_dir / "clip.mp4").read_bytes() == b"data"
    assert files.move_back(s, _job(keep=True)) == "clip.mp4"
    assert (s.downloads_dir / "clip.mp4").read_bytes() == b"data"
    assert not (s.keep_dir / "clip.mp4").exists()


def test_move_to_keep_collision_gets_suffix(keep_settings):
    s = keep_settings
    _write(s.keep_dir / "clip.mp4", b"older")
    _write(s.downloads_dir / "clip.mp4", b"new")
    assert files.move_to_keep(s, _job()) == "clip (2).mp4"
    assert (s.keep_dir / "clip.mp4").read_bytes() == b"older"
    assert (s.keep_dir / "clip (2).mp4").read_bytes() == b"new"


def test_move_back_collision_gets_suffix(keep_settings):
    s = keep_settings
    _write(s.downloads_dir / "clip.mp4", b"other job")
    _write(s.keep_dir / "clip.mp4", b"kept")
    assert files.move_back(s, _job(keep=True)) == "clip (2).mp4"
    assert (s.downloads_dir / "clip (2).mp4").read_bytes() == b"kept"
    assert (s.downloads_dir / "clip.mp4").read_bytes() == b"other job"


def test_move_with_missing_source_returns_filename_unchanged(keep_settings):
    s = keep_settings
    assert files.move_to_keep(s, _job()) == "clip.mp4"
    assert files.move_back(s, _job(keep=True)) == "clip.mp4"
    assert files.move_to_keep(s, _job(filename=None)) is None


def test_move_rejects_traversal_filename(keep_settings, tmp_path):
    s = keep_settings
    _write(s.data_dir / "jobs-ish.json")
    assert files.move_to_keep(s, _job(filename="../jobs-ish.json")) == "../jobs-ish.json"
    assert (s.data_dir / "jobs-ish.json").exists()


def test_move_refuses_a_symlink(keep_settings, tmp_path):
    s = keep_settings
    outside = _write(tmp_path / "outside.txt", b"secret")
    (s.downloads_dir / "clip.mp4").symlink_to(outside)
    assert files.move_to_keep(s, _job()) == "clip.mp4"
    assert (s.downloads_dir / "clip.mp4").is_symlink()
    assert list(s.keep_dir.iterdir()) == []
    assert outside.read_bytes() == b"secret"


# -- find_served_file ------------------------------------------------------------------

def test_find_served_file_downloads_first_then_keep_dir(keep_settings):
    s = keep_settings
    _write(s.keep_dir / "k.mp4")
    assert files.find_served_file(s, "k.mp4") == s.keep_dir.resolve() / "k.mp4"
    _write(s.downloads_dir / "k.mp4")
    assert files.find_served_file(s, "k.mp4") == s.downloads_dir.resolve() / "k.mp4"
    assert files.find_served_file(s, "nope.mp4") is None


def test_find_served_file_without_keep_dir(settings):
    _write(settings.downloads_dir / "a.mp4")
    assert files.find_served_file(settings, "a.mp4") == settings.downloads_dir.resolve() / "a.mp4"
    assert files.find_served_file(settings, "b.mp4") is None


@pytest.mark.parametrize("name", ["", "../jobs.json", "..", ".", ".hidden", "a/b.mp4", "a\\b.mp4",
                                  "a\x00.mp4", "/etc/passwd"])
def test_find_served_file_rejects_bad_names(keep_settings, name):
    s = keep_settings
    _write(s.downloads_dir / ".hidden")
    assert files.find_served_file(s, name) is None


def test_find_served_file_ignores_directories_and_escaping_symlinks(keep_settings, tmp_path):
    s = keep_settings
    (s.downloads_dir / "dir.mp4").mkdir()
    _write(tmp_path / "outside.mp4")
    (s.keep_dir / "link.mp4").symlink_to(tmp_path / "outside.mp4")
    assert files.find_served_file(s, "dir.mp4") is None
    assert files.find_served_file(s, "link.mp4") is None


# -- sidecars (separate subtitle files) ------------------------------------------------

def _sjob(keep=False, sidecars=("clip.en.srt", "clip.de.srt")) -> Job:
    return Job(id="j1", url="https://e.com/v", status=DONE, filename="clip.mp4", keep=keep,
               sidecars=list(sidecars))


def test_sidecar_paths_beside_the_video(keep_settings):
    s = keep_settings
    _write(s.downloads_dir / "clip.mp4")
    _write(s.downloads_dir / "clip.en.srt")
    _write(s.downloads_dir / "clip.de.srt")
    assert files.sidecar_paths(s, _sjob()) == [s.downloads_dir / "clip.en.srt", s.downloads_dir / "clip.de.srt"]
    _write(s.keep_dir / "clip.mp4")
    _write(s.keep_dir / "clip.en.srt")
    # kept: the video is in KEEP_DIR, so its sidecars are looked up there only
    assert files.sidecar_paths(s, _sjob(keep=True)) == [s.keep_dir / "clip.en.srt"]


def test_sidecar_paths_skip_missing_unsafe_and_escaping(settings, tmp_path):
    _write(settings.downloads_dir / "clip.mp4")
    _write(tmp_path / "outside.srt")
    (settings.downloads_dir / "link.srt").symlink_to(tmp_path / "outside.srt")
    job = _sjob(sidecars=["missing.srt", "../outside.srt", "link.srt", ".hidden.srt"])
    assert files.sidecar_paths(settings, job) == []
    assert files.sidecar_paths(settings, _sjob(sidecars=[])) == []


def test_sidecar_paths_without_the_video(settings):
    _write(settings.downloads_dir / "clip.en.srt")
    assert files.sidecar_paths(settings, _sjob()) == [settings.downloads_dir / "clip.en.srt"]


def test_move_sidecars_to_keep_and_back(keep_settings):
    s = keep_settings
    _write(s.downloads_dir / "clip.en.srt", b"en")
    _write(s.downloads_dir / "clip.de.srt", b"de")
    _write(s.keep_dir / "clip.de.srt", b"someone else")
    assert files.move_sidecars(s, _sjob(), to_keep=True) == ["clip.en.srt", "clip.de (2).srt"]
    assert (s.keep_dir / "clip.en.srt").read_bytes() == b"en"
    assert (s.keep_dir / "clip.de (2).srt").read_bytes() == b"de"
    assert list(s.downloads_dir.iterdir()) == []
    back = files.move_sidecars(s, _sjob(keep=True, sidecars=["clip.en.srt", "clip.de (2).srt"]), to_keep=False)
    assert back == ["clip.en.srt", "clip.de (2).srt"]
    assert (s.downloads_dir / "clip.de (2).srt").read_bytes() == b"de"


def test_move_sidecars_without_keep_dir_is_a_no_op(settings):
    _write(settings.downloads_dir / "clip.en.srt")
    assert files.move_sidecars(settings, _sjob(), to_keep=True) == ["clip.en.srt", "clip.de.srt"]
    assert (settings.downloads_dir / "clip.en.srt").is_file()


def test_delete_files_removes_video_and_sidecars_only(keep_settings):
    s = keep_settings
    for name in ("clip.mp4", "clip.en.srt", "clip.de.srt", "other.mp4"):
        _write(s.downloads_dir / name)
    files.delete_files(s, _sjob())
    assert [p.name for p in s.downloads_dir.iterdir()] == ["other.mp4"]
    _write(s.keep_dir / "clip.mp4")
    _write(s.keep_dir / "clip.en.srt")
    files.delete_files(s, _sjob(keep=True))
    assert list(s.keep_dir.iterdir()) == []
    files.delete_files(s, _sjob())   # nothing left: no error
