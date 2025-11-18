# tests/test_precommit_ruff_format_hook.py
"""Unit tests for the custom Ruff format pre-commit wrapper."""

from __future__ import annotations

import pytest

from script.precommit_hooks import ruff_format

EXPECTED_EXIT_CODE = 7


def test_main_injects_force_exclude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force-exclude should be appended when not explicitly disabled."""

    recorded_options: list[list[str]] = []
    recorded_filenames: list[list[str]] = []

    def _fake_run(options, filenames):  # type: ignore[no-untyped-def]
        recorded_options.append(list(options))
        recorded_filenames.append(list(filenames))
        return 0

    monkeypatch.setattr(ruff_format, "_run_ruff", _fake_run)

    exit_code = ruff_format.main(["some.py"])

    assert exit_code == 0
    assert recorded_options == [["--force-exclude"]]
    assert recorded_filenames == [["some.py"]]


def test_main_respects_no_force_exclude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing ``--no-force-exclude`` should avoid injecting ``--force-exclude``."""

    recorded_options: list[list[str]] = []

    def _fake_run(options, filenames):  # type: ignore[no-untyped-def]
        recorded_options.append(list(options))
        return 0

    monkeypatch.setattr(ruff_format, "_run_ruff", _fake_run)

    exit_code = ruff_format.main(["--no-force-exclude", "--", "target.py"])

    assert exit_code == 0
    assert recorded_options == [["--no-force-exclude"]]


def test_main_passes_through_check_without_custom_excludes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure ``--check`` is forwarded without injecting ``--exclude`` options."""

    recorded_options: list[list[str]] = []

    def _fake_run(options, filenames):  # type: ignore[no-untyped-def]
        recorded_options.append(list(options))
        return 0

    monkeypatch.setattr(ruff_format, "_run_ruff", _fake_run)

    exit_code = ruff_format.main(["--check"])

    assert exit_code == 0
    assert recorded_options == [["--check", "--force-exclude"]]


def test_main_propagates_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruff failures should bubble up to the caller."""

    def _fake_run(options, filenames):  # type: ignore[no-untyped-def]
        return 7

    monkeypatch.setattr(ruff_format, "_run_ruff", _fake_run)

    assert ruff_format.main(["config.py"]) == EXPECTED_EXIT_CODE
