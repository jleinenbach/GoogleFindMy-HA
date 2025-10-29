# tests/test_auth_flow.py

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import auth_flow


class _FakeDriver:
    """Record interactions from the auth flow while returning canned cookies."""

    def __init__(self, cookie_sequence: Sequence[Any]) -> None:
        self._cookie_sequence = list(cookie_sequence)
        self._cookie_index = 0
        self.get_calls: list[str] = []
        self.quit_calls = 0
        self.cookie_calls = 0

    def get(self, url: str) -> None:
        self.get_calls.append(url)

    def get_cookie(self, name: str) -> Any:
        assert name == "oauth_token"
        self.cookie_calls += 1
        if self._cookie_index < len(self._cookie_sequence):
            result = self._cookie_sequence[self._cookie_index]
            self._cookie_index += 1
            return result
        return None

    def quit(self) -> None:
        self.quit_calls += 1


class _FakeWaitFactory:
    """Factory mirroring WebDriverWait(driver, timeout)."""

    def __init__(self) -> None:
        self.waits: list[_FakeWaitInstance] = []

    def __call__(self, driver: _FakeDriver, timeout: int) -> "_FakeWaitInstance":
        wait = _FakeWaitInstance(driver, timeout)
        self.waits.append(wait)
        return wait


class _FakeWaitInstance:
    def __init__(self, driver: _FakeDriver, timeout: int) -> None:
        self.driver = driver
        self.timeout = timeout
        self.until_calls = 0

    def until(self, predicate: Any) -> Any:
        self.until_calls += 1
        return predicate(self.driver)


def _patch_flow(monkeypatch: pytest.MonkeyPatch, driver: _FakeDriver) -> _FakeWaitFactory:
    wait_factory = _FakeWaitFactory()
    monkeypatch.setattr(auth_flow, "create_driver", lambda headless: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", wait_factory)
    return wait_factory


def test_request_oauth_account_token_flow_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver([
        {"value": "account-token"},
        {"value": "account-token"},
    ])
    waits = _patch_flow(monkeypatch, driver)

    token = auth_flow.request_oauth_account_token_flow()

    assert token == "account-token"
    assert driver.get_calls == ["https://accounts.google.com/EmbeddedSetup"]
    assert driver.quit_calls == 1
    assert waits.waits and waits.waits[0].until_calls == 1


def test_request_oauth_account_token_flow_missing_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver([
        {"value": "seed-cookie"},
        None,
    ])
    waits = _patch_flow(monkeypatch, driver)

    with pytest.raises(RuntimeError, match="OAuth token cookie missing"):
        auth_flow.request_oauth_account_token_flow()

    assert driver.quit_calls == 1
    assert waits.waits and waits.waits[0].until_calls == 1


def test_request_oauth_account_token_flow_cookie_value_not_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver([
        {"value": 42},
        {"value": 42},
    ])
    waits = _patch_flow(monkeypatch, driver)

    with pytest.raises(RuntimeError, match="OAuth token cookie value is missing"):
        auth_flow.request_oauth_account_token_flow()

    assert driver.quit_calls == 1
    assert waits.waits and waits.waits[0].until_calls == 1
