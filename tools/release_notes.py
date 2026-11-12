"""Release notes for a tag from CHANGELOG.md (Keep a Changelog form).

    python tools/release_notes.py v1.2.3 > notes.md

Prints the image line and the "## [1.2.3]" section; exits 1 with a GitHub Actions error
annotation when CHANGELOG.md has no such section, so a tag without notes fails the release job.
"""
import re
import sys
from pathlib import Path

IMAGE = "ghcr.io/jonchaka/snsdl"


LINK_REF = re.compile(r"^\[[^\]]+\]:\s+\S+$")
RELEASE = re.compile(r"^(\d+\.\d+)\.\d+$")


def section(changelog: str, version: str) -> str:
    """The body under "## [version]" up to the next "## [" heading, without the trailing link
    reference lines ("[1.0.0]: url") of the last section, stripped; "" when absent."""
    out: list[str] = []
    on = False
    for line in changelog.splitlines():
        if line.startswith("## ["):
            if on:
                break
            on = line.startswith(f"## [{version}]")
            continue
        if on:
            out.append(line)
    while out and (not out[-1].strip() or LINK_REF.match(out[-1])):
        out.pop()
    return "\n".join(out).strip()


def notes(changelog: str, tag: str) -> str:
    version = tag[1:] if tag.startswith("v") else tag
    body = section(changelog, version)
    if not body:
        raise SystemExit(f"::error::CHANGELOG.md has no section for {version}")
    m = RELEASE.match(version)   # a prerelease such as 1.0.0-rc1 gets no major.minor tag
    also = f" (also `{m.group(1)}`; `latest` follows main)" if m else ""
    return f"Image: `{IMAGE}:{version}`{also}\n\n{body}\n"


if __name__ == "__main__":
    sys.stdout.write(notes(Path("CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1]))
