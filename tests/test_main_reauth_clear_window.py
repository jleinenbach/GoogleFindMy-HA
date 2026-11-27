# tests/test_main_reauth_clear_window.py
"""``--reauth`` keeps the account when a stop lands between clearing and restore.

``--reauth`` clears the cached tokens with one write and only then hands
``_main`` the callable that puts them back. The SIGTERM handler used to be set
after that, so a stop in between met SIGTERM's default and the tokens stayed
gone. ``main._hold_stop_signals`` now holds SIGTERM and SIGINT during the
clearing and delivers them once the callable is assigned, and the handler is
in place before the clearing starts. ``_raise_on_sigterm`` also no longer
refuses the sign-in outside the main thread (review of PR #1485, findings 1
and 2).
"""

from __future__ import annotations

import io
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
    / "reauth_clear_window.py"
)

_CACHED = {
    "username": "user@example.com",
    "oauth_token": "oauth2_4/OLD",
    "aas_token": "aas_et/OLD",
    "shared_key": "bb" * 32,
}

_STOP_LINE = "The login was stopped (SIGTERM) during sign-in 1 of 2."


def _write(tmp_path: Path, data: dict[str, str]) -> Path:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_stop_right_after_the_clearing_puts_the_tokens_back(
    tmp_path: Path,
    cli_sandbox: _CliSandbox,  # noqa: F811 - the fixture imported above
) -> None:
    """A real process that gets SIGTERM right after the clearing write keeps its tokens."""
    path = _write(tmp_path, _CACHED)
    assert cli_sandbox.env["GOOGLEFINDMY_SECRETS_PATH"] == str(path)
    root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, str(_PROBE)],
        cwd=root,
        env=cli_sandbox.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = child.communicate(timeout=120)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    assert child.returncode == 143, err[-2000:]
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
    # Where it stopped: after the clearing, before sign-in 1 of 2.
    assert "SENDING_SIGTERM" in err
    assert "Cleared 3 cached token(s)." in out
    assert "SIGN_IN_1_REACHED" not in err
    assert _STOP_LINE in err
    assert "Login did not complete; restored 3 cached token(s)." in out
    _assert_no_process_kill(cli_sandbox)


def _prepare_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict[str, str]
) -> Path:
    path = _write(tmp_path, data)
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(path))
    monkeypatch.setenv(_ENV_CLI_PROCESS, "1")  # _main sets it; restored afterwards
    monkeypatch.setattr(cli_main, "_configure_cli_logging", lambda **kw: None)
    # No terminal: the overwrite warning must not wait for Enter (pytest -s).
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return path


def test_reauth_with_nothing_to_clear_still_ends_with_143_and_the_stop_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handler no longer depends on there being tokens to put back."""
    path = _prepare_main(monkeypatch, tmp_path, {"username": "user@example.com"})

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

    assert ended == [143]
    assert _STOP_LINE in err
    assert "restored" not in out
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "username": "user@example.com"
    }


def test_reauth_outside_the_main_thread_still_signs_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``signal.signal`` refuses outside the main thread; the sign-in must run anyway."""
    _prepare_main(monkeypatch, tmp_path, _CACHED)
    signed_in: list[str] = []
    errors: list[BaseException] = []

    def _sign_in_one() -> None:
        signed_in.append(threading.current_thread().name)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli_main, "_ensure_authenticated", _sign_in_one)

    def _run() -> None:
        try:
            cli_main._main(["--reauth"])
        except BaseException as err:  # noqa: BLE001 - recorded for the assert
            errors.append(err)

    worker = threading.Thread(target=_run, name="not-main")
    worker.start()
    worker.join(timeout=60)

    assert signed_in == ["not-main"]
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def _recorders() -> tuple[list[int], object, object]:
    seen: list[int] = []

    def _term(signum: int, frame: FrameType | None) -> None:
        seen.append(signum)

    def _int(signum: int, frame: FrameType | None) -> None:
        seen.append(signum)

    return seen, _term, _int


def test_held_signals_arrive_once_after_the_block() -> None:
    seen, term, intr = _recorders()
    before_term = signal.signal(signal.SIGTERM, term)  # type: ignore[arg-type]
    before_int = signal.signal(signal.SIGINT, intr)  # type: ignore[arg-type]
    try:
        with cli_main._hold_stop_signals():
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
            assert seen == []
        assert seen == [signal.SIGTERM, signal.SIGINT]
        assert signal.getsignal(signal.SIGTERM) is term
        assert signal.getsignal(signal.SIGINT) is intr
    finally:
        signal.signal(signal.SIGTERM, before_term)
        signal.signal(signal.SIGINT, before_int)


def test_the_handlers_come_back_when_the_held_block_raises() -> None:
    seen, term, intr = _recorders()
    before_term = signal.signal(signal.SIGTERM, term)  # type: ignore[arg-type]
    before_int = signal.signal(signal.SIGINT, intr)  # type: ignore[arg-type]
    try:
        with pytest.raises(RuntimeError, match="clearing failed"):
            with cli_main._hold_stop_signals():
                assert signal.getsignal(signal.SIGTERM) is not term
                raise RuntimeError("clearing failed")
        assert signal.getsignal(signal.SIGTERM) is term
        assert signal.getsignal(signal.SIGINT) is intr
        assert seen == []
    finally:
        signal.signal(signal.SIGTERM, before_term)
        signal.signal(signal.SIGINT, before_int)


def test_outside_the_main_thread_nothing_is_held() -> None:
    entered: list[bool] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            with cli_main._hold_stop_signals():
                entered.append(True)
        except BaseException as err:  # noqa: BLE001 - recorded for the assert
            errors.append(err)

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout=30)

    assert entered == [True]
    assert errors == []


def test_raise_on_sigterm_outside_the_main_thread_runs_the_block_unguarded() -> None:
    entered: list[object] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            with cli_main._raise_on_sigterm():
                entered.append(signal.getsignal(signal.SIGTERM))
        except BaseException as err:  # noqa: BLE001 - recorded for the assert
            errors.append(err)

    before = signal.getsignal(signal.SIGTERM)
    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout=30)

    assert entered == [before]
    assert errors == []


def test_dropped_signals_are_not_delivered_but_the_handlers_come_back() -> None:
    """``deliver=False`` holds the signals like the default, then forgets them."""
    seen, term, intr = _recorders()
    before_term = signal.signal(signal.SIGTERM, term)  # type: ignore[arg-type]
    before_int = signal.signal(signal.SIGINT, intr)  # type: ignore[arg-type]
    try:
        with cli_main._hold_stop_signals(deliver=False):
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
        assert seen == []
        assert signal.getsignal(signal.SIGTERM) is term
        assert signal.getsignal(signal.SIGINT) is intr
    finally:
        signal.signal(signal.SIGTERM, before_term)
        signal.signal(signal.SIGINT, before_int)


def _second_stop_during_the_restore(
    monkeypatch: pytest.MonkeyPatch, *stops: signal.Signals
) -> list[dict[str, str]]:
    """Raise ``stops`` inside the restore write, as the container's second Ctrl+C would."""
    real_write = cli_main._atomic_write_json
    writes: list[dict[str, str]] = []

    def _write(target: Path, data: dict[str, str]) -> None:
        writes.append(dict(data))
        if len(writes) == 2:  # the first write clears, the second restores
            for stop in stops:
                signal.raise_signal(stop)
        real_write(target, data)

    monkeypatch.setattr(cli_main, "_atomic_write_json", _write)
    return writes


def _run_main(argv: list[str]) -> BaseException | None:
    # A KeyboardInterrupt must not reach pytest itself: it would end the session.
    try:
        cli_main._main(argv)
    except BaseException as err:  # noqa: BLE001 - the outcome is the assert
        return err
    return None


def test_a_second_stop_during_the_restore_neither_cuts_it_short_nor_changes_the_ending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """docker stop in sign-in 1, then a Ctrl+C and another SIGTERM during the restore.

    SIGTERM points at a recorder before ``_main``: ``_raise_on_sigterm`` puts
    its predecessor back before the restore, so without the hold a SIGTERM
    there reaches the recorder instead of ending pytest.
    """
    path = _prepare_main(monkeypatch, tmp_path, _CACHED)
    writes = _second_stop_during_the_restore(monkeypatch, signal.SIGINT, signal.SIGTERM)
    seen, term, _intr = _recorders()
    ended: list[int] = []

    def _end(code: int) -> None:
        ended.append(code)
        raise SystemExit(code)

    def _sign_in_one() -> None:
        raise cli_main._LoginTerminated

    monkeypatch.setattr(cli_main, "_ensure_authenticated", _sign_in_one)
    monkeypatch.setattr(cli_main, "_end_process_now", _end)
    before_term = signal.signal(signal.SIGTERM, term)  # type: ignore[arg-type]
    before_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        outcome = _run_main(["--reauth"])
    finally:
        signal.signal(signal.SIGTERM, before_term)
        signal.signal(signal.SIGINT, before_int)
    out, err = capsys.readouterr()

    assert isinstance(outcome, SystemExit), repr(outcome)
    assert ended == [143]  # not 130: the held Ctrl+C did not take over
    assert seen == []
    assert len(writes) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
    assert "Login did not complete; restored 3 cached token(s)." in out
    assert _STOP_LINE in err


def test_the_containers_second_ctrl_c_does_not_abort_the_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C in sign-in 1, the entrypoint's forwarded one during the restore."""
    path = _prepare_main(monkeypatch, tmp_path, _CACHED)
    _second_stop_during_the_restore(monkeypatch, signal.SIGINT)

    def _sign_in_one() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "_ensure_authenticated", _sign_in_one)
    before_int = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        outcome = _run_main(["--reauth"])
    finally:
        signal.signal(signal.SIGINT, before_int)
    out, _err = capsys.readouterr()

    assert isinstance(outcome, KeyboardInterrupt), repr(outcome)
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
    assert "Login did not complete; restored 3 cached token(s)." in out


class _BrokenStdout(io.StringIO):
    def write(self, s: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_failing_print_after_the_clearing_still_returns_the_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tokens are gone once the write is done; the callable must come back."""
    path = _prepare_main(monkeypatch, tmp_path, _CACHED)
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())

    restore = cli_main._clear_stale_tokens_for_reauth()

    assert restore is not None
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "username": "user@example.com"
    }
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    restore()
    assert json.loads(path.read_text(encoding="utf-8")) == _CACHED
