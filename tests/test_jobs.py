import json

from app.jobs import DONE, DOWNLOADING, FAILED, QUEUED, Job, JobStore


def test_add_creates_queued_job(store):
    job = store.add("https://example.com/v/1")
    assert len(job.id) == 8
    assert job.status == QUEUED
    assert job.progress == 0
    assert job.title is None and job.filename is None and job.error is None
    assert store.get(job.id) is job


def test_update_sets_fields_and_returns_job(store):
    job = store.add("https://example.com/v/1")
    updated = store.update(job.id, status=DOWNLOADING, progress=42, title="T")
    assert updated.status == DOWNLOADING
    assert updated.progress == 42
    assert updated.title == "T"


def test_update_unknown_id_returns_none(store):
    assert store.update("nope", status=DOWNLOADING) is None


def test_update_rejects_unknown_field(store):
    job = store.add("https://example.com/v/1")
    try:
        store.update(job.id, bogus=1)
    except AttributeError:
        return
    raise AssertionError("expected AttributeError")


def test_list_is_newest_first(store):
    a = store.add("https://example.com/a")
    b = store.add("https://example.com/b")
    assert [j.id for j in store.list()] == [b.id, a.id]


def test_find_active_only_matches_queued_or_downloading(store):
    url = "https://example.com/v/1"
    assert store.find_active(url) is None
    job = store.add(url)
    assert store.find_active(url) is job
    store.update(job.id, status=DOWNLOADING)
    assert store.find_active(url) is job
    store.update(job.id, status=DONE)
    assert store.find_active(url) is None
    store.update(job.id, status=FAILED)
    assert store.find_active(url) is None


def test_persistence_round_trip(settings):
    s1 = JobStore(settings.jobs_file)
    job = s1.add("https://example.com/v/1")
    s1.update(job.id, status=DONE, filename="x.mp4", progress=100)

    s2 = JobStore(settings.jobs_file)
    s2.load()
    loaded = s2.get(job.id)
    assert loaded is not None
    assert loaded.to_dict() == job.to_dict()


def test_save_writes_json_list(settings, store):
    store.add("https://example.com/v/1")
    data = json.loads(settings.jobs_file.read_text())
    assert isinstance(data, list) and data[0]["url"] == "https://example.com/v/1"


def test_load_missing_file_is_empty(settings):
    s = JobStore(settings.jobs_file)
    s.load()
    assert s.list() == []


def test_clear_empties_store_and_file(settings, store):
    store.add("https://example.com/v/1")
    store.clear()
    assert store.list() == []
    assert json.loads(settings.jobs_file.read_text()) == []


def test_recover_after_restart(store):
    dl = store.add("https://example.com/dl")
    store.update(dl.id, status=DOWNLOADING, progress=50)
    q = store.add("https://example.com/q")
    done = store.add("https://example.com/done")
    store.update(done.id, status=DONE)

    requeue = store.recover_after_restart()

    assert [j.id for j in requeue] == [q.id]
    assert store.get(dl.id).status == FAILED
    assert store.get(dl.id).error == "interrupted by restart"
    assert store.get(done.id).status == DONE


def test_job_dict_round_trip():
    j = Job(id="abcdefgh", url="https://x", status=DONE, progress=100,
            title="t", filename="f.mp4", error=None, created_at="2026-01-01T00:00:00+00:00")
    assert Job.from_dict(j.to_dict()) == j


import pytest

from app.jobs import check_format_quality


def test_add_defaults_to_mp4_best(store):
    job = store.add("https://example.com/v/1")
    assert job.format == "mp4"
    assert job.quality == "best"


def test_add_stores_format_and_quality(store):
    job = store.add("https://example.com/v/1", format="mp3", quality="320")
    assert store.get(job.id).format == "mp3"
    assert store.get(job.id).quality == "320"


def test_legacy_record_without_format_loads_as_mp4_best(settings):
    settings.jobs_file.write_text(json.dumps([{
        "id": "legacy01", "url": "https://x", "status": "done", "progress": 100,
        "title": "t", "filename": "f.mp4", "error": None, "created_at": "2026-01-01T00:00:00+00:00",
    }]))
    store = JobStore(settings.jobs_file)
    store.load()
    job = store.get("legacy01")
    assert job.format == "mp4"
    assert job.quality == "best"
    assert job.to_dict()["format"] == "mp4"
    assert job.to_dict()["quality"] == "best"


def test_find_active_distinguishes_format_and_quality(store):
    url = "https://example.com/v/1"
    video = store.add(url, format="mp4", quality="1080")
    assert store.find_active(url, "mp4", "1080") is video
    assert store.find_active(url, "mp4", "720") is None
    assert store.find_active(url, "mp3", "best") is None
    audio = store.add(url, format="mp3", quality="best")
    assert store.find_active(url, "mp3", "best") is audio
    assert store.find_active(url, "mp4", "1080") is video


def test_remove_deletes_record_and_persists(settings, store):
    keep = store.add("https://example.com/keep")
    gone = store.add("https://example.com/gone")
    removed = store.remove(gone.id)
    assert removed is gone
    assert store.get(gone.id) is None
    assert store.get(keep.id) is keep
    on_disk = [d["id"] for d in json.loads(settings.jobs_file.read_text())]
    assert on_disk == [keep.id]


def test_remove_unknown_id_returns_none(store):
    assert store.remove("nope") is None


@pytest.mark.parametrize("fmt,quality", [
    ("mp4", "best"), ("mp4", "1080"), ("mp4", "720"),
    ("mp3", "best"), ("mp3", "320"), ("mp3", "192"),
])
def test_check_format_quality_accepts_valid_pairs(fmt, quality):
    check_format_quality(fmt, quality)  # no exception


@pytest.mark.parametrize("fmt,quality,msg", [
    ("mp3", "1080", "quality 1080 is not valid for format mp3"),
    ("mp4", "abc", "quality abc is not valid for format mp4"),
    ("mp4", "0", "quality 0 is not valid for format mp4"),
    ("mp4", "-720", "quality -720 is not valid for format mp4"),
    ("mp4", "١٠٨٠", "quality ١٠٨٠ is not valid for format mp4"),
    ("mp3", "128", "quality 128 is not valid for format mp3"),
    ("wav", "best", "format must be one of mp4, mp3"),
])
def test_check_format_quality_rejects_bad_pairs(fmt, quality, msg):
    with pytest.raises(ValueError) as e:
        check_format_quality(fmt, quality)
    assert str(e.value) == msg


def test_add_defaults_playlist_fields_to_none(store):
    job = store.add("https://example.com/v/1")
    assert job.playlist is None
    assert job.playlist_index is None
    assert job.title is None


def test_add_stores_playlist_fields_and_title(store):
    job = store.add("https://example.com/v/1", playlist="My List", playlist_index=3, title="Third")
    loaded = store.get(job.id)
    assert loaded.playlist == "My List"
    assert loaded.playlist_index == 3
    assert loaded.title == "Third"
    assert loaded.to_dict()["playlist"] == "My List"
    assert loaded.to_dict()["playlist_index"] == 3


def test_legacy_record_without_playlist_fields_loads(settings):
    settings.jobs_file.write_text(json.dumps([{
        "id": "legacy02", "url": "https://x", "format": "mp4", "quality": "best", "status": "done",
        "progress": 100, "title": "t", "filename": "f.mp4", "error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }]))
    store = JobStore(settings.jobs_file)
    store.load()
    job = store.get("legacy02")
    assert job.playlist is None and job.playlist_index is None


def test_playlist_job_round_trip():
    j = Job(id="abcdefgh", url="https://x", playlist="PL", playlist_index=7)
    assert Job.from_dict(j.to_dict()) == j
