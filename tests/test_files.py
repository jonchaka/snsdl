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
