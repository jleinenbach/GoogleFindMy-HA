# tests/test_main_reauth_overwrite_warning.py
"""``--reauth`` warns before it replaces a bundle Home Assistant may not have imported.

There is one ``secrets.json``. A second run for another account goes through
``--reauth``, which clears the file and lets the sign-in write new keys; a
bundle Home Assistant had not imported yet was gone without a word.
``main._warn_before_replacing_unconsumed_bundle`` now prints a warning before
the first write and, at an attended terminal, waits for Enter; Ctrl+C or the
end of input ends the run with status 130 and leaves the file as it was.
"""

from __future__ import annotations

import builtins
import io
import json
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy import main as cli_main
from custom_components.googlefindmy.Auth.auth_flow import _ENV_ASSUME_INTERACTIVE
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import (
    _ENV_CLI_PROCESS,
)

_BUNDLE = {
    "username": "user@example.com",
    "oauth_token": "oauth2_4/OLD",
    "aas_token": "aas_et/OLD",
    "shared_key": "bb" * 32,
}

_WARNING = "still holds a bundle for u***@example.com."
_PROMPT = "Press Enter to continue, or Ctrl+C to stop. "
_STOPPED = "Stopped. Nothing was changed."


class _Terminal(io.StringIO):
    """An stdin that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
    *,
    terminal: bool = False,
    container: bool = True,
) -> Path:
    path = tmp_path / "secrets.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(path))
    monkeypatch.setenv(_ENV_CLI_PROCESS, "1")  # _main sets it; restored afterwards
    if container:
        monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", "1")
    else:
        monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)
    monkeypatch.setattr(cli_main, "_configure_cli_logging", lambda **kw: None)
    monkeypatch.setattr(sys, "stdin", _Terminal("") if terminal else io.StringIO(""))
    monkeypatch.delenv(_ENV_ASSUME_INTERACTIVE, raising=False)
    return path


def _stop_at_sign_in(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for sign-in 1 of 2; tests/AGENTS.md: never reach the real asyncio.run."""
    reached: list[str] = []

    def _sign_in_one() -> None:
        reached.append("sign-in")
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli_main, "_ensure_authenticated", _sign_in_one)
    return reached


def test_the_warning_comes_before_the_first_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE))
    reached = _stop_at_sign_in(monkeypatch)
    real_write = cli_main._atomic_write_json
    stderr_before_each_write: list[str] = []

    def _spy(path: Path, data: dict[str, Any]) -> None:
        stderr_before_each_write.append(capsys.readouterr().err)
        real_write(path, data)

    monkeypatch.setattr(cli_main, "_atomic_write_json", _spy)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    assert reached == ["sign-in"]
    assert stderr_before_each_write, "the run never wrote the file"
    first = stderr_before_each_write[0]
    assert _WARNING in first
    assert "data/secrets.json next to the launcher" in first
    assert "user@example.com" not in "".join(stderr_before_each_write)


def test_at_a_terminal_enter_goes_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE), terminal=True)
    reached = _stop_at_sign_in(monkeypatch)
    prompts: list[str] = []

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(builtins, "input", _input)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    assert prompts == [_PROMPT]
    assert reached == ["sign-in"]


def test_an_ide_console_that_says_someone_is_there_is_asked_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same opt-in as the sign-in prompts: a piped console with the switch set."""
    _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE))
    monkeypatch.setenv(_ENV_ASSUME_INTERACTIVE, "1")
    reached = _stop_at_sign_in(monkeypatch)
    prompts: list[str] = []

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(builtins, "input", _input)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    assert prompts == [_PROMPT]
    assert reached == ["sign-in"]


def test_a_closed_input_counts_as_nobody_there(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``isatty`` on a closed stream raises; the run warns and goes on instead."""
    _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE))
    closed = io.StringIO("")
    closed.close()
    monkeypatch.setattr(sys, "stdin", closed)
    reached = _stop_at_sign_in(monkeypatch)

    def _input(prompt: str = "") -> str:
        raise AssertionError("asked although nobody can answer")

    monkeypatch.setattr(builtins, "input", _input)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    assert reached == ["sign-in"]
    assert _WARNING in capsys.readouterr().err


@pytest.mark.parametrize("stop", [KeyboardInterrupt, EOFError])
def test_ctrl_c_or_end_of_input_stops_with_130_and_keeps_the_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stop: type[BaseException],
) -> None:
    path = _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE), terminal=True)
    before = path.read_bytes()
    reached = _stop_at_sign_in(monkeypatch)

    def _input(prompt: str = "") -> str:
        raise stop

    monkeypatch.setattr(builtins, "input", _input)
    sigint_before = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(SystemExit) as ended:
            cli_main._main(["--reauth"])
        # A second Ctrl+C must not cut the message short.
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, sigint_before)

    assert ended.value.code == cli_main._EXIT_LOGIN_ABORTED == 130
    assert path.read_bytes() == before
    assert reached == []
    assert _STOPPED in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(json.dumps({"username": "user@example.com"}), id="no-bundle"),
        pytest.param(
            json.dumps({"username": "user@example.com", "shared_key": "  "}),
            id="blank-bundle",
        ),
        pytest.param("{not json", id="broken"),
        pytest.param(json.dumps(["shared_key"]), id="not-an-object"),
    ],
)
def test_no_warning_without_a_usable_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    _prepare(monkeypatch, tmp_path, content, terminal=True)
    reached = _stop_at_sign_in(monkeypatch)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    assert reached == ["sign-in"]
    assert "still holds a bundle" not in capsys.readouterr().err


def test_no_warning_when_the_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _prepare(monkeypatch, tmp_path, "{}", terminal=True)
    path.unlink()
    reached = _stop_at_sign_in(monkeypatch)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    assert reached == ["sign-in"]
    assert "still holds a bundle" not in capsys.readouterr().err


def test_no_warning_without_reauth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE), terminal=True)
    reached = _stop_at_sign_in(monkeypatch)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main([])

    assert reached == ["sign-in"]
    assert "still holds a bundle" not in capsys.readouterr().err


def test_the_older_per_user_key_counts_and_a_missing_name_stays_unnamed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Outside the container the warning names the path; no username, no name."""
    path = _prepare(
        monkeypatch,
        tmp_path,
        json.dumps({"shared_key_user@example.com": "bb" * 32}),
        container=False,
    )
    _stop_at_sign_in(monkeypatch)

    with pytest.raises(RuntimeError, match="stop here"):
        cli_main._main(["--reauth"])

    err = capsys.readouterr().err
    assert f"The credential file ({path}) still holds a bundle for <unknown>." in err


def test_the_guide_quotes_the_warning_and_the_stop_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``## Forcing a fresh login`` quotes both messages, each after its own lead-in.

    Both are rendered through the real function (AGENTS.md: a quoted message is
    guarded by its code path), and the status comes from the code, not the prose.
    """
    from tests.test_auth_flow import _documented_quotes

    _prepare(monkeypatch, tmp_path, json.dumps(_BUNDLE), terminal=True)

    def _input(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", _input)
    sigint_before = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(SystemExit) as ended:
            cli_main._warn_before_replacing_unconsumed_bundle()
    finally:
        signal.signal(signal.SIGINT, sigint_before)
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.strip()]
    stopped = " ".join(lines[-1].split())
    warning = " ".join(" ".join(lines[:-1]).split())

    quotes = _documented_quotes("Forcing a fresh login")
    warning_lead = (
        "a `--reauth` run warns before it changes anything; the account is masked:"
    )
    stop_lead = "and this line, and the file stays as it was:"
    after_warning = [block for prose, block in quotes if prose.endswith(warning_lead)]
    after_stop = [
        (prose, block) for prose, block in quotes if prose.endswith(stop_lead)
    ]
    assert after_warning == [warning], (after_warning, warning)
    assert len(after_stop) == 1, after_stop
    prose, block = after_stop[0]
    assert block == stopped
    assert f"`{ended.value.code}`" in prose
