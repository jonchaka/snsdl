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


# -- media layout: media_subdir / served_path -------------------------------------------------

@pytest.mark.parametrize("uploader, expected", [
    ("Some Channel", "Some Channel"),
    ('a/b\\c:d*e?f"g<h>i|j', "a b c d e f g h i j"),
    ("tab\there\nnew\x00line\x7f", "tab here new line"),
    ("  lots   of   space  ", "lots of space"),
    ("..dots and spaces.. ", "dots and spaces"),
    ("x" * 100, "x" * 80),
    ("", "Other"),
    (None, "Other"),
    (" ... ", "Other"),
    ("///", "Other"),
    ("日本語チャンネル", "日本語チャンネル"),
])
def test_media_subdir_sanitizes_the_uploader(uploader, expected):
    job = Job(id="j1", url="https://e.com/v", uploader=uploader)
    assert files.media_subdir(job) == expected
    assert files._valid_name(files.media_subdir(job))


def test_media_subdir_does_not_end_in_a_space_or_dot_after_the_cut():
    job = Job(id="j1", url="https://e.com/v", uploader="x" * 79 + " tail")
    assert files.media_subdir(job) == "x" * 79


def test_served_path():
    assert files.served_path(Job(id="j", url="u", filename="a b.mp4")) == "a b.mp4"
    assert files.served_path(Job(id="j", url="u", filename="a.mp4", subdir="Chan")) == "Chan/a.mp4"


# -- keep ---------------------------------------------------------------------------------

THUMB = "abcdefgh123"


@pytest.fixture
def media_settings(keep_settings):
    keep_settings.media_layout = True
    return keep_settings


def _kept_job(store, settings, filename="clip.mp4", sidecars=(), **fields):
    job = store.add("https://e.com/v")
    store.update(job.id, status=DONE, filename=filename, sidecars=list(sidecars), **fields)
    _write(settings.downloads_dir / filename, b"video")
    for name in sidecars:
        _write(settings.downloads_dir / name, b"subs")
    return job.id


def test_keep_without_media_layout_moves_into_keep_dir(keep_settings, store):
    s = keep_settings
    jid = _kept_job(store, s, sidecars=["clip.en.srt"], uploader="Chan")
    job = files.keep(s, store, jid, True)
    assert job.keep is True and job.subdir is None and job.filename == "clip.mp4"
    assert sorted(p.name for p in s.keep_dir.iterdir()) == ["clip.en.srt", "clip.mp4"]
    job = files.keep(s, store, jid, False)
    assert job.keep is False and list(s.keep_dir.iterdir()) == []
    assert (s.downloads_dir / "clip.mp4").read_bytes() == b"video"


def test_keep_without_keep_dir_only_sets_the_flag(settings, store):
    settings.media_layout = True   # ignored without KEEP_DIR
    jid = _kept_job(store, settings, uploader="Chan")
    job = files.keep(settings, store, jid, True)
    assert job.keep is True and job.subdir is None
    assert (settings.downloads_dir / "clip.mp4").is_file()


def test_keep_missing_or_not_done_job_is_none(keep_settings, store):
    assert files.keep(keep_settings, store, "nope", True) is None
    job = store.add("https://e.com/v")
    assert files.keep(keep_settings, store, job.id, True) is None
    assert store.get(job.id).keep is False


@pytest.mark.parametrize("ext", ["jpg", "png", "webp"])
def test_keep_with_media_layout_writes_folder_nfo_and_poster(media_settings, store, ext):
    s = media_settings
    _write(s.thumbs_dir / f"{THUMB}.{ext}", b"image")
    jid = _kept_job(store, s, sidecars=["clip.en.srt"], uploader="My: Channel", title="A <b> & c",
                    description="About it", upload_date="2026-01-02", duration=1530, thumb=THUMB,
                    finished_at="2026-11-06T10:20:30.123456+00:00")
    job = files.keep(s, store, jid, True)
    folder = s.keep_dir / "My Channel"
    assert job.keep is True and job.subdir == "My Channel" and job.sidecars == ["clip.en.srt"]
    assert sorted(p.name for p in folder.iterdir()) == ["clip-poster." + ext, "clip.en.srt", "clip.mp4", "clip.nfo"]
    assert (folder / f"clip-poster.{ext}").read_bytes() == b"image"
    assert files.file_path(s, job) == folder / "clip.mp4"
    assert files.sidecar_paths(s, job) == [folder / "clip.en.srt"]
    assert sorted(p.name for p in files.extras_paths(s, job)) == ["clip-poster." + ext, "clip.nfo"]

    import xml.etree.ElementTree as ET
    root = ET.parse(folder / "clip.nfo").getroot()
    assert root.tag == "movie"
    assert {el.tag: el.text for el in root} == {
        "title": "A <b> & c", "plot": "About it", "premiered": "2026-01-02", "studio": "My: Channel",
        "runtime": "26", "uniqueid": "https://e.com/v", "dateadded": "2026-11-06 10:20:30"}
    assert root.find("uniqueid").attrib == {"type": "url", "default": "true"}


def test_nfo_leaves_out_unknown_values(media_settings, store):
    s = media_settings
    jid = _kept_job(store, s, filename="x.mp4")
    files.keep(s, store, jid, True)
    import xml.etree.ElementTree as ET
    root = ET.parse(s.keep_dir / "Other" / "x.nfo").getroot()
    assert [el.tag for el in root] == ["title", "uniqueid"]
    assert root.find("title").text == "x"
    assert not list((s.keep_dir / "Other").glob("*-poster.*"))   # no thumbnail, no poster


def test_keep_skips_an_avif_poster(media_settings, store):
    s = media_settings
    _write(s.thumbs_dir / f"{THUMB}.avif", b"image")
    jid = _kept_job(store, s, uploader="Chan", thumb=THUMB)
    files.keep(s, store, jid, True)
    assert sorted(p.name for p in (s.keep_dir / "Chan").iterdir()) == ["clip.mp4", "clip.nfo"]


def test_keep_succeeds_when_the_nfo_cannot_be_written(media_settings, store, monkeypatch):
    import xml.etree.ElementTree as ET

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(ET.ElementTree, "write", boom)
    s = media_settings
    jid = _kept_job(store, s, uploader="Chan")
    job = files.keep(s, store, jid, True)
    assert job.keep is True and (s.keep_dir / "Chan" / "clip.mp4").is_file()
    assert not (s.keep_dir / "Chan" / "clip.nfo").exists()


def test_unkeep_removes_extras_and_the_emptied_folder(media_settings, store):
    s = media_settings
    _write(s.thumbs_dir / f"{THUMB}.jpg", b"image")
    jid = _kept_job(store, s, sidecars=["clip.en.srt"], uploader="Chan", thumb=THUMB)
    files.keep(s, store, jid, True)
    job = files.keep(s, store, jid, False)
    assert job.keep is False and job.subdir is None and job.filename == "clip.mp4"
    assert list(s.keep_dir.iterdir()) == []
    assert sorted(p.name for p in s.downloads_dir.iterdir()) == ["clip.en.srt", "clip.mp4"]
    assert s.keep_dir.is_dir()


def test_unkeep_leaves_a_folder_other_files_still_use(media_settings, store):
    s = media_settings
    a = _kept_job(store, s, filename="a.mp4", uploader="Chan")
    b = _kept_job(store, s, filename="b.mp4", uploader="Chan")
    files.keep(s, store, a, True)
    files.keep(s, store, b, True)
    files.keep(s, store, a, False)
    assert sorted(p.name for p in (s.keep_dir / "Chan").iterdir()) == ["b.mp4", "b.nfo"]


def test_keep_twice_leaves_a_kept_file_where_it_is(media_settings, store):
    s = media_settings
    jid = _kept_job(store, s, uploader="Chan")
    files.keep(s, store, jid, True)
    store.update(jid, uploader="Renamed")
    job = files.keep(s, store, jid, True)
    assert job.subdir == "Chan" and (s.keep_dir / "Chan" / "clip.mp4").is_file()


def test_keep_job_cleared_mid_move_deletes_moved_files_and_folder(media_settings, store, monkeypatch):
    s = media_settings
    jid = _kept_job(store, s, sidecars=["clip.en.srt"], uploader="Chan")
    real = files.move_sidecars

    def move_then_clear(settings, job, to_keep):
        out = real(settings, job, to_keep)
        store.clear()
        return out

    monkeypatch.setattr(files, "move_sidecars", move_then_clear)
    assert files.keep(s, store, jid, True) is None
    assert list(s.keep_dir.iterdir()) == []


def test_keep_into_a_symlinked_folder_falls_back_to_keep_dir(media_settings, store, tmp_path):
    s = media_settings
    (tmp_path / "elsewhere").mkdir()
    (s.keep_dir / "Chan").symlink_to(tmp_path / "elsewhere")
    jid = _kept_job(store, s, uploader="Chan")
    job = files.keep(s, store, jid, True)
    assert job.subdir is None and (s.keep_dir / "clip.mp4").is_file()
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_delete_files_removes_extras_and_the_emptied_folder(media_settings, store):
    s = media_settings
    _write(s.thumbs_dir / f"{THUMB}.png", b"image")
    jid = _kept_job(store, s, sidecars=["clip.en.srt"], uploader="Chan", thumb=THUMB)
    job = files.keep(s, store, jid, True)
    files.delete_files(s, job)
    assert list(s.keep_dir.iterdir()) == []


def test_extras_paths_need_a_subdir(keep_settings):
    s = keep_settings
    _write(s.downloads_dir / "clip.mp4")
    _write(s.downloads_dir / "clip.nfo")
    assert files.extras_paths(s, _job()) == []


def test_remove_empty_subdir_never_touches_keep_dir_or_a_full_folder(keep_settings):
    s = keep_settings
    (s.keep_dir / "Full").mkdir()
    _write(s.keep_dir / "Full" / "a.mp4")
    files.remove_empty_subdir(s, "Full")
    files.remove_empty_subdir(s, "Missing")
    files.remove_empty_subdir(s, None)
    files.remove_empty_subdir(s, "")
    files.remove_empty_subdir(s, "..")
    assert (s.keep_dir / "Full" / "a.mp4").is_file() and s.keep_dir.is_dir()


# -- find_served_file with a subdir ------------------------------------------------------------

def test_find_served_file_in_a_media_folder(keep_settings, tmp_path):
    s = keep_settings
    (s.keep_dir / "Chan").mkdir()
    _write(s.keep_dir / "Chan" / "a.mp4")
    _write(s.downloads_dir / "b.mp4")
    assert files.find_served_file(s, "Chan/a.mp4") == (s.keep_dir / "Chan" / "a.mp4").resolve()
    assert files.find_served_file(s, "a.mp4") is None             # not at the top level
    assert files.find_served_file(s, "downloads/b.mp4") is None   # two segments: KEEP_DIR only
    (tmp_path / "out").mkdir()
    _write(tmp_path / "out" / "c.mp4")
    (s.keep_dir / "Link").symlink_to(tmp_path / "out")
    assert files.find_served_file(s, "Link/c.mp4") is None


@pytest.mark.parametrize("path", ["Chan/a/b.mp4", "../keep/a.mp4", "Chan/../a.mp4", "./a.mp4",
                                  ".Chan/a.mp4", "Chan/", "/a.mp4", "Chan//a.mp4", "Chan/.a.mp4"])
def test_find_served_file_refuses_bad_two_segment_paths(keep_settings, path):
    s = keep_settings
    (s.keep_dir / "Chan" / "a").mkdir(parents=True)
    _write(s.keep_dir / "Chan" / "a" / "b.mp4")
    _write(s.keep_dir / "a.mp4")
    assert files.find_served_file(s, path) is None


def test_find_served_file_two_segments_need_keep_dir(settings):
    (settings.downloads_dir / "Chan").mkdir()
    _write(settings.downloads_dir / "Chan" / "a.mp4")
    assert files.find_served_file(settings, "Chan/a.mp4") is None
