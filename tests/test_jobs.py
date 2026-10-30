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
