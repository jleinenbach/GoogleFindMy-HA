# tests/test_auth_flow.py
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

try:  # pragma: no cover - defensive import shim for optional dependency
    import undetected_chromedriver  # type: ignore[unused-ignore]  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - executed in CI without the package
    import sys
    import types

    class _ChromeOptionsStub:
        def __init__(self) -> None:
            self.arguments: list[str] = []
            self.binary_location: str | None = None

        def add_argument(self, argument: str) -> None:
            self.arguments.append(argument)

    def _chrome_stub(*args: Any, **kwargs: Any) -> object:
        return object()

    sys.modules.setdefault(
        "undetected_chromedriver",
        types.SimpleNamespace(ChromeOptions=_ChromeOptionsStub, Chrome=_chrome_stub),
    )


from custom_components.googlefindmy.Auth import auth_flow
from custom_components.googlefindmy.Auth.auth_flow import (
    WebDriverWait,
    create_driver,
    request_oauth_account_token_flow,
)


def _attended(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the flow see an attended terminal.

    Under pytest ``sys.stdin`` is never a tty, so the desktop gate would refuse
    to start. Tests that exercise the *attended* path have to say so explicitly
    rather than rely on the ambient session.
    """

    monkeypatch.setattr(auth_flow, "_stdin_is_attended", lambda: True)


class FakeDriver:
    """Minimal driver that records interactions and exposes canned cookies."""

    def __init__(self, *, cookie_after_wait: dict[str, str] | None) -> None:
        self._cookie_after_wait = cookie_after_wait
        self._wait_observed = False
        self.visited_urls: list[str] = []
        self.cookie_calls: int = 0
        self.quit_calls: int = 0

    def mark_wait_observed(self) -> None:
        self._wait_observed = True

    def get(self, url: str) -> None:
        self.visited_urls.append(url)

    def get_cookie(self, name: str) -> Any:
        assert name == "oauth_token"
        self.cookie_calls += 1
        if not self._wait_observed:
            return None
        return self._cookie_after_wait

    def quit(self) -> None:
        self.quit_calls += 1

    def execute_script(self, script: str) -> Any:  # noqa: ARG002
        return None

    def get_cookies(self) -> list[dict[str, str]]:
        return []


class ImmediateWaitFactory:
    """Replacement for WebDriverWait that immediately evaluates predicates."""

    def __init__(self) -> None:
        self.instances: list[ImmediateWait] = []

    def __call__(self, driver: FakeDriver, timeout: int) -> "ImmediateWait":
        instance = ImmediateWait(driver, timeout)
        self.instances.append(instance)
        return instance


class ImmediateWait:
    def __init__(self, driver: FakeDriver, timeout: int) -> None:
        self.driver = driver
        self.timeout = timeout
        self.until_calls: int = 0

    def until(self, predicate: Callable[[FakeDriver], Any]) -> Any:
        self.until_calls += 1
        self.driver.mark_wait_observed()
        return predicate(self.driver)


def _apply_flow_patches(
    monkeypatch: pytest.MonkeyPatch, driver: FakeDriver
) -> ImmediateWaitFactory:
    wait_factory = ImmediateWaitFactory()
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", wait_factory)
    return wait_factory


def test_request_oauth_account_token_flow_returns_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver(cookie_after_wait={"value": "token123"})
    wait_factory = _apply_flow_patches(monkeypatch, driver)

    token, email = request_oauth_account_token_flow(headless=True)

    assert token == "token123"
    assert email is None  # FakeDriver has no real session
    assert driver.visited_urls == ["https://accounts.google.com/EmbeddedSetup"]
    assert driver.quit_calls == 1
    assert wait_factory.instances and wait_factory.instances[0].until_calls == 1


def test_request_oauth_account_token_flow_missing_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver(cookie_after_wait=None)
    wait_factory = _apply_flow_patches(monkeypatch, driver)

    with pytest.raises(
        RuntimeError, match="OAuth token cookie missing despite wait completion"
    ):
        request_oauth_account_token_flow(headless=True)

    assert driver.quit_calls == 1
    assert wait_factory.instances and wait_factory.instances[0].until_calls == 1


def test_request_oauth_flow_forwards_chrome_path_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow must pass ``chrome_path``/``chrome_version`` to ``create_driver``."""

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    captured: dict[str, Any] = {}

    def fake_create_driver(**kwargs: Any) -> FakeDriver:
        captured.update(kwargs)
        return driver

    monkeypatch.setattr(auth_flow, "create_driver", fake_create_driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())

    request_oauth_account_token_flow(
        headless=True, chrome_path="/opt/chrome", chrome_version=149
    )

    assert captured == {
        "chrome_path": "/opt/chrome",
        "chrome_version": 149,
        "headless": True,
    }


def test_request_oauth_flow_non_string_cookie_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cookie whose ``value`` is not a string is rejected."""

    driver = FakeDriver(cookie_after_wait={"value": 12345})  # type: ignore[dict-item]
    _apply_flow_patches(monkeypatch, driver)

    with pytest.raises(RuntimeError, match="not a string"):
        request_oauth_account_token_flow(headless=True)

    assert driver.quit_calls == 1


def test_stdin_is_attended_reads_the_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tty means attended; everything else -- pipe, None, broken -- does not."""

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    assert auth_flow._stdin_is_attended() is True

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert auth_flow._stdin_is_attended() is False

    monkeypatch.setattr(sys, "stdin", None)
    assert auth_flow._stdin_is_attended() is False

    def _closed() -> bool:
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=_closed))
    assert auth_flow._stdin_is_attended() is False


def test_desktop_gate_refuses_a_pipe_without_consuming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A piped stdin is refused, and its first line is left untouched.

    ``main.py`` reads the account e-mail from the same stdin after the flow
    returns. Consuming a line here would swallow it and leave the later prompt
    with EOF, so the gate must refuse rather than read.
    """

    def _must_not_read(*_args: object, **_kwargs: object) -> str:
        pytest.fail("a non-tty stdin must not be consumed by the gate")

    def _must_not_run(**_kwargs: object) -> object:
        pytest.fail("create_driver must not be reached")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", _must_not_read)
    monkeypatch.setattr(auth_flow, "create_driver", _must_not_run)
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)

    with pytest.raises(RuntimeError, match="not a terminal"):
        request_oauth_account_token_flow(headless=False)


def test_request_oauth_flow_prints_on_the_attended_desktop_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attended desktop path (``headless=False``) walks print and input."""

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    _attended(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    # Make email extraction succeed so the "Detected account" branch runs too.
    monkeypatch.setattr(
        auth_flow, "_extract_email_from_session", lambda _: "user@example.com"
    )

    token, email = request_oauth_account_token_flow(headless=False)

    assert token == "tok"
    assert email == "user@example.com"


def test_request_oauth_flow_without_email_skips_account_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attended path with no detected email skips the 'Detected account' print."""

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    _attended(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.setattr(auth_flow, "_extract_email_from_session", lambda _: None)

    token, email = request_oauth_account_token_flow(headless=False)

    assert token == "tok"
    assert email is None


def test_request_oauth_flow_headless_skips_prompts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``headless=True`` is the automated path: no prints, no stdin gate.

    This used to be driven by ``"homeassistant" in sys.modules``, which was
    unusable as a context signal: a standalone CLI run satisfies it either way
    (installed package or main.py's own stubs), so the gate below never fired
    where it mattered. ``headless`` is the parameter every automated caller
    already passes.
    """

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())

    def _fail_on_input(*_args: object, **_kwargs: object) -> str:
        pytest.fail("the automated path must never block on stdin")

    monkeypatch.setattr("builtins.input", _fail_on_input)

    token, _email = request_oauth_account_token_flow(headless=True)

    assert token == "tok"
    assert capsys.readouterr().out == ""


def test_request_oauth_flow_desktop_gate_aborts_before_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an attended terminal the flow refuses to start Chrome at all.

    Regression for the measured chain: the CLI reached the browser path
    unattended, and its Chrome cleanup then shot the calling process tree
    (exit 143). The gate must abort *before* ``create_driver``, so no browser
    and no process cleanup ever runs in an unattended context. That ordering is
    the point of this test -- ``create_driver`` is a trap, not a stub.
    """

    def _must_not_run(**_kwargs: object) -> object:
        pytest.fail("create_driver must not be reached without an attended terminal")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(auth_flow, "create_driver", _must_not_run)
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)

    with pytest.raises(RuntimeError, match="not a terminal"):
        request_oauth_account_token_flow(headless=False)


def test_request_oauth_flow_gate_survives_a_terminal_closed_mid_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tty check passes, then the terminal goes away: still no browser.

    This is the residual net behind the ``isatty`` check -- the window between
    "stdin is a terminal" and the actual read is small but real (the user closes
    the terminal, the ssh session drops).
    """

    def _must_not_run(**_kwargs: object) -> object:
        pytest.fail("create_driver must not be reached after a failed prompt")

    def _eof(*_args: object, **_kwargs: object) -> str:
        raise EOFError

    _attended(monkeypatch)
    monkeypatch.setattr(auth_flow, "create_driver", _must_not_run)
    monkeypatch.setattr("builtins.input", _eof)
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)

    with pytest.raises(RuntimeError, match="Standard input closed"):
        request_oauth_account_token_flow(headless=False)


def test_request_oauth_flow_desktop_gate_waits_for_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an attended stdin the gate prompts exactly once and then continues."""

    prompts: list[str] = []
    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    _attended(monkeypatch)
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": prompts.append(prompt) or ""
    )
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)

    token, _email = request_oauth_account_token_flow(headless=False)

    assert token == "tok"
    assert len(prompts) == 1
    assert "Press Enter" in prompts[0]


def test_request_oauth_flow_container_shows_novnc_not_desktop_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In the container the prompt names the noVNC URL and never blocks stdin.

    Regression for the docker-login UX fix: with
    ``GOOGLEFINDMY_CONTAINER_LOGIN=1`` set (and the HA heuristic off) the flow
    must print the container-correct instructions -- open the real noVNC URL and
    sign in to the Chrome window that comes up by itself -- and must NOT print
    the desktop 'install Chrome on your system' text, which is wrong when Chrome
    runs inside the container.

    It must also not wait on stdin first: the Enter gate used to run *before*
    ``create_driver``, so the viewer showed an empty desktop until someone
    answered a prompt in the terminal. ``builtins.input`` is therefore patched
    to a fail sentinel -- any call fails this test.
    """

    def _fail_on_input(*_args: object, **_kwargs: object) -> str:
        pytest.fail("container branch must not block on stdin before Chrome starts")

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    monkeypatch.setattr("builtins.input", _fail_on_input)
    monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", "1")
    monkeypatch.setenv("GOOGLEFINDMY_NOVNC_URL", "http://192.168.1.21:7900")
    monkeypatch.setattr(auth_flow, "_extract_email_from_session", lambda _: None)

    token, _email = request_oauth_account_token_flow(headless=False)
    out = capsys.readouterr().out

    assert token == "tok"
    assert "http://192.168.1.21:7900" in out
    assert "Chrome opens by itself" in out
    assert "press Enter" not in out
    assert "installed on your system" not in out


def test_request_oauth_flow_desktop_branch_has_no_container_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without the container signal the desktop text is unchanged.

    Negative guard for the same fix: with ``GOOGLEFINDMY_CONTAINER_LOGIN``
    unset, the interactive branch keeps the original desktop wording and shows
    none of the container-only noVNC instructions.
    """

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    _attended(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)
    monkeypatch.setattr(auth_flow, "_extract_email_from_session", lambda _: None)

    token, _email = request_oauth_account_token_flow(headless=False)
    out = capsys.readouterr().out

    assert token == "tok"
    assert "installed on your system" in out
    assert "noVNC" not in out
    assert "browser view that opens" not in out


# ---------------------------------------------------------------------------
# _parse_cli_args
# ---------------------------------------------------------------------------


def test_parse_cli_args_defaults() -> None:
    args = auth_flow._parse_cli_args([])
    assert args.chrome_path is None
    assert args.chrome_version is None


def test_parse_cli_args_values() -> None:
    args = auth_flow._parse_cli_args(
        ["--chrome-path", "/opt/chrome", "--chrome-version", "149"]
    )
    assert args.chrome_path == "/opt/chrome"
    assert args.chrome_version == 149


def test_parse_cli_args_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        auth_flow._parse_cli_args(["--help"])
    assert excinfo.value.code == 0


def test_parse_cli_args_invalid_version_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        auth_flow._parse_cli_args(["--chrome-version", "x"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# _extract_email_from_session
# ---------------------------------------------------------------------------


class _ScriptDriver:
    """Driver double whose ``execute_script``/``get_cookies`` are configurable."""

    def __init__(
        self,
        *,
        script_results: list[Any] | None = None,
        cookies: list[dict[str, Any]] | None = None,
        script_raises: bool = False,
        cookies_raises: bool = False,
    ) -> None:
        self._script_results = list(script_results or [])
        self._cookies = cookies or []
        self._script_raises = script_raises
        self._cookies_raises = cookies_raises

    def execute_script(self, _script: str) -> Any:
        if self._script_raises:
            raise RuntimeError("script boom")
        if self._script_results:
            return self._script_results.pop(0)
        return None

    def get_cookies(self) -> list[dict[str, Any]]:
        if self._cookies_raises:
            raise RuntimeError("cookies boom")
        return self._cookies


def test_extract_email_from_data_email_attribute() -> None:
    driver = _ScriptDriver(script_results=["user@example.com"])
    assert (
        auth_flow._extract_email_from_session(driver)  # type: ignore[arg-type]
        == "user@example.com"
    )


def test_extract_email_from_cookie_scan() -> None:
    """Both DOM selectors miss, but a cookie value looks like an email."""

    driver = _ScriptDriver(
        script_results=[None, "no-at-sign"],
        cookies=[
            {"value": 123},
            {"value": "plain"},
            {"value": "found@example.com"},
        ],
    )
    assert (
        auth_flow._extract_email_from_session(driver)  # type: ignore[arg-type]
        == "found@example.com"
    )


def test_extract_email_returns_none_when_nothing_matches() -> None:
    driver = _ScriptDriver(
        script_results=[None, None],
        cookies=[{"value": "nope"}],
    )
    assert auth_flow._extract_email_from_session(driver) is None  # type: ignore[arg-type]


def test_extract_email_swallows_script_and_cookie_errors() -> None:
    """Exceptions in both strategies are swallowed and yield ``None``."""

    driver = _ScriptDriver(script_raises=True, cookies_raises=True)
    assert auth_flow._extract_email_from_session(driver) is None  # type: ignore[arg-type]


def test_module_entrypoint_invokes_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the ``__main__`` guard by running the module as a script.

    The freshly executed copy rebinds ``create_driver`` via ``from ... import``,
    so patching the source ``chrome_driver.create_driver`` to raise keeps the
    real browser out of the test while still covering the entry-point lines.
    """

    import runpy

    from custom_components.googlefindmy import chrome_driver

    calls: dict[str, Any] = {}

    def boom(**kwargs: Any) -> Any:
        calls.update(kwargs)
        raise RuntimeError("no chrome in __main__ test")

    monkeypatch.setattr(chrome_driver, "create_driver", boom)
    # The entry point runs the interactive desktop path, so the Enter gate is
    # reached before create_driver: satisfy it instead of letting it abort.
    # ``runpy`` executes a *fresh* copy of the module, so patching the already
    # imported ``auth_flow._stdin_is_attended`` would miss it; patch the stream
    # the fresh copy will look at instead.
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)
    monkeypatch.setattr(sys, "argv", ["auth_flow.py"])

    with pytest.raises(RuntimeError, match="no chrome in __main__ test"):
        runpy.run_path(auth_flow.__file__, run_name="__main__")

    assert calls == {
        "chrome_path": None,
        "chrome_version": None,
        "headless": False,
    }


# Silence unused-import checks while keeping explicit references for clarity.
del WebDriverWait, create_driver
