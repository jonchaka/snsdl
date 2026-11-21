"""Cut a release in CHANGELOG.md (Keep a Changelog form).

    python3 tools/release.py 1.2.3

Moves the notes under "## [Unreleased]" to a new "## [1.2.3] - YYYY-MM-DD" heading (UTC date),
points the Unreleased compare link at the new tag and adds the "[1.2.3]: .../compare/..." line.
Refuses (exit 1, file untouched) when the version is malformed or not newer than the last release,
when nothing is waiting under Unreleased, or when the file is not in the expected form. Used by
`make release VERSION=1.2.3`, which then commits and tags.
"""
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = "https://github.com/jonchaka/snsdl"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
RELEASED = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$")
UNRELEASED = "## [Unreleased]"


class ReleaseError(Exception):
    pass


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def cut(changelog: str, version: str, today: date) -> str:
    """The changelog with the Unreleased notes released as `version` dated `today`."""
    if not VERSION.match(version):
        raise ReleaseError(f"version must be MAJOR.MINOR.PATCH without a leading v, got {version!r}")
    lines = changelog.splitlines()
    if UNRELEASED not in lines:
        raise ReleaseError(f"CHANGELOG.md has no {UNRELEASED!r} heading")
    start = lines.index(UNRELEASED)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), None)
    if end is None:
        raise ReleaseError("CHANGELOG.md has no released version below Unreleased")
    m = RELEASED.match(lines[end])
    if not m:
        raise ReleaseError(f"unexpected heading below Unreleased: {lines[end]!r} (want '## [x.y.z] - YYYY-MM-DD')")
    previous = m.group(1)
    if _key(version) <= _key(previous):
        raise ReleaseError(f"{version} is not newer than {previous}")
    body = lines[start + 1:end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        raise ReleaseError("nothing under Unreleased to release")
    old_link = f"[Unreleased]: {REPO}/compare/v{previous}...HEAD"
    if old_link not in lines:
        raise ReleaseError(f"CHANGELOG.md lacks the link line {old_link!r}")
    out = lines[:start + 1] + ["", f"## [{version}] - {today.isoformat()}", ""] + body + [""] + lines[end:]
    out[out.index(old_link)] = (f"[Unreleased]: {REPO}/compare/v{version}...HEAD\n"
                                f"[{version}]: {REPO}/compare/v{previous}...v{version}")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release.py MAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    path = Path("CHANGELOG.md")
    try:
        text = cut(path.read_text(encoding="utf-8"), argv[1], datetime.now(timezone.utc).date())
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
