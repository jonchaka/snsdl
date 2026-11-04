import io
import zipfile
from pathlib import Path

from app import zipstream


def _unzip(chunks):
    data = b"".join(chunks)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None
    return zf, data


def test_stream_zip_is_valid_stored_zip_with_contents(tmp_path):
    a = tmp_path / "a.mp4"; a.write_bytes(b"A" * 10)
    b = tmp_path / "b.mp3"; b.write_bytes(b"B" * 3_000_000)   # > one chunk
    chunks = list(zipstream.stream_zip([("001-a.mp4", a), ("002-b.mp3", b)]))
    zf, data = _unzip(chunks)
    assert [i.filename for i in zf.infolist()] == ["001-a.mp4", "002-b.mp3"]
    assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
    assert zf.read("001-a.mp4") == b"A" * 10 and zf.read("002-b.mp3") == b"B" * 3_000_000


def test_stream_zip_yields_bounded_chunks_not_the_whole_file(tmp_path):
    big = tmp_path / "big.bin"; big.write_bytes(b"x" * (5 * 1024 * 1024))
    sizes = [len(c) for c in zipstream.stream_zip([("big.bin", big)], chunk=1024 * 1024)]
    assert max(sizes) <= 1024 * 1024 + 200          # one chunk plus a local header
    assert sum(sizes) > 5 * 1024 * 1024


def test_stream_zip_skips_files_that_vanished(tmp_path):
    a = tmp_path / "a.mp4"; a.write_bytes(b"A")
    zf, _ = _unzip(zipstream.stream_zip([("gone.mp4", tmp_path / "gone.mp4"), ("a.mp4", a)]))
    assert zf.namelist() == ["a.mp4"]


def test_stream_zip_with_no_entries_is_an_empty_archive():
    zf, _ = _unzip(zipstream.stream_zip([]))
    assert zf.namelist() == []


def test_arcnames_strip_job_id_prefix_and_number_by_index():
    out = zipstream.arcnames([(2, "j2", "j2-Second_Song.mp4"), (1, "j1", "j1-First.mp4")])
    assert out == [("002-Second_Song.mp4", "j2-Second_Song.mp4"), ("001-First.mp4", "j1-First.mp4")]


def test_arcnames_dedupe_and_sanitise():
    out = zipstream.arcnames([(1, "a", "a-x.mp4"), (1, "b", "b-x.mp4"), (1, "c", "c-x.mp4"),
                              (None, "d", "weird"), (3, "e", "e-bad/name:here.mp4")])
    assert [n for n, _ in out] == ["001-x.mp4", "001-x (2).mp4", "001-x (3).mp4", "weird", "003-bad_name_here.mp4"]
