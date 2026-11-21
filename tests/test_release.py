import importlib.util
from datetime import date
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release = _load("release")
release_notes = _load("release_notes")

CHANGELOG = """# Changelog

Notes.

## [Unreleased]

### Added
- Thing three.

### Fixed
- A bug.

## [1.1.0] - 2026-11-13

### Added
- Thing two.

## [1.0.0] - 2026-11-08

### Added
- Thing one.

[Unreleased]: https://github.com/jonchaka/snsdl/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/jonchaka/snsdl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/jonchaka/snsdl/releases/tag/v1.0.0
"""

TODAY = date(2026, 12, 1)


def test_cut_moves_the_unreleased_notes_under_a_dated_heading_and_fixes_the_links():
    out = release.cut(CHANGELOG, "1.2.0", TODAY)
    assert out == """# Changelog

Notes.

## [Unreleased]

## [1.2.0] - 2026-12-01

### Added
- Thing three.

### Fixed
- A bug.

## [1.1.0] - 2026-11-13

### Added
- Thing two.

## [1.0.0] - 2026-11-08

### Added
- Thing one.

[Unreleased]: https://github.com/jonchaka/snsdl/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/jonchaka/snsdl/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/jonchaka/snsdl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/jonchaka/snsdl/releases/tag/v1.0.0
"""


def test_the_release_job_finds_the_new_section():
    out = release.cut(CHANGELOG, "2.0.0", TODAY)
    assert release_notes.section(out, "2.0.0") == "### Added\n- Thing three.\n\n### Fixed\n- A bug."
    assert release_notes.section(out, "1.1.0") == "### Added\n- Thing two."


def test_a_second_cut_is_refused_because_unreleased_is_empty():
    once = release.cut(CHANGELOG, "1.2.0", TODAY)
    with pytest.raises(release.ReleaseError, match="nothing under"):
        release.cut(once, "1.2.1", TODAY)


@pytest.mark.parametrize("bad", ["v1.2.0", "1.2", "1.2.0-rc1", "", "latest"])
def test_the_version_must_be_major_minor_patch(bad):
    with pytest.raises(release.ReleaseError, match="MAJOR.MINOR.PATCH"):
        release.cut(CHANGELOG, bad, TODAY)


@pytest.mark.parametrize("old", ["1.1.0", "1.0.5", "0.9.9"])
def test_the_version_must_be_newer_than_the_last_release(old):
    with pytest.raises(release.ReleaseError, match="not newer than 1.1.0"):
        release.cut(CHANGELOG, old, TODAY)


def test_versions_compare_as_numbers_not_text():
    after_1_10 = (CHANGELOG.replace("[1.1.0]", "[1.10.0]").replace("...v1.1.0", "...v1.10.0")
                  .replace("v1.1.0...HEAD", "v1.10.0...HEAD"))
    with pytest.raises(release.ReleaseError, match="1.9.0 is not newer than 1.10.0"):   # "1.9.0" > "1.10.0" as text
        release.cut(after_1_10, "1.9.0", TODAY)
    assert "## [1.11.0] - 2026-12-01" in release.cut(after_1_10, "1.11.0", TODAY)


def test_trailing_spaces_on_the_matched_lines_are_tolerated():
    spaced = (CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]  \n")
              .replace("## [1.1.0] - 2026-11-13\n", "## [1.1.0] - 2026-11-13 \n")
              .replace("v1.1.0...HEAD\n", "v1.1.0...HEAD \n"))
    out = release.cut(spaced, "1.2.0", TODAY)
    assert "## [1.2.0] - 2026-12-01" in out
    assert "[1.2.0]: https://github.com/jonchaka/snsdl/compare/v1.1.0...v1.2.0" in out


def test_a_released_heading_in_an_unexpected_form_is_refused():
    for heading in ["## [1.1.0] \u2014 2026-11-13", "## [1.1.0]", "## [v1.1.0] - 2026-11-13"]:
        with pytest.raises(release.ReleaseError, match="unexpected heading"):
            release.cut(CHANGELOG.replace("## [1.1.0] - 2026-11-13", heading), "1.2.0", TODAY)


def test_no_released_version_below_unreleased_is_refused():
    only_unreleased = CHANGELOG.split("## [1.1.0]")[0]
    with pytest.raises(release.ReleaseError, match="no released version"):
        release.cut(only_unreleased, "1.2.0", TODAY)


def test_missing_unreleased_heading_is_an_error():
    with pytest.raises(release.ReleaseError, match="Unreleased"):
        release.cut(CHANGELOG.replace("## [Unreleased]", "## [Pending]"), "1.2.0", TODAY)


def test_missing_unreleased_link_line_is_an_error_and_nothing_is_lost():
    broken = CHANGELOG.replace("[Unreleased]: https://github.com/jonchaka/snsdl/compare/v1.1.0...HEAD\n", "")
    with pytest.raises(release.ReleaseError, match=r"link line"):
        release.cut(broken, "1.2.0", TODAY)


def test_main_rewrites_the_file_only_on_success(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    bad = subprocess.run([sys.executable, str(TOOLS / "release.py"), "1.0.0"], capture_output=True, text=True)
    assert bad.returncode == 1 and "not newer" in bad.stderr
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG
    good = subprocess.run([sys.executable, str(TOOLS / "release.py"), "1.2.0"], capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
    text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [1.2.0] - " in text and "[1.2.0]: https://github.com/jonchaka/snsdl/compare/v1.1.0...v1.2.0" in text
