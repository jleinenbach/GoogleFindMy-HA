# tests/test_precommit_ruff_format_hook.py
"""Unit tests for the custom Ruff format pre-commit wrapper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from script.precommit_hooks import ruff_format


def test_collect_changed_files_runs_git_diff_for_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the helper queries git diff when filenames are provided."""

    recorded: list[list[str]] = []

    def _fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        command = args[0]
        recorded.append(command)
        return SimpleNamespace(returncode=0, stdout="first.py\nsecond.py\n")

    monkeypatch.setattr(ruff_format.subprocess, "run", _fake_run)

    changed = ruff_format._collect_changed_files(["first.py", "second.py"])

    assert recorded == [["git", "diff", "--name-only", "--", "first.py", "second.py"]]
    assert changed == ["first.py", "second.py"]


def test_collect_changed_files_skips_diff_without_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not call git diff when no filenames are supplied."""

    called = False

    def _fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(ruff_format.subprocess, "run", _fake_run)

    assert ruff_format._collect_changed_files([]) == []
    assert not called
