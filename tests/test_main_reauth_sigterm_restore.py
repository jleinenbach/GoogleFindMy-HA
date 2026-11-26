# tests/test_main_reauth_sigterm_restore.py
"""``--reauth`` puts the cleared tokens back when SIGTERM stops sign-in 1 of 2.

``--reauth`` clears the cached tokens before sign-in 1 of 2, because an empty
cache is what opens the login. A login that ends without a new token puts them
back. Sign-in 1 of 2 runs before any event loop, so ``docker stop`` used to end
the process through SIGTERM's default, the restore never ran, and the account
was left signed out. ``main._raise_on_sigterm`` turns the signal into an
exception for exactly that block.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import FrameType

import pytest

from custom_components.googlefindmy import main as cli_main
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import (
    _ENV_CLI_PROCESS,
)

# tests/AGENTS.md: a test that spawns the CLI runs it through ``cli_sandbox``
# and asserts ``_assert_no_process_kill``. The fixture lives in test_main.py;
# importing it here makes pytest offer it to this module as well.
from tests.test_main import _assert_no_process_kill, _CliSandbox, cli_sandbox  # noqa: F401

_PROBE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "stop_probe"
    / "reauth_sign_in_one.py"
)

# Three keys that --reauth clears, beside the username it keeps.
_CACHED = {
    "username": "user@example.com",
    "oauth_token": "oauth2_4/OLD",
    "aas_token": "aas_et/OLD",
    "shared_key": "bb" * 32,
}

_STOP_LINE = "The login was stopped (SIGTERM) during sign-in 1 of 2."


def _write_cache(tmp_path: Path) -> Path:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(_CACHED), encoding="utf-8")
    return path


def test_a_stop_during_sign_in_one_puts_the_cleared_tokens_back(
    tmp_path: Path,
    cli_sandbox: _CliSandbox,  # noqa: F811 - the fixture imported above
) -> None:
    """A real process, stopped from outside, ends with 143 and keeps the account.

    Only a real interpreter shows what SIGTERM does to a process that is not
    inside an event loop, so the caller sends the signal the way ``docker
    stop`` does, once the stand-in for the Chrome window reports that it waits.
    """
    path = _write_cache(tmp_path)
    # The sandbox points GOOGLEFINDMY_SECRETS_PATH at this very file.
    assert cli_sandbox.env["GOOGLEFINDMY_SECRETS_PATH"] == str(path)
    root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, str(_PROBE)],
        cwd=root,
        env=cli_sandbox.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # readline blocks; a child that hangs before its ready line must not hang
    # the suite with it.
    guard = threading.Timer(120, child.kill)
    guard.start()
    try:
        assert child.stderr is not None
        seen: list[str] = []
        line = child.stderr.readline()
        while line and line.strip() != "SIGN_IN_1_WAITING":
            seen.append(line)
            line = child.stderr.readline()
        assert line.strip() == "SIGN_IN_1_WAITING", "".join(seen)[-2000:]
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=60)
    finally:
        guard.cancel()
        if child.poll() is None:
            child.kill()
            child.wait()

    assert child.returncode == 143, err[-2000:]
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
    assert "BROWSER_CLOSED" in err  # the stop unwound through the browser's finally
    assert _STOP_LINE in err
    assert "Login did not complete; restored 3 cached token(s)." in out
    _assert_no_process_kill(cli_sandbox)


def test_the_previous_handler_comes_back_even_when_the_block_raises() -> None:
    def _sentinel(signum: int, frame: FrameType | None) -> None:
        """Stand in for whatever handler was there before."""

    before = signal.signal(signal.SIGTERM, _sentinel)
    try:
        with pytest.raises(RuntimeError, match="sign-in failed"):
            with cli_main._raise_on_sigterm():
                assert signal.getsignal(signal.SIGTERM) is not _sentinel
                raise RuntimeError("sign-in failed")
        assert signal.getsignal(signal.SIGTERM) is _sentinel
    finally:
        signal.signal(signal.SIGTERM, before)


def test_inside_the_block_sigterm_raises_login_terminated() -> None:
    with pytest.raises(cli_main._LoginTerminated):
        with cli_main._raise_on_sigterm():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)


def _stop_in_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, list[int], str, str]:
    """Run ``_main(["--reauth"])`` and stop it in sign-in 1 of 2, in-process.

    The handler is called directly instead of sending a signal to the test
    process; ``_end_process_now`` is replaced, because the real one ends the
    interpreter.
    """
    path = _write_cache(tmp_path)
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(path))
    monkeypatch.setenv(_ENV_CLI_PROCESS, "1")  # _main sets it; restored afterwards
    monkeypatch.setattr(cli_main, "_configure_cli_logging", lambda **kw: None)

    def _sign_in_one() -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    ended: list[int] = []

    def _end(code: int) -> None:
        ended.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(cli_main, "_ensure_authenticated", _sign_in_one)
    monkeypatch.setattr(cli_main, "_end_process_now", _end)

    with pytest.raises(SystemExit):
        cli_main._main(["--reauth"])
    out, err = capsys.readouterr()
    return path, ended, out, err


def test_main_puts_the_tokens_back_and_ends_with_143(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wiring in ``_main``: restore first, then status 143."""
    path, ended, out, err = _stop_in_process(monkeypatch, tmp_path, capsys)

    assert ended == [cli_main._EXIT_TERMINATED] == [143]
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
    assert "Login did not complete; restored 3 cached token(s)." in out
    assert _STOP_LINE in err


# docker-login/README.md quotes the sign-in 1 of 2 stop line under
# "Cancelling a login", in the fenced block right after its own lead-in.
_SIGN_IN_ONE_LEAD_IN = "and then ends with exit status `143` and this line:"


def test_the_guide_quotes_the_sign_in_one_stop_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The guide shows the line a stop in sign-in 1 of 2 prints, and its status.

    The line is rendered through ``_main``, so a change of its wording, or of
    ``_EXIT_TERMINATED``, fails here until the guide follows.
    """
    from tests.test_auth_flow import _documented_quotes

    _path, ended, _out, err = _stop_in_process(monkeypatch, tmp_path, capsys)
    printed = [" ".join(line.split()) for line in err.splitlines() if "SIGTERM" in line]
    assert len(printed) == 1, err

    quoted = [
        (prose, block)
        for prose, block in _documented_quotes()
        if prose.endswith(_SIGN_IN_ONE_LEAD_IN)
    ]
    assert len(quoted) == 1, (
        f"expected exactly one fenced block right after {_SIGN_IN_ONE_LEAD_IN!r} "
        f"in '## Cancelling a login', found {len(quoted)}"
    )
    prose, block = quoted[0]
    assert block == printed[0], f"Block: {block!r}. Printed: {printed[0]!r}"
    assert f"`{ended[0]}`" in prose


def test_without_reauth_sigterm_keeps_its_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--reauth`` there is nothing to put back, so nothing is installed."""
    path = _write_cache(tmp_path)
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(path))
    monkeypatch.setenv(_ENV_CLI_PROCESS, "1")
    monkeypatch.setattr(cli_main, "_configure_cli_logging", lambda **kw: None)
    before = signal.getsignal(signal.SIGTERM)
    seen: list[object] = []

    def _sign_in_one() -> None:
        seen.append(signal.getsignal(signal.SIGTERM))
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli_main, "_ensure_authenticated", _sign_in_one)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main([])

    assert seen == [before]
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
