import json

from app.jobs import ACTIVE, DONE, DOWNLOADING, FAILED, PAUSED, QUEUED, Job, JobStore


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
    ("wav", "best", "format must be one of mp4, mp3, audio"),
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


def test_add_many_returns_jobs_in_order_persists_and_saves_once(store, monkeypatch):
    saves = []
    real_save = store.save
    monkeypatch.setattr(store, "save", lambda: (saves.append(1), real_save())[1])
    items = [
        {"url": "https://example.com/v/1", "format": "mp3", "quality": "320",
         "playlist": "PL", "playlist_index": 1, "title": "One"},
        {"url": "https://example.com/v/2", "playlist": "PL", "playlist_index": 2},
    ]
    jobs = store.add_many(items)
    assert len(saves) == 1
    assert [j.url for j in jobs] == ["https://example.com/v/1", "https://example.com/v/2"]
    assert len({j.id for j in jobs}) == 2
    assert (jobs[0].format, jobs[0].quality, jobs[0].title) == ("mp3", "320", "One")
    assert (jobs[1].format, jobs[1].quality, jobs[1].title) == ("mp4", "best", None)
    assert [j.playlist_index for j in jobs] == [1, 2]
    assert all(j.playlist == "PL" and j.status == QUEUED for j in jobs)
    on_disk = json.loads(store._path.read_text())
    assert [d["id"] for d in on_disk] == [j.id for j in jobs]


def test_add_many_empty_returns_empty_and_does_not_save(store, monkeypatch):
    saves = []
    monkeypatch.setattr(store, "save", lambda: saves.append(1))
    assert store.add_many([]) == []
    assert saves == []


# -- playlist_id / remove_many ------------------------------------------------

def test_playlist_id_defaults_none_and_round_trips(store):
    job = store.add("https://example.com/v/1")
    assert job.playlist_id is None
    j = Job(id="abcdefgh", url="https://x", playlist="PL", playlist_index=1, playlist_id="plid0001")
    assert Job.from_dict(j.to_dict()) == j
    assert j.to_dict()["playlist_id"] == "plid0001"


def test_legacy_record_without_playlist_id_loads(settings):
    settings.jobs_file.write_text(json.dumps([{
        "id": "legacy03", "url": "https://x", "format": "mp4", "quality": "best", "status": "done",
        "progress": 100, "title": "t", "filename": "f.mp4", "error": None,
        "created_at": "2026-01-01T00:00:00+00:00", "playlist": "PL", "playlist_index": 2,
    }]))
    store = JobStore(settings.jobs_file)
    store.load()
    assert store.get("legacy03").playlist_id is None




def test_remove_where_removes_matching_jobs_with_one_save(settings, store):
    a = store.add("https://example.com/a", playlist_id="p1")
    b = store.add("https://example.com/b", playlist_id="p1")
    c = store.add("https://example.com/c", playlist_id="p2")
    store.update(b.id, status=DONE)
    saves = []
    real_save = store.save
    store.save = lambda: (saves.append(1), real_save())[1]
    removed = store.remove_where(lambda j: j.playlist_id == "p1" and j.status in ACTIVE)
    assert removed == [a]
    assert saves == [1]
    assert {j.id for j in store.list()} == {b.id, c.id}
    assert store.remove_where(lambda j: False) == []
    assert saves == [1]


def test_remove_where_evaluates_predicate_while_holding_the_lock(store):
    store.add("https://example.com/a")
    held = []
    store.remove_where(lambda j: held.append(store._lock.locked()) or False)
    assert held == [True]


# -- requeue_where ---------------------------------------------------------------

def test_requeue_where_resets_only_matching_failed_jobs_with_one_save(settings, store):
    f1 = store.add("https://example.com/f1", playlist="PL", playlist_index=1, playlist_id="p1", title="T1")
    f2 = store.add("https://example.com/f2", playlist_id="p2")
    done = store.add("https://example.com/d", playlist_id="p1")
    queued = store.add("https://example.com/q", playlist_id="p1")
    store.update(f1.id, status=FAILED, error="boom", progress=37, filename="stale.mp4")
    store.update(f2.id, status=FAILED, error="boom")
    store.update(done.id, status=DONE, progress=100, filename="d.mp4")
    saves = []
    real_save = store.save
    store.save = lambda: (saves.append(1), real_save())[1]

    reset = store.requeue_where(lambda j: j.playlist_id == "p1")

    assert reset == [f1]
    assert saves == [1]
    assert (f1.status, f1.progress, f1.error, f1.filename) == (QUEUED, 0, None, None)
    assert (f1.url, f1.playlist, f1.playlist_index, f1.playlist_id, f1.title) == (
        "https://example.com/f1", "PL", 1, "p1", "T1")
    assert store.get(f2.id).status == FAILED
    assert store.get(done.id).status == DONE and store.get(done.id).filename == "d.mp4"
    assert store.get(queued.id).status == QUEUED
    on_disk = {d["id"]: d for d in json.loads(settings.jobs_file.read_text())}
    assert on_disk[f1.id]["status"] == QUEUED and on_disk[f1.id]["error"] is None


def test_requeue_where_no_match_saves_nothing(store):
    store.add("https://example.com/a")
    saves = []
    store.save = lambda: saves.append(1)
    assert store.requeue_where(lambda j: True) == []   # nothing is failed
    assert saves == []


def test_requeue_where_evaluates_predicate_while_holding_the_lock(store):
    job = store.add("https://example.com/a")
    store.update(job.id, status=FAILED, error="x")
    held = []
    store.requeue_where(lambda j: held.append(store._lock.locked()) or False)
    assert held == [True]


# -- paused / transition_where ----------------------------------------------------

def test_paused_is_active_and_find_active_matches_it(store):
    assert PAUSED == "paused" and PAUSED in ACTIVE
    job = store.add("https://example.com/v")
    store.update(job.id, status=PAUSED)
    assert store.find_active("https://example.com/v") is job


def test_transition_where_moves_only_matching_jobs_in_from_state(settings, store):
    a = store.add("https://example.com/a", playlist_id="p1")
    b = store.add("https://example.com/b", playlist_id="p1")
    c = store.add("https://example.com/c", playlist_id="p2")
    store.update(b.id, status=DOWNLOADING)
    saves = []
    real_save = store.save
    store.save = lambda: (saves.append(1), real_save())[1]
    moved = store.transition_where(lambda j: j.playlist_id == "p1", QUEUED, PAUSED)
    assert moved == [a]
    assert saves == [1]
    assert (store.get(a.id).status, store.get(b.id).status, store.get(c.id).status) == (PAUSED, DOWNLOADING, QUEUED)
    on_disk = {d["id"]: d["status"] for d in json.loads(settings.jobs_file.read_text())}
    assert on_disk[a.id] == PAUSED
    assert store.transition_where(lambda j: True, PAUSED, QUEUED) == [a]
    assert store.get(a.id).status == QUEUED
    assert saves == [1, 1]


def test_transition_where_no_match_saves_nothing_and_holds_lock(store):
    store.add("https://example.com/a")
    saves, held = [], []
    store.save = lambda: saves.append(1)
    assert store.transition_where(lambda j: held.append(store._lock.locked()) or False, QUEUED, PAUSED) == []
    assert saves == [] and held == [True]


def test_recover_after_restart_leaves_paused_jobs_paused(store):
    paused = store.add("https://example.com/p")
    store.update(paused.id, status=PAUSED)
    queued = store.add("https://example.com/q")
    requeue = store.recover_after_restart()
    assert [j.id for j in requeue] == [queued.id]
    assert store.get(paused.id).status == PAUSED


# -- tolerant loading / size ------------------------------------------------------

def test_from_dict_ignores_unknown_keys():
    j = Job(id="abcdefgh", url="https://x")
    d = {**j.to_dict(), "future_field": 123, "another": "x"}
    assert Job.from_dict(d) == j


def test_size_defaults_none_and_round_trips(store):
    assert store.add("https://example.com/v").size is None
    j = Job(id="abcdefgh", url="https://x", status=DONE, size=1234)
    assert Job.from_dict(j.to_dict()) == j and j.to_dict()["size"] == 1234


# -- speed / eta -------------------------------------------------------------------

def test_job_defaults_have_no_speed_or_eta():
    job = Job(id="a", url="https://e.com/v")
    assert job.speed is None and job.eta is None
    assert "speed" in job.to_dict() and "eta" in job.to_dict()


def test_recover_after_restart_clears_speed_and_eta(store):
    job = store.add("https://e.com/v")
    store.update(job.id, status=DOWNLOADING, speed=500, eta=20)
    store.recover_after_restart()
    assert store.get(job.id).speed is None and store.get(job.id).eta is None


def test_requeue_where_clears_speed_and_eta(store):
    job = store.add("https://e.com/v")
    store.update(job.id, status=FAILED, speed=500, eta=20)
    store.requeue_where(lambda j: True)
    assert store.get(job.id).speed is None and store.get(job.id).eta is None


# -- keep / clear(keep=) -------------------------------------------------------------

def test_job_keep_defaults_false():
    assert Job(id="a", url="https://e.com/v").keep is False


def test_clear_with_keep_predicate_keeps_matching_jobs_with_one_save(settings, store, monkeypatch):
    a = store.add("https://e.com/a"); b = store.add("https://e.com/b"); c = store.add("https://e.com/c")
    store.update(a.id, status=DONE, keep=True)
    store.update(b.id, status=DONE, keep=False)
    saves = []
    real_save = store.save
    monkeypatch.setattr(store, "save", lambda: (saves.append(1), real_save())[1])
    kept = store.clear(keep=lambda j: j.keep)
    assert [j.id for j in kept] == [a.id]
    assert [j.id for j in store.list()] == [a.id]
    assert len(saves) == 1
    assert [d["id"] for d in json.loads(settings.jobs_file.read_text())] == [a.id]


def test_clear_without_predicate_keeps_nothing(store):
    job = store.add("https://e.com/a")
    store.update(job.id, status=DONE, keep=True)
    assert store.clear() == []
    assert store.list() == []


# -- audio (original) format ---------------------------------------------------------

def test_audio_format_accepts_only_best():
    check_format_quality("audio", "best")
    with pytest.raises(ValueError, match="not valid for format audio"):
        check_format_quality("audio", "320")
    with pytest.raises(ValueError, match="not valid for format audio"):
        check_format_quality("audio", "1080")


# -- subtitles -----------------------------------------------------------------

def test_add_stores_subtitles_and_defaults_to_none(store):
    plain = store.add("https://e.com/v")
    subbed = store.add("https://e.com/v", subtitles="en")
    assert plain.subtitles is None and store.get(subbed.id).subtitles == "en"


def test_find_active_distinguishes_subtitles(store):
    url = "https://e.com/v"
    subbed = store.add(url, format="mp4", quality="1080", subtitles="en")
    assert store.find_active(url, "mp4", "1080") is None
    assert store.find_active(url, "mp4", "1080", "en") is subbed
    assert store.find_active(url, "mp4", "1080", "auto:en") is None


def test_requeue_clears_needs_login(store):
    job = store.add("https://e.com/v")
    store.update(job.id, status=FAILED, error="login", needs_login=True)
    store.requeue_where(lambda j: True)
    assert store.get(job.id).needs_login is False


# -- round 6: clips, priority, log, thumbnails ------------------------------------

def test_round6_fields_default_to_none_or_false():
    j = Job(id="a", url="https://e.com/v")
    assert (j.clip_start, j.clip_end, j.priority, j.log, j.thumb) == (None, None, False, None, None)


def test_round6_fields_round_trip_through_dict_and_file(settings, store):
    job = store.add("https://e.com/v", clip_start=1.5, clip_end=90.0, thumb="abcdEFGH12")
    store.update(job.id, priority=True, log="ERROR: boom")
    j = store.get(job.id)
    assert Job.from_dict(j.to_dict()) == j
    other = JobStore(settings.jobs_file)
    other.load()
    loaded = other.get(job.id)
    assert (loaded.clip_start, loaded.clip_end, loaded.thumb, loaded.priority, loaded.log) == \
        (1.5, 90.0, "abcdEFGH12", True, "ERROR: boom")


def test_legacy_record_without_round6_fields_loads(settings):
    settings.jobs_file.write_text(json.dumps([{"id": "old1", "url": "https://e.com/v", "status": "done"}]))
    s = JobStore(settings.jobs_file)
    s.load()
    j = s.get("old1")
    assert (j.clip_start, j.clip_end, j.priority, j.log, j.thumb) == (None, None, False, None, None)


def test_to_dict_public_omits_log():
    j = Job(id="a", url="https://e.com/v", status=FAILED, log="secret-ish details")
    assert j.to_dict()["log"] == "secret-ish details"
    public = j.to_dict(public=True)
    assert "log" not in public
    assert public["id"] == "a" and public["priority"] is False and "thumb" in public


def test_add_many_accepts_clip_and_thumb_keys(store):
    jobs = store.add_many([{"url": "https://e.com/a", "clip_start": 10.0, "clip_end": None, "thumb": "k1k1k1k1"},
                           {"url": "https://e.com/b"}])
    assert (jobs[0].clip_start, jobs[0].clip_end, jobs[0].thumb) == (10.0, None, "k1k1k1k1")
    assert (jobs[1].clip_start, jobs[1].thumb) == (None, None)


def test_find_active_distinguishes_clips(store):
    url = "https://e.com/v"
    whole = store.add(url)
    clip = store.add(url, clip_start=5.0, clip_end=20.0)
    open_end = store.add(url, clip_start=5.0)
    assert store.find_active(url) is whole
    assert store.find_active(url, "mp4", "best", None, 5.0, 20.0) is clip
    assert store.find_active(url, clip_start=5.0) is open_end
    assert store.find_active(url, clip_start=5.0, clip_end=21.0) is None
    assert store.find_active(url, clip_end=20.0) is None


def test_requeue_clears_priority_and_log(store):
    job = store.add("https://e.com/v")
    store.update(job.id, status=FAILED, error="boom", log="ERROR: boom", priority=True)
    store.requeue_where(lambda j: True)
    j = store.get(job.id)
    assert j.status == QUEUED and j.priority is False and j.log is None


def test_recover_after_restart_returns_priority_first_then_oldest(store):
    a = store.add("https://e.com/a"); store.update(a.id, created_at="2026-01-01T00:00:03+00:00")
    b = store.add("https://e.com/b"); store.update(b.id, created_at="2026-01-01T00:00:01+00:00")
    c = store.add("https://e.com/c"); store.update(c.id, created_at="2026-01-01T00:00:04+00:00", priority=True)
    d = store.add("https://e.com/d"); store.update(d.id, created_at="2026-01-01T00:00:02+00:00", priority=True)
    requeue = store.recover_after_restart()
    assert [j.id for j in requeue] == [d.id, c.id, b.id, a.id]


def test_recover_after_restart_resets_priority_on_failed_jobs(store):
    job = store.add("https://e.com/v")
    store.update(job.id, status=DOWNLOADING, priority=True)
    store.recover_after_restart()
    j = store.get(job.id)
    assert j.status == FAILED and j.priority is False


def test_update_where_applies_fields_only_when_the_predicate_holds(store):
    job = store.add("https://example.com/a")
    updated = store.update_where(job.id, lambda j: j.status == QUEUED, priority=True)
    assert updated is not None and updated.priority is True
    store.update(job.id, status=DOWNLOADING, priority=False)
    assert store.update_where(job.id, lambda j: j.status == QUEUED, priority=True) is None
    assert store.get(job.id).priority is False
    assert store.update_where("nope", lambda j: True, priority=True) is None


def test_update_where_saves_once(store, monkeypatch):
    job = store.add("https://example.com/a")
    saves = []
    monkeypatch.setattr(store, "save", lambda: saves.append(1))
    store.update_where(job.id, lambda j: j.status == QUEUED, status=PAUSED, priority=False)
    store.update_where(job.id, lambda j: j.status == QUEUED, status=PAUSED)
    assert saves == [1] and store.get(job.id).status == PAUSED


# -- round 7 ---------------------------------------------------------------------------

from app import jobs as jobs_mod


def test_retry_constants():
    assert jobs_mod.RETRY_DELAY == 30.0
    assert jobs_mod.AUTO_RETRIES == 1


def test_round7_fields_default():
    j = Job(id="a", url="https://e.com/v")
    assert (j.clip_exact, j.attempts, j.retry_at) == (False, 0, None)


def test_legacy_record_without_round7_fields_loads(settings):
    settings.jobs_file.write_text(json.dumps([{"id": "old1", "url": "https://e.com/v", "status": "queued"}]))
    s = JobStore(settings.jobs_file)
    s.load()
    j = s.get("old1")
    assert (j.clip_exact, j.attempts, j.retry_at) == (False, 0, None)


def test_round7_fields_round_trip_through_file(settings, store):
    job = store.add("https://e.com/v", clip_start=1.0, clip_end=5.0, clip_exact=True)
    store.update(job.id, attempts=1, retry_at="2026-11-05T10:00:30+00:00")
    other = JobStore(settings.jobs_file)
    other.load()
    loaded = other.get(job.id)
    assert (loaded.clip_exact, loaded.attempts, loaded.retry_at) == (True, 1, "2026-11-05T10:00:30+00:00")


def test_add_many_accepts_clip_exact(store):
    jobs = store.add_many([{"url": "https://e.com/a", "clip_start": 1.0, "clip_exact": True},
                           {"url": "https://e.com/b"}])
    assert jobs[0].clip_exact is True and jobs[1].clip_exact is False


def test_find_active_distinguishes_clip_exact(store):
    url = "https://e.com/v"
    fast = store.add(url, clip_start=5.0, clip_end=20.0)
    exact = store.add(url, clip_start=5.0, clip_end=20.0, clip_exact=True)
    assert store.find_active(url, clip_start=5.0, clip_end=20.0) is fast
    assert store.find_active(url, clip_start=5.0, clip_end=20.0, clip_exact=True) is exact
    store.remove(exact.id)
    assert store.find_active(url, clip_start=5.0, clip_end=20.0, clip_exact=True) is None


def test_requeue_resets_attempts_and_retry_at(store):
    job = store.add("https://e.com/v")
    store.update(job.id, status=FAILED, error="boom", attempts=1, retry_at="2026-11-05T10:00:30+00:00")
    store.requeue_where(lambda j: True)
    j = store.get(job.id)
    assert j.status == QUEUED and j.attempts == 0 and j.retry_at is None


def test_recover_after_restart_keeps_retry_at_on_queued_jobs(store):
    job = store.add("https://e.com/v")
    store.update(job.id, attempts=1, retry_at="2026-11-05T10:00:30+00:00", error="retrying: timed out")
    requeue = store.recover_after_restart()
    assert [j.id for j in requeue] == [job.id]
    j = store.get(job.id)
    assert j.retry_at == "2026-11-05T10:00:30+00:00" and j.attempts == 1
