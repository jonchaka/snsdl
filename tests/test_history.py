import json

from app import history as history_mod
from app.history import HISTORY_CAP, Entry, HistoryStore
from app.jobs import DONE, FAILED, Job


def _job(id="j1", url="https://www.youtube.com/watch?v=a", **kw):
    kw.setdefault("status", DONE)
    kw.setdefault("title", "Title")
    kw.setdefault("finished_at", "2026-11-01T00:00:00+00:00")
    return Job(id=id, url=url, **kw)


def _store(tmp_path):
    h = HistoryStore(tmp_path / "history.json")
    h.load()
    return h


def test_record_done_jobs_only_and_fields(tmp_path):
    h = _store(tmp_path)
    done = _job(size=100, duration=61, thumb="abc", uploader="Up", format="mp3", quality="320",
                sponsorblock=True)
    failed = _job(id="j2", url="https://e.com/x", status=FAILED)
    [entry] = h.record([done, failed])
    assert len(entry.id) == 8 and int(entry.id, 16) >= 0
    assert (entry.url, entry.title, entry.format, entry.quality) == (done.url, "Title", "mp3", "320")
    assert (entry.size, entry.duration, entry.thumb, entry.uploader) == (100, 61, "abc", "Up")
    assert entry.site == "youtube.com"
    assert entry.sponsorblock is True and entry.split is False
    assert entry.finished_at == "2026-11-01T00:00:00+00:00"
    assert h.count() == 1
    # persisted
    h2 = _store(tmp_path)
    assert [e.to_dict() for e in h2.list()] == [entry.to_dict()]


def test_record_without_finished_at_uses_now(tmp_path):
    h = _store(tmp_path)
    [entry] = h.record([_job(finished_at=None)])
    assert entry.finished_at and entry.finished_at.startswith("20")


def test_record_dedupes_same_key_and_moves_to_top(tmp_path):
    h = _store(tmp_path)
    h.record([_job(id="a", url="https://e.com/1", title="old")])
    h.record([_job(id="b", url="https://e.com/2")])
    h.record([_job(id="c", url="https://e.com/1", title="new")])
    entries = h.list()
    assert [e.url for e in entries] == ["https://e.com/1", "https://e.com/2"]
    assert entries[0].title == "new"
    # A different key (format, clip, quality, subtitles, split) is a separate entry.
    h.record([_job(id="d", url="https://e.com/1", format="mp3")])
    h.record([_job(id="e", url="https://e.com/1", clip_start=1.0, clip_end=2.0)])
    h.record([_job(id="f", url="https://e.com/1", quality="720")])
    h.record([_job(id="g", url="https://e.com/1", subtitles="en")])
    h.record([_job(id="h", url="https://e.com/1", split=True)])
    assert h.count() == 7


def test_record_groups_split_parts(tmp_path):
    h = _store(tmp_path)
    parts = [_job(id=f"p{i}", playlist_id="split-xyz", playlist="Whole video", playlist_index=i,
                  title=f"Chapter {i}", size=10 * i, duration=i, thumb="th")
             for i in (1, 2, 3)]
    other = _job(id="o", url="https://e.com/other", playlist_id="list1", playlist="A list")
    entries = h.record(parts + [other])
    assert len(entries) == 2
    split = next(e for e in entries if e.split)
    assert split.title == "Whole video"
    assert split.size == 60 and split.duration == 6
    assert split.url == parts[0].url and split.thumb == "th"
    plain = next(e for e in entries if not e.split)
    assert plain.title == "Title"   # an ordinary playlist is not grouped
    assert h.count() == 2


def test_split_group_with_unknown_sizes(tmp_path):
    h = _store(tmp_path)
    parts = [_job(id=f"p{i}", playlist_id="split-q", playlist="V", size=None, duration=None)
             for i in (1, 2)]
    [entry] = h.record(parts)
    assert entry.size is None and entry.duration is None


def test_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(history_mod, "HISTORY_CAP", 3)
    h = _store(tmp_path)
    for i in range(5):
        h.record([_job(id=str(i), url=f"https://e.com/{i}")])
    assert [e.url for e in h.list()] == ["https://e.com/4", "https://e.com/3", "https://e.com/2"]
    assert HISTORY_CAP == 1000


def test_list_filter_and_limit(tmp_path):
    h = _store(tmp_path)
    h.record([_job(id="a", url="https://vimeo.com/1", title="Cats", uploader="Alice")])
    h.record([_job(id="b", url="https://www.youtube.com/watch?v=2", title="Dogs", uploader="Bob")])
    assert [e.title for e in h.list(q="cAtS")] == ["Cats"]
    assert [e.title for e in h.list(q="youtube.com")] == ["Dogs"]   # site
    assert [e.title for e in h.list(q="BOB")] == ["Dogs"]           # uploader
    assert [e.title for e in h.list(q="vimeo.com/1")] == ["Cats"]   # url
    assert h.list(q="nothing") == []
    assert [e.title for e in h.list(limit=1)] == ["Dogs"]
    assert h.count() == 2


def test_find_newest_for_url(tmp_path):
    h = _store(tmp_path)
    h.record([_job(id="a", url="https://e.com/1", format="mp3")])
    h.record([_job(id="b", url="https://e.com/1", format="mp4")])
    assert h.find("https://e.com/1").format == "mp4"
    assert h.find("https://e.com/none") is None


def test_remove_clear_and_thumbs(tmp_path):
    h = _store(tmp_path)
    [a] = h.record([_job(id="a", url="https://e.com/1", thumb="t1")])
    h.record([_job(id="b", url="https://e.com/2", thumb="t2"), _job(id="c", url="https://e.com/3")])
    assert h.thumbs_in_use() == {"t1", "t2"}
    assert h.remove(a.id) is True
    assert h.remove(a.id) is False
    assert h.thumbs_in_use() == {"t2"}
    assert _store(tmp_path).count() == 2   # persisted
    h.clear()
    assert h.count() == 0 and h.list() == []
    assert _store(tmp_path).count() == 0


def test_corrupt_file_moved_to_bad(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not json")
    h = HistoryStore(path)
    h.load()
    assert h.count() == 0
    assert (tmp_path / "history.json.bad").read_text() == "{not json"
    assert not path.exists()
    h.record([_job()])
    assert json.loads(path.read_text())[0]["url"] == "https://www.youtube.com/watch?v=a"


def test_missing_file_is_empty_and_entry_round_trip(tmp_path):
    h = HistoryStore(tmp_path / "nope.json")
    h.load()
    assert h.count() == 0
    e = Entry(id="deadbeef", url="u", title="t", site="other", finished_at="x")
    d = e.to_dict()
    assert Entry.from_dict({**d, "unknown": 1}) == e
