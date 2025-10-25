# tests/test_local_verify_script.py
"""Unit tests for the consolidated local verification helper script."""

from __future__ import annotations

from types import SimpleNamespace
from collections.abc import Sequence

import pytest

from script import local_verify


@pytest.fixture
def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[Sequence[str]]:
    """Intercept subprocess.run calls and capture the invoked commands."""

    executed: list[Sequence[str]] = []

    def _fake_run(command: Sequence[str], *, check: bool = False) -> SimpleNamespace:
        executed.append(tuple(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(local_verify.subprocess, "run", _fake_run)
    return executed


def test_default_commands_include_ruff_and_pytest(
    _capture_run: list[Sequence[str]],
) -> None:
    """Ensure the helper runs Ruff format --check before pytest."""

    exit_code = local_verify.main([])

    assert exit_code == 0
    assert len(_capture_run) == 2

    ruff_command = list(_capture_run[0])
    assert ruff_command[0] == local_verify.sys.executable
    assert str(local_verify.RUFF_FORMAT_SCRIPT) in ruff_command
    assert "--check" in ruff_command
    assert not any(arg.startswith("--exclude=") for arg in ruff_command)
    pytest_command = list(_capture_run[1])
    assert pytest_command[0] == "pytest"


def test_pytest_still_runs_when_ruff_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruff failures should not prevent pytest from executing."""

    recorded: list[Sequence[str]] = []

    def _sequenced_run(
        command: Sequence[str], *, check: bool = False
    ) -> SimpleNamespace:
        recorded.append(tuple(command))
        return SimpleNamespace(returncode=1 if not recorded[:-1] else 0)

    monkeypatch.setattr(local_verify.subprocess, "run", _sequenced_run)

    exit_code = local_verify.main([])

    assert exit_code == 1
    assert len(recorded) == 2
    assert list(recorded[1])[0] == "pytest"
