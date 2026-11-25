# tests/test_docker_login_partial_bundle_notice.py
"""A login that ends early names the incomplete credential file it left.

A run cut short (closed window, Ctrl+C, a failed key step) can leave a
``secrets.json`` with tokens but no ``shared_key``. Home Assistant does not offer
such a file, so the user found a file on disk, saw no card and could not tell
why (L-4b). ``entrypoint.sh`` now prints where the file is and what to do with
it, from ``cleanup``: the EXIT trap is the one place every exit passes through,
including the immediate exit that ``exit_if_terminating`` takes for a status
above 128 (a closed login window ends ``main.py`` with 130).

The functions are executed, not grepped. They are cut out with
``_extract_shell_function``; because that helper has no brakes of its own
(``tests/AGENTS.md``, "Shell fragment extraction"), ``_function`` below adds a
``must_contain`` check and a line cap before anything runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_docker_login_hardening import _extract_shell_function, _read

_NOTICE_NEEDLES = ('"shared_key"', "data/secrets.json", ">&2", "_login_ran")
_CLEANUP_NEEDLES = ("local status=$?", 'notice_partial_bundle "${status}"')


def _function(name: str, needles: tuple[str, ...], max_lines: int) -> str:
    block = _extract_shell_function(_read("entrypoint.sh"), name)
    lines = block.splitlines()
    if len(lines) > max_lines:
        raise AssertionError(
            f"{name}: grabbed {len(lines)} lines (cap {max_lines}); refusing to run it"
        )
    missing = [n for n in needles if n not in block]
    if missing:
        raise AssertionError(f"{name}: extracted block no longer contains {missing!r}")
    return block


def _run(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "GOOGLEFINDMY_SECRETS_PATH": str(tmp_path / "secrets.json"),
        },
    )


def _notice(
    tmp_path: Path, *, status: int, content: str | None, login_ran: bool = True
) -> subprocess.CompletedProcess[str]:
    block = _function("notice_partial_bundle", _NOTICE_NEEDLES, 25)
    if content is not None:
        (tmp_path / "secrets.json").write_text(content, encoding="utf-8")
    ran = "1" if login_ran else ""
    script = (
        f"set -e\n_login_ran={ran}\n{block}\n"
        f"notice_partial_bundle {status}\necho reached-end\n"
    )
    return _run(script, tmp_path)


def test_a_failed_run_names_the_partial_bundle(tmp_path: Path) -> None:
    """Status 130 with a file that has tokens but no shared_key."""
    result = _notice(
        tmp_path, status=130, content='{"aas_token": "aas_et/X", "username": "u"}'
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reached-end"  # nothing else on stdout
    assert "data/secrets.json" in result.stderr
    assert "delete" in result.stderr
    assert "Home Assistant does not offer" in result.stderr
    assert str(tmp_path / "secrets.json") in result.stderr


@pytest.mark.parametrize(
    ("status", "content", "login_ran"),
    [
        pytest.param(0, '{"aas_token": "x"}', True, id="success-without-key"),
        pytest.param(
            130, '{"aas_token": "x", "shared_key": "aa"}', True, id="complete"
        ),
        pytest.param(1, None, True, id="no-file"),
        pytest.param(130, '{"aas_token": "x"}', False, id="login-never-started"),
    ],
)
def test_no_notice_when_this_run_left_nothing_half_done(
    tmp_path: Path, status: int, content: str | None, login_ran: bool
) -> None:
    """Success, a complete bundle, no file, or a file from an earlier run."""
    result = _notice(tmp_path, status=status, content=content, login_ran=login_ran)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.strip() == "reached-end"


def test_the_exit_trap_prints_the_notice_and_keeps_the_exit_status(
    tmp_path: Path,
) -> None:
    """The real cleanup, as the EXIT trap, after an exit with 130.

    This is the path exit_if_terminating takes for a closed login window. The
    notice has to appear there, and the launcher must still see 130.
    """
    notice = _function("notice_partial_bundle", _NOTICE_NEEDLES, 25)
    cleanup = _function("cleanup", _CLEANUP_NEEDLES, 30)
    (tmp_path / "secrets.json").write_text('{"aas_token": "x"}', encoding="utf-8")
    script = (
        "set -e\n_cleaned=0\nSUPERVISOR_PID=\n_login_ran=1\n"
        f"{notice}\n{cleanup}\ntrap cleanup EXIT\nexit 130\n"
    )
    result = _run(script, tmp_path)

    assert result.returncode == 130
    assert "data/secrets.json" in result.stderr
    assert result.stderr.count("The sign-in stopped before it finished") == 1


def test_the_marker_is_set_right_after_the_login_starts() -> None:
    """Set any earlier and a stale file from a previous run would be reported."""
    lines = _read("entrypoint.sh").splitlines()
    launch = [i for i, ln in enumerate(lines) if "exec python3 main.py" in ln]
    assert len(launch) == 1, "expected exactly one main.py launch line"
    assert lines[launch[0] + 1 : launch[0] + 3] == ["CHILD_PID=$!", "_login_ran=1"]
    assert sum(ln.strip() == "_login_ran=1" for ln in lines) == 1
