# tests/test_diff_coverage.py
"""Unit tests for the patch-coverage measurement helper.

A measuring instrument that is itself unmeasured is not evidence, so the module
that answers "are the changed lines covered?" carries its own red probe: a
constructed diff whose changed lines are demonstrably uncovered. If the helper
reported full coverage there, it would not be measuring the thing it names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from script import diff_coverage

REPO = Path("/repo")

_XML = """<?xml version="1.0" ?>
<coverage line-rate="0.8" branch-rate="0.7">
  <sources><source>/repo/custom_components/googlefindmy</source></sources>
  <packages>
    <package name=".">
      <classes>
        <class name="api.py" filename="api.py">
          <lines>
            <line number="10" hits="1"/>
            <line number="11" hits="0"/>
            <line number="12" hits="3"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def _coverage() -> dict[str, dict[int, int]]:
    """Return the parsed fixture report."""

    return diff_coverage.parse_coverage_xml(_XML, REPO)


def test_coverage_paths_are_repository_relative() -> None:
    """The Cobertura filename is resolved through the declared source root."""

    parsed = _coverage()

    assert set(parsed) == {"custom_components/googlefindmy/api.py"}
    assert parsed["custom_components/googlefindmy/api.py"][11] == 0


def test_diff_hunks_yield_the_added_line_numbers() -> None:
    """A unified=0 hunk header carries the post-image range directly."""

    diff = (
        "diff --git a/custom_components/googlefindmy/api.py b/custom_components/googlefindmy/api.py\n"
        "--- a/custom_components/googlefindmy/api.py\n"
        "+++ b/custom_components/googlefindmy/api.py\n@@ -3 +10,3 @@\n@@ -20 +40 @@\n"
    )

    changed = diff_coverage.parse_diff(diff)

    assert changed == {"custom_components/googlefindmy/api.py": {10, 11, 12, 40}}


def test_deleted_files_contribute_no_lines() -> None:
    """A file whose post-image is /dev/null must not enter the measurement."""

    diff = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"

    assert diff_coverage.parse_diff(diff) == {}


def test_uninstrumented_files_are_listed_and_not_counted() -> None:
    """Files outside the coverage source are reported separately, never as misses."""

    changed = {
        "custom_components/googlefindmy/api.py": {10, 12},
        "tests/test_api.py": {1, 2, 3},
    }

    result = diff_coverage.evaluate(changed, _coverage())

    assert result.total == 2
    assert result.covered == 2
    assert result.uninstrumented == (("tests/test_api.py", 3),)
    assert result.uninstrumented_lines == 3
    assert result.percent == pytest.approx(100.0)


def test_red_probe_uncovered_changes_are_not_full_coverage() -> None:
    """Red probe: a diff that only touches an uncovered line must not read as 100%."""

    changed = {"custom_components/googlefindmy/api.py": {11}}

    result = diff_coverage.evaluate(changed, _coverage())

    assert result.covered == 0
    assert result.total == 1
    assert result.percent == pytest.approx(0.0)
    assert result.files[0].missing == (11,)
    report = diff_coverage.format_report(result, 82.21)
    assert "uncovered lines: 11" in report
    assert "BELOW THRESHOLD" in report


def test_lines_without_a_statement_are_excluded() -> None:
    """Blank lines and comments have no XML entry and belong to neither side."""

    changed = {"custom_components/googlefindmy/api.py": {10, 99}}

    result = diff_coverage.evaluate(changed, _coverage())

    assert result.total == 1
    assert result.files[0].missing == ()


def test_an_empty_measurement_is_not_a_full_one() -> None:
    """No instrumented changed statement means no percentage, not 100%."""

    result = diff_coverage.evaluate({"docs/x.md": {1}}, _coverage())

    assert result.percent is None
    report = diff_coverage.format_report(result, 82.21)
    assert "This is not 100%." in report
    assert "BELOW THRESHOLD" not in report


def _patch_git(
    monkeypatch: pytest.MonkeyPatch, recorded: list[list[str]], diff: str
) -> None:
    """Replace the git bridge so the tests never touch a real repository."""

    def _fake(args, repo_root):  # type: ignore[no-untyped-def]
        recorded.append(list(args))
        if args[0] == "merge-base":
            return "cafe1234\n"
        if args[0] == "ls-files":
            return ""
        return diff

    monkeypatch.setattr(diff_coverage, "_run_git", _fake)


def _write_xml(tmp_path: Path) -> Path:
    """Write the fixture report into a temporary file."""

    target = tmp_path / "coverage.xml"
    target.write_text(_XML, encoding="utf-8")
    return target


def test_main_resolves_the_base_through_merge_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The diff base is the merge base of the given ref, not the ref itself."""

    recorded: list[list[str]] = []
    _patch_git(
        monkeypatch,
        recorded,
        "diff --git a/custom_components/googlefindmy/api.py b/custom_components/googlefindmy/api.py\n"
        "--- a/custom_components/googlefindmy/api.py\n"
        "+++ b/custom_components/googlefindmy/api.py\n@@ -1 +10,1 @@\n",
    )

    status = diff_coverage.main(
        [
            "--coverage-xml",
            str(_write_xml(tmp_path)),
            "--repo-root",
            "/repo",
            "--base",
            "origin/main",
        ]
    )

    assert status == diff_coverage.EXIT_OK
    assert recorded[0] == ["merge-base", "origin/main", "HEAD"]
    # The working tree, not cafe1234..HEAD: the coverage report is taken over
    # the working tree, and both sides of the intersection must mean the same
    # revision.
    assert recorded[1] == ["diff", "--unified=0", "cafe1234"]
    # Untracked files are part of that same working tree and are asked for
    # after the tracked diff, never instead of it.
    assert recorded[2] == ["ls-files", "--others", "--exclude-standard", "-z"]
    assert "base: cafe1234" in capsys.readouterr().out


def _untracked_repo(tmp_path: Path, body: str) -> Path:
    """Create a repository root holding one untracked, instrumented module."""

    package = tmp_path / "custom_components" / "googlefindmy"
    package.mkdir(parents=True)
    (package / "fresh.py").write_text(body, encoding="utf-8")
    (tmp_path / "coverage.xml").write_text(
        _XML.replace("/repo/", f"{tmp_path.as_posix()}/")
        .replace(
            'name="api.py" filename="api.py"', 'name="fresh.py" filename="fresh.py"'
        )
        .replace('<line number="10" hits="1"/>', '<line number="1" hits="1"/>')
        .replace('<line number="11" hits="0"/>', '<line number="2" hits="0"/>')
        .replace('<line number="12" hits="3"/>', ""),
        encoding="utf-8",
    )
    return tmp_path


def test_an_untracked_file_enters_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A new, unstaged module is part of the tree the suite ran on.

    Codex finding on PR #1274: ``git diff <base>`` omits untracked files, so
    their statements were listed by the coverage report but never counted as
    changed. Here the tracked diff is empty and the only change is untracked,
    with one covered and one uncovered statement; the result has to be 1/2,
    not "nothing changed".
    """

    repo = _untracked_repo(tmp_path, "a = 1\nb = 2\n")
    recorded: list[list[str]] = []

    def _fake(args, repo_root):  # type: ignore[no-untyped-def]
        recorded.append(list(args))
        if args[0] == "merge-base":
            return "cafe1234\n"
        if args[0] == "ls-files":
            return "custom_components/googlefindmy/fresh.py\0"
        return ""

    monkeypatch.setattr(diff_coverage, "_run_git", _fake)

    status = diff_coverage.main(
        ["--coverage-xml", "coverage.xml", "--repo-root", str(repo), "--threshold", "0"]
    )

    out = capsys.readouterr().out
    assert status == diff_coverage.EXIT_OK
    assert "RESULT: 1/2 (50.00%)" in out


def test_an_untracked_file_that_vanished_is_a_measurement_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A listed file that cannot be read is reported, not silently skipped."""

    repo = _untracked_repo(tmp_path, "a = 1\n")

    def _fake(args, repo_root):  # type: ignore[no-untyped-def]
        if args[0] == "merge-base":
            return "cafe1234\n"
        if args[0] == "ls-files":
            return "custom_components/googlefindmy/gone.py\0"
        return ""

    monkeypatch.setattr(diff_coverage, "_run_git", _fake)

    status = diff_coverage.main(
        ["--coverage-xml", "coverage.xml", "--repo-root", str(repo)]
    )

    assert status == diff_coverage.EXIT_ERROR
    assert (
        "untracked file 'custom_components/googlefindmy/gone.py'"
        in capsys.readouterr().err
    )


def test_main_signals_a_shortfall_with_its_own_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Falling below the threshold is status 2, distinct from a broken measurement."""

    _patch_git(
        monkeypatch,
        [],
        "diff --git a/custom_components/googlefindmy/api.py b/custom_components/googlefindmy/api.py\n"
        "--- a/custom_components/googlefindmy/api.py\n"
        "+++ b/custom_components/googlefindmy/api.py\n@@ -1 +11,1 @@\n",
    )

    status = diff_coverage.main(
        ["--coverage-xml", str(_write_xml(tmp_path)), "--repo-root", "/repo"]
    )

    assert status == diff_coverage.EXIT_BELOW_THRESHOLD
    assert "BELOW THRESHOLD" in capsys.readouterr().out


def test_main_reports_a_missing_report_as_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing coverage.xml is an error, not a coverage verdict."""

    status = diff_coverage.main(
        ["--coverage-xml", str(tmp_path / "absent.xml"), "--repo-root", "/repo"]
    )

    assert status == diff_coverage.EXIT_ERROR
    assert "could not be measured" in capsys.readouterr().err


def test_main_reports_a_git_failure_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A git failure must not be reported as zero patch coverage."""

    def _fail(args, repo_root):  # type: ignore[no-untyped-def]
        raise diff_coverage.MeasurementError("no merge base")

    monkeypatch.setattr(diff_coverage, "_run_git", _fail)

    status = diff_coverage.main(
        ["--coverage-xml", str(_write_xml(tmp_path)), "--repo-root", "/repo"]
    )

    assert status == diff_coverage.EXIT_ERROR
    assert "no merge base" in capsys.readouterr().err


def test_main_reports_an_absent_measurement_with_its_own_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diff without an instrumented statement gets its own exit status.

    Sharing status 0 with "measured and fine" is how a docs-only diff would be
    reported as a passing patch coverage by every caller that reads the status
    instead of the text.
    """

    _patch_git(monkeypatch, [], "--- a/docs/x.md\n+++ b/docs/x.md\n@@ -1 +1,2 @@\n")

    status = diff_coverage.main(
        ["--coverage-xml", str(_write_xml(tmp_path)), "--repo-root", "/repo"]
    )

    assert status == diff_coverage.EXIT_NOT_MEASURABLE
    assert "This is not 100%." in capsys.readouterr().out


def test_a_report_from_another_checkout_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sources that resolve nowhere must not silently empty the measurement."""

    status = diff_coverage.main(
        ["--coverage-xml", str(_write_xml(tmp_path)), "--repo-root", str(tmp_path)]
    )

    assert status == diff_coverage.EXIT_ERROR
    assert "another checkout" in capsys.readouterr().err


def test_a_malformed_hunk_header_is_a_measurement_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken header must fail the measurement, not the whole preflight process."""

    _patch_git(
        monkeypatch,
        [],
        "diff --git a/custom_components/googlefindmy/api.py b/custom_components/googlefindmy/api.py\n"
        "--- a/custom_components/googlefindmy/api.py\n"
        "+++ b/custom_components/googlefindmy/api.py\n@@ broken @@\n",
    )

    status = diff_coverage.main(
        ["--coverage-xml", str(_write_xml(tmp_path)), "--repo-root", "/repo"]
    )

    assert status == diff_coverage.EXIT_ERROR
    assert "hunk header" in capsys.readouterr().err


def test_a_plus_line_without_a_source_header_is_not_a_file() -> None:
    """Only a "+++" that follows its "---" partner starts a file section."""

    diff = "--- a/x\n+++ not-a-header\n@@ -1 +5,2 @@\n"

    assert diff_coverage.parse_diff(diff) == {}


def test_an_existing_file_wins_over_a_merely_resolvable_path(tmp_path: Path) -> None:
    """Two sources can both resolve; only one of them names a real file.

    Cobertura allows several <source> elements, and a source that already ends
    in the package prefix doubles it into a path that resolves cleanly and does
    not exist. Without the file check the parser would pick that one, and every
    entry would land under "not instrumented" while looking measured.
    """

    package = tmp_path / "custom_components" / "googlefindmy"
    package.mkdir(parents=True)
    (package / "api.py").write_text("x = 1\n", encoding="utf-8")
    xml = _XML.replace(
        "<sources><source>/repo/custom_components/googlefindmy</source></sources>",
        "<sources>"
        f"<source>{package / 'custom_components' / 'googlefindmy'}</source>"
        f"<source>{package}</source>"
        "</sources>",
    )

    parsed = diff_coverage.parse_coverage_xml(xml, tmp_path)

    assert set(parsed) == {"custom_components/googlefindmy/api.py"}


def test_a_non_numeric_line_record_is_a_measurement_error() -> None:
    """A truncated or foreign report must not raise through the whole preflight."""

    broken = _XML.replace('hits="1"', 'hits=""', 1)

    with pytest.raises(diff_coverage.MeasurementError) as raised:
        diff_coverage.parse_coverage_xml(broken, REPO)

    assert "non-numeric" in str(raised.value)


def test_a_content_line_is_not_mistaken_for_a_file_header() -> None:
    """A removed "-- note" and an added "++ note" must not start a file section."""

    diff = (
        "diff --git a/custom_components/googlefindmy/api.py b/custom_components/googlefindmy/api.py\n"
        "--- a/custom_components/googlefindmy/api.py\n"
        "+++ b/custom_components/googlefindmy/api.py\n"
        "@@ -1 +10,1 @@\n"
        "--- note\n"
        "+++ note\n"
        "@@ -2 +11,1 @@\n"
    )

    changed = diff_coverage.parse_diff(diff)

    assert changed == {"custom_components/googlefindmy/api.py": {10, 11}}


def test_a_quoted_path_is_unquoted() -> None:
    """git quotes paths with spaces; the quotes never match a coverage entry."""

    diff = (
        'diff --git a/x "b/custom_components/googlefindmy/a b.py"\n'
        '--- a/x\n+++ "b/custom_components/googlefindmy/a b.py"\n@@ -1 +3,1 @@\n'
    )

    assert diff_coverage.parse_diff(diff) == {
        "custom_components/googlefindmy/a b.py": {3}
    }
