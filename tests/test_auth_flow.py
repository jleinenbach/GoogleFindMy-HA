# tests/test_auth_flow.py
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

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
    monkeypatch.setattr(auth_flow, "create_driver", lambda headless: driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", wait_factory)
    return wait_factory


def test_request_oauth_account_token_flow_returns_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver(cookie_after_wait={"value": "token123"})
    wait_factory = _apply_flow_patches(monkeypatch, driver)

    token = request_oauth_account_token_flow(headless=True)

    assert token == "token123"
    assert driver.visited_urls == ["https://accounts.google.com/EmbeddedSetup"]
    assert driver.quit_calls == 1
    assert wait_factory.instances and wait_factory.instances[0].until_calls == 1


def test_request_oauth_account_token_flow_missing_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver(cookie_after_wait=None)
    wait_factory = _apply_flow_patches(monkeypatch, driver)

    with pytest.raises(RuntimeError, match="OAuth token cookie missing despite wait completion"):
        request_oauth_account_token_flow(headless=True)

    assert driver.quit_calls == 1
    assert wait_factory.instances and wait_factory.instances[0].until_calls == 1


# Silence unused-import checks while keeping explicit references for clarity.
del WebDriverWait, create_driver
