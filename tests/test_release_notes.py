import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "release_notes.py"
spec = importlib.util.spec_from_file_location("release_notes", TOOL)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## [Unreleased]

## [1.1.0] - 2026-12-01

### Added
- Thing two.

## [1.0.0] - 2026-11-13

### Added
- Thing one.

[1.0.0]: https://github.com/jonchaka/snsdl/releases/tag/v1.0.0
"""


def test_section_returns_only_that_version():
    assert release_notes.section(CHANGELOG, "1.0.0") == "### Added\n- Thing one."
    assert release_notes.section(CHANGELOG, "1.1.0") == "### Added\n- Thing two."
    assert release_notes.section(CHANGELOG, "9.9.9") == ""


def test_notes_prefix_image_line_and_accept_v_prefix():
    out = release_notes.notes(CHANGELOG, "v1.1.0")
    assert out.splitlines()[0] == "Image: `ghcr.io/jonchaka/snsdl:1.1.0` (also `1.1`; `latest` follows main)"
    assert out.endswith("### Added\n- Thing two.\n")


def test_notes_fail_loudly_without_a_section():
    with pytest.raises(SystemExit) as e:
        release_notes.notes(CHANGELOG, "v2.0.0")
    assert str(e.value) == "::error::CHANGELOG.md has no section for 2.0.0"


def test_real_changelog_has_the_first_release():
    text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text()
    assert release_notes.section(text, "1.0.0").startswith("### Added")
