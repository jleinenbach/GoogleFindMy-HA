# tests/test_get_oauth_token.py
"""Tests for the standalone OAuth token helper ``get_oauth_token``."""

from __future__ import annotations

import builtins
import sys
from typing import Any

import pytest

from custom_components.googlefindmy import chrome_driver, get_oauth_token


class _FakeDriver:
    """Minimal Selenium driver double recording navigation and cookies."""

    def __init__(self, cookie: dict[str, Any] | None) -> None:
        self._cookie = cookie
        self.visited_urls: list[str] = []
        self.quit_calls = 0

    def get(self, url: str) -> None:
        self.visited_urls.append(url)

    def get_cookie(self, name: str) -> Any:
        assert name == "oauth_token"
        return self._cookie

    def quit(self) -> None:
        self.quit_calls += 1


class _ImmediateWait:
    """WebDriverWait replacement that evaluates the predicate immediately."""

    def __init__(self, driver: _FakeDriver, timeout: int) -> None:
        self.driver = driver
        self.timeout = timeout

    def until(self, predicate: Any) -> Any:
        return predicate(self.driver)


@pytest.fixture(autouse=True)
def _no_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize interactive ``input`` prompts during tests."""

    monkeypatch.setattr(builtins, "input", lambda *args, **kwargs: "")


def _patch_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the immediate WebDriverWait into the selenium support module."""

    import selenium.webdriver.support.ui as ui_module

    monkeypatch.setattr(ui_module, "WebDriverWait", _ImmediateWait)


# ---------------------------------------------------------------------------
# _cookie_value
# ---------------------------------------------------------------------------


def test_cookie_value_returns_none_for_missing_cookie() -> None:
    assert get_oauth_token._cookie_value(None) is None


def test_cookie_value_returns_value() -> None:
    assert get_oauth_token._cookie_value({"value": "token123"}) == "token123"


def test_cookie_value_returns_none_when_value_absent() -> None:
    assert get_oauth_token._cookie_value({}) is None


# ---------------------------------------------------------------------------
# _parse_cli_args
# ---------------------------------------------------------------------------


def test_parse_cli_args_defaults() -> None:
    args = get_oauth_token._parse_cli_args([])
    assert args.chrome_path is None
    assert args.chrome_version is None


def test_parse_cli_args_values() -> None:
    args = get_oauth_token._parse_cli_args(
        ["--chrome-path", "/opt/chrome", "--chrome-version", "149"]
    )
    assert args.chrome_path == "/opt/chrome"
    assert args.chrome_version == 149


def test_parse_cli_args_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        get_oauth_token._parse_cli_args(["--help"])
    assert excinfo.value.code == 0


def test_parse_cli_args_invalid_version_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        get_oauth_token._parse_cli_args(["--chrome-version", "not-an-int"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver(cookie={"value": "token-abc"})
    captured: dict[str, Any] = {}

    def fake_create_driver(
        *, chrome_path: Any, chrome_version: Any, headless: bool
    ) -> Any:
        captured["chrome_path"] = chrome_path
        captured["chrome_version"] = chrome_version
        captured["headless"] = headless
        return driver

    monkeypatch.setattr(chrome_driver, "create_driver", fake_create_driver)
    _patch_wait(monkeypatch)

    get_oauth_token.main(chrome_path="/opt/chrome", chrome_version=149)

    assert captured == {
        "chrome_path": "/opt/chrome",
        "chrome_version": 149,
        "headless": False,
    }
    assert driver.visited_urls == ["https://accounts.google.com/EmbeddedSetup"]
    assert driver.quit_calls == 1


def test_main_missing_cookie_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver(cookie=None)

    monkeypatch.setattr(chrome_driver, "create_driver", lambda **kwargs: driver)
    _patch_wait(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        get_oauth_token.main()

    assert excinfo.value.code == 1
    assert driver.quit_calls == 1


def test_main_import_error_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "selenium.webdriver.support.ui":
            raise ImportError("missing selenium")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SystemExit) as excinfo:
        get_oauth_token.main()

    assert excinfo.value.code == 1


def test_main_create_driver_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("no chrome")

    monkeypatch.setattr(chrome_driver, "create_driver", boom)

    with pytest.raises(SystemExit) as excinfo:
        get_oauth_token.main()

    assert excinfo.value.code == 1


def test_module_entrypoint_invokes_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the ``__main__`` guard by executing the module body as a script.

    ``runpy`` re-executes the source file with ``__name__ == "__main__"`` in a
    throwaway namespace, so we inject lightweight ``create_driver`` and ``main``
    replacements via the imported submodules to avoid launching a real browser.
    """

    import runpy

    captured: dict[str, Any] = {}
    driver = _FakeDriver(cookie={"value": "entry-token"})

    def fake_create_driver(
        *, chrome_path: Any, chrome_version: Any, headless: bool
    ) -> Any:
        captured["chrome_path"] = chrome_path
        captured["chrome_version"] = chrome_version
        return driver

    monkeypatch.setattr(chrome_driver, "create_driver", fake_create_driver)
    _patch_wait(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["get_oauth_token.py", "--chrome-version", "150"])

    runpy.run_path(get_oauth_token.__file__, run_name="__main__")

    # The script parses argv and forwards it to ``main``, which builds the
    # driver via the patched factory and walks the success branch.
    assert captured == {"chrome_path": None, "chrome_version": 150}
    assert driver.visited_urls == ["https://accounts.google.com/EmbeddedSetup"]
