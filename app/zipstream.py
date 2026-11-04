"""Stream a stored (uncompressed) zip of existing files without building it in memory.

zipfile can write to a non-seekable stream: it then emits data descriptors and, with
ZIP_STORED, never needs to rewind. We hand it a tiny file-like sink that buffers what it
writes and let a generator drain the sink after every chunk of file data."""
import re
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

CHUNK = 1024 * 1024


class _Sink:
    """Write-only, non-seekable buffer that zipfile writes into."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        return data


def stream_zip(entries: Iterable[tuple[str, Path]], chunk: int = CHUNK) -> Iterator[bytes]:
    """Yield the bytes of a zip holding (arcname, path) entries; files are stored, not deflated.
    Entries whose file has disappeared are skipped."""
    sink = _Sink()
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for arcname, path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.compress_type = zipfile.ZIP_STORED
            info.file_size = stat.st_size
            with path.open("rb") as src, zf.open(info, mode="w", force_zip64=True) as dst:
                while True:
                    block = src.read(chunk)
                    if not block:
                        break
                    dst.write(block)
                    yield sink.take()
            yield sink.take()
    yield sink.take()   # central directory


_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def arcnames(items: Iterable[tuple[int, str, str]]) -> list[tuple[str, str]]:
    """[(index, job_id, filename)] -> [(arcname, filename)] with names
    `{index:03d}-{filename minus the job-id prefix}`, de-duplicated with ' (2)' style suffixes."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, job_id, filename in items:
        base = filename.removeprefix(f"{job_id}-") or filename
        base = _UNSAFE.sub("_", base).strip() or "file"
        name = f"{index:03d}-{base}" if index is not None else base
        stem, dot, ext = name.rpartition(".")
        candidate, n = name, 1
        while candidate in seen:
            n += 1
            candidate = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
        seen.add(candidate)
        out.append((candidate, filename))
    return out
