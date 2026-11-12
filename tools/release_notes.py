"""Release notes for a tag from CHANGELOG.md (Keep a Changelog form).

    python tools/release_notes.py v1.2.3 > notes.md

Prints the image line and the "## [1.2.3]" section; exits 1 with a GitHub Actions error
annotation when CHANGELOG.md has no such section, so a tag without notes fails the release job.
"""
import sys
from pathlib import Path

IMAGE = "ghcr.io/jonchaka/snsdl"


def section(changelog: str, version: str) -> str:
    """The body under "## [version]" up to the next "## [" heading or link reference list, stripped; "" when absent."""
    out: list[str] = []
    on = False
    for line in changelog.splitlines():
        if line.startswith("## [") or (on and line.startswith("[") and "]: " in line):
            if on:
                break
            on = line.startswith(f"## [{version}]")
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()


def notes(changelog: str, tag: str) -> str:
    version = tag[1:] if tag.startswith("v") else tag
    body = section(changelog, version)
    if not body:
        raise SystemExit(f"::error::CHANGELOG.md has no section for {version}")
    major_minor = version.rsplit(".", 1)[0]
    return f"Image: `{IMAGE}:{version}` (also `{major_minor}`; `latest` follows main)\n\n{body}\n"


if __name__ == "__main__":
    sys.stdout.write(notes(Path("CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1]))
