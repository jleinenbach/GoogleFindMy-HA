# tests/test_auth_flow.py
import sys
from collections.abc import Callable
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


def test_request_oauth_flow_prints_when_not_home_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-headless, non-HA mode walks the interactive print/input branches."""

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    # Ensure the HA detection path is False so the print branches execute.
    monkeypatch.delitem(sys.modules, "homeassistant", raising=False)
    # Make email extraction succeed so the "Detected account" branch runs too.
    monkeypatch.setattr(
        auth_flow, "_extract_email_from_session", lambda _: "user@example.com"
    )

    token, email = request_oauth_account_token_flow(headless=False)

    assert token == "tok"
    assert email == "user@example.com"


def test_request_oauth_flow_non_ha_without_email_skips_account_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-HA mode with no detected email skips the 'Detected account' print."""

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.delitem(sys.modules, "homeassistant", raising=False)
    monkeypatch.setattr(auth_flow, "_extract_email_from_session", lambda _: None)

    token, email = request_oauth_account_token_flow(headless=False)

    assert token == "tok"
    assert email is None


def test_request_oauth_flow_home_assistant_context_skips_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``homeassistant`` is imported, prompts and prints are skipped."""

    driver = FakeDriver(cookie_after_wait={"value": "tok"})
    monkeypatch.setattr(auth_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", ImmediateWaitFactory())
    # Force the HA detection branch.
    monkeypatch.setitem(sys.modules, "homeassistant", object())

    token, _email = request_oauth_account_token_flow(headless=False)

    assert token == "tok"


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
    monkeypatch.delitem(sys.modules, "homeassistant", raising=False)
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
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    monkeypatch.delitem(sys.modules, "homeassistant", raising=False)
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
    monkeypatch.setitem(sys.modules, "homeassistant", object())
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
