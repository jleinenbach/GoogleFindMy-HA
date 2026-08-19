# tests/test_shared_key_flow.py
"""Tests for the manual shared key retrieval flow ``KeyBackup.shared_key_flow``."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.KeyBackup import shared_key_flow


class _FakeAlert:
    """Selenium alert double exposing canned text and an ``accept`` recorder."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True


class _FakeSwitchTo:
    """``driver.switch_to`` shim returning the next queued alert."""

    def __init__(self, driver: _FakeDriver) -> None:
        self._driver = driver

    @property
    def alert(self) -> _FakeAlert:
        return self._driver.current_alert


class _FakeDriver:
    """Scripted driver that feeds a sequence of alert payloads to the flow.

    The ``WebDriverWait`` replacement consults ``alert_present`` to decide
    whether an alert is pending. Each loop iteration pops the next payload and
    arms ``current_alert`` so ``switch_to.alert`` returns it.
    """

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.current_alert: _FakeAlert = _FakeAlert("")
        self.visited_urls: list[str] = []
        self.scripts: list[str] = []
        self.quit_calls = 0
        self.switch_to = _FakeSwitchTo(self)

    def get(self, url: str) -> None:
        self.visited_urls.append(url)

    def execute_script(self, script: str) -> Any:
        self.scripts.append(script)
        return None

    def quit(self) -> None:
        self.quit_calls += 1

    def arm_next_alert(self) -> bool:
        """Load the next payload as the active alert; return False when drained."""

        if not self._payloads:
            return False
        self.current_alert = _FakeAlert(self._payloads.pop(0))
        return True


class _FlowWait:
    """WebDriverWait replacement driving both wait stages of the flow.

    The first ``until`` (url_contains) returns immediately. Subsequent ``until``
    calls (alert_is_present, 0.5s timeout) arm the next alert; once payloads run
    out a ``TimeoutException`` would loop forever, so the driver instead raises
    ``_StopFlow`` to break the otherwise infinite ``while True`` loop in a test.
    """

    def __init__(self, driver: _FakeDriver, timeout: float) -> None:
        self.driver = driver
        self.timeout = timeout

    def until(self, _condition: Any) -> Any:
        # The first stage waits on a URL; treat any condition with the long
        # timeout as that stage and resolve instantly.
        if self.timeout >= 1:
            return True
        if not self.driver.arm_next_alert():
            raise _StopFlow
        return True


class _StopFlow(Exception):
    """Sentinel used by tests to terminate the otherwise infinite alert loop."""


def _patch_flow(
    monkeypatch: pytest.MonkeyPatch,
    driver: _FakeDriver,
    *,
    shared_key: bytes | None = None,
) -> None:
    """Patch driver creation, waits and the security URL/parser helpers."""

    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", _FlowWait)
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: "https://sec"
    )
    if shared_key is not None:
        monkeypatch.setattr(
            shared_key_flow, "get_fmdn_shared_key", lambda _: shared_key
        )


# ---------------------------------------------------------------------------
# _parse_cli_args
# ---------------------------------------------------------------------------


def test_parse_cli_args_defaults() -> None:
    args = shared_key_flow._parse_cli_args([])
    assert args.chrome_path is None
    assert args.chrome_version is None


def test_parse_cli_args_values() -> None:
    args = shared_key_flow._parse_cli_args(
        ["--chrome-path", "/opt/chrome", "--chrome-version", "149"]
    )
    assert args.chrome_path == "/opt/chrome"
    assert args.chrome_version == 149


def test_parse_cli_args_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        shared_key_flow._parse_cli_args(["--help"])
    assert excinfo.value.code == 0


def test_parse_cli_args_invalid_version_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        shared_key_flow._parse_cli_args(["--chrome-version", "x"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# request_shared_key_flow
# ---------------------------------------------------------------------------


def test_create_driver_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("no chrome")

    monkeypatch.setattr(shared_key_flow, "create_driver", boom)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None


def test_returns_shared_key_hex_on_set_vault_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": "raw-keys"})
    driver = _FakeDriver([payload])
    raw_key = bytes(range(8))
    _patch_flow(monkeypatch, driver, shared_key=raw_key)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda d: d.quit())

    captured_path: dict[str, Any] = {}
    monkeypatch.setattr(
        shared_key_flow,
        "create_driver",
        lambda **kwargs: captured_path.update(kwargs) or driver,
    )

    result = shared_key_flow.request_shared_key_flow(
        chrome_path="/opt/chrome", chrome_version=149
    )

    assert result == raw_key.hex()
    assert captured_path == {"chrome_path": "/opt/chrome", "chrome_version": 149}
    assert driver.visited_urls == ["https://accounts.google.com/", "https://sec"]
    assert driver.quit_calls == 1


def test_close_view_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"method": "closeView"})
    driver = _FakeDriver([payload])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None


def test_malformed_json_is_discarded_then_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver(["{not-json", good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None
    assert "malformed alert payload" in " ".join(caplog.messages).lower()


def test_set_vault_keys_with_invalid_payload_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": 123})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([bad, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None
    assert "invalid vaultkeys" in " ".join(caplog.messages).lower()


def test_unhandled_method_is_logged_then_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    other = json.dumps({"method": "somethingElse"})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([other, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None
    assert "unhandled alert payload" in " ".join(caplog.messages).lower()


def test_timeout_exception_continues_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending-alert ``TimeoutException`` must ``continue`` the wait loop."""

    from selenium.common.exceptions import TimeoutException

    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([good])

    state = {"raised": False}

    class _TimeoutOnceWait:
        def __init__(self, drv: _FakeDriver, timeout: float) -> None:
            self.driver = drv
            self.timeout = timeout

        def until(self, _condition: Any) -> Any:
            if self.timeout >= 1:
                return True
            if not state["raised"]:
                state["raised"] = True
                raise TimeoutException
            if not self.driver.arm_next_alert():
                raise _StopFlow
            return True

    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", _TimeoutOnceWait)
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: "https://sec"
    )
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None
    assert state["raised"] is True


def test_module_entrypoint_invokes_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the ``__main__`` guard by running the module as a script.

    ``runpy`` re-executes the file with fresh module-level imports, so the
    patched ``WebDriverWait`` cannot reach the rebound copy. To avoid launching
    a real browser, the freshly imported ``create_driver`` is forced to raise so
    the flow returns ``None`` through its early failure branch before any wait
    loop runs.
    """

    import runpy

    from custom_components.googlefindmy import chrome_driver

    calls: dict[str, Any] = {}

    def boom(**kwargs: Any) -> Any:
        calls.update(kwargs)
        raise RuntimeError("no chrome in __main__ test")

    # shared_key_flow binds ``create_driver`` via ``from ... import`` at module
    # load, so the runpy copy resolves this patched attribute at import time.
    monkeypatch.setattr(chrome_driver, "create_driver", boom)
    monkeypatch.setattr(chrome_driver, "safe_quit_driver", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["shared_key_flow.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(shared_key_flow.__file__, run_name="__main__")

    assert calls == {"chrome_path": None, "chrome_version": None}
    # The flow returned no key, and since this commit the command says so in
    # its exit status instead of reporting success.
    assert excinfo.value.code == 1


def test_the_command_reports_failure_in_its_exit_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`request_shared_key_flow` returns `None` for every failure.

    A caller that ignores that return exits 0 on a run that retrieved nothing,
    and a script around it cannot tell success from failure. Missing browser
    packages are now an ordinary reason to land here, so the name of the
    remedy is printed with it.
    """

    from custom_components.googlefindmy import browser_deps
    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    monkeypatch.setattr(
        flow,
        "_parse_cli_args",
        lambda: SimpleNamespace(chrome_path=None, chrome_version=None),
    )
    monkeypatch.setattr(flow, "request_shared_key_flow", lambda **_: None)
    monkeypatch.setattr(browser_deps, "browser_packages_missing", lambda: True)

    assert flow._main() == 1
    assert browser_deps.INSTALL_COMMAND in capsys.readouterr().err


def test_the_command_succeeds_when_a_key_comes_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Counterpart, so the exit status is not simply always 1."""

    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    monkeypatch.setattr(
        flow,
        "_parse_cli_args",
        lambda: SimpleNamespace(chrome_path=None, chrome_version=None),
    )
    monkeypatch.setattr(flow, "request_shared_key_flow", lambda **_: "aa" * 32)

    assert flow._main() == 0
    assert "aa" * 32 in capsys.readouterr().out


def test_the_unusable_package_type_is_not_turned_into_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other driver failure becomes `None`; this one must not.

    `undetected_chromedriver` installed but unimportable is invisible to a
    presence probe and unreadable from the message, so the type is the only
    carrier left. Erasing it here left the user with generic Chrome advice
    against a missing package.
    """

    from custom_components.googlefindmy import browser_deps, chrome_driver
    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    def _unusable(**_: Any) -> Any:
        raise browser_deps.BrowserPackagesUnusable("uc installed but broken")

    monkeypatch.setattr(chrome_driver, "create_driver", _unusable)
    monkeypatch.setattr(flow, "create_driver", _unusable)

    with pytest.raises(browser_deps.BrowserPackagesUnusable):
        flow.request_shared_key_flow()


def test_an_ordinary_driver_failure_still_becomes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart, so the pass-through is not simply "raise everything"."""

    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    def _boom(**_: Any) -> Any:
        raise RuntimeError("chrome crashed")

    monkeypatch.setattr(flow, "create_driver", _boom)

    assert flow.request_shared_key_flow() is None
