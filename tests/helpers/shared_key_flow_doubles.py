# tests/helpers/shared_key_flow_doubles.py
"""Selenium doubles for ``KeyBackup.shared_key_flow``.

Shared by ``tests/test_shared_key_flow.py`` and
``tests/test_log_hygiene_keybackup.py`` so both drive the flow through the same
driver: one scripted for the alert loop, the Find My Device page and the
in-page vault fetch. No sleeping anywhere: waits resolve or raise at once, and
the deadline test advances an injected clock instead.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from custom_components.googlefindmy.KeyBackup import shared_key_flow
from tests.helpers.owner_key_vault_fixtures import VAULT_DOMAIN, VaultWorld


class FakeAlert:
    """Selenium alert double exposing canned text and an ``accept`` recorder."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True


class FakeSwitchTo:
    """``driver.switch_to`` shim returning the next queued alert."""

    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    @property
    def alert(self) -> FakeAlert:
        return self._driver.current_alert


class FakeDriver:
    """Scripted driver that feeds alert payloads and vault answers to the flow.

    The ``WebDriverWait`` replacement consults ``arm_next_alert`` to decide
    whether an alert is pending. ``async_answer`` is what ``execute_async_script``
    hands back; an exception instance there is raised instead, and
    ``get_error`` makes ``get`` raise for ``get_error_url``. ``calls`` records
    ``get``, ``set_script_timeout`` and ``execute_async_script`` in order, so a
    test can pin the navigation before the fetch.
    """

    def __init__(
        self, payloads: list[str] | None = None, *, async_answer: object = None
    ) -> None:
        self._payloads = list(payloads or [])
        self.current_alert: FakeAlert = FakeAlert("")
        self.visited_urls: list[str] = []
        self.scripts: list[str] = []
        self.async_scripts: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self.async_answer = async_answer
        self.get_error: BaseException | None = None
        self.get_error_url: str | None = None
        self.quit_calls = 0
        self.switch_to = FakeSwitchTo(self)

    def get(self, url: str) -> None:
        self.calls.append(("get", url))
        self.visited_urls.append(url)
        if self.get_error is not None and url == self.get_error_url:
            raise self.get_error

    def execute_script(self, script: str) -> Any:
        self.scripts.append(script)
        return None

    def set_script_timeout(self, seconds: float) -> None:
        self.calls.append(("set_script_timeout", seconds))

    def execute_async_script(self, script: str) -> object:
        self.calls.append(("execute_async_script", script))
        self.async_scripts.append(script)
        if isinstance(self.async_answer, BaseException):
            raise self.async_answer
        return self.async_answer

    def quit(self) -> None:
        self.quit_calls += 1

    def arm_next_alert(self) -> bool:
        """Load the next payload as the active alert; return False when drained."""

        if not self._payloads:
            return False
        self.current_alert = FakeAlert(self._payloads.pop(0))
        return True


class StopFlow(Exception):
    """Sentinel used by tests to terminate the alert loop."""


class FlowWait:
    """WebDriverWait replacement driving every wait stage of the flow.

    Waits with a timeout of one second or more (sign-in, page readiness) resolve
    at once. The 0.5 s alert waits arm the next alert; once the payloads run out
    the double raises ``StopFlow`` instead of looping.
    """

    def __init__(self, driver: FakeDriver, timeout: float) -> None:
        self.driver = driver
        self.timeout = timeout

    def until(self, _condition: Any) -> Any:
        if self.timeout >= 1:
            return True
        if not self.driver.arm_next_alert():
            raise StopFlow
        return True


class FakeClock:
    """Monotonic clock double; only the alert waits move it forward."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class NoAlertWait:
    """WebDriverWait replacement for a page that never raises an alert.

    Every alert wait advances ``clock`` by its timeout and raises
    ``TimeoutException``, like Selenium after waiting that long. More than
    ``max_alert_waits`` alert waits raise ``StopFlow``, so a loop without a
    deadline ends red instead of hanging the test run.
    """

    max_alert_waits = 10_000

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.alert_waits = 0

    def __call__(self, _driver: FakeDriver, timeout: float) -> NoAlertWait:
        self._timeout = timeout
        return self

    def until(self, _condition: Any) -> Any:
        from selenium.common.exceptions import TimeoutException  # noqa: PLC0415

        if self._timeout >= 1:
            return True
        self.alert_waits += 1
        if self.alert_waits > self.max_alert_waits:
            raise StopFlow
        self.clock.now += self._timeout
        raise TimeoutException


def patch_flow(
    monkeypatch: pytest.MonkeyPatch,
    driver: FakeDriver,
    *,
    shared_key: bytes | None = None,
) -> None:
    """Patch driver creation, waits and the security URL/parser helpers."""

    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", FlowWait)
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: "https://sec"
    )
    if shared_key is not None:
        monkeypatch.setattr(
            shared_key_flow, "get_fmdn_shared_key", lambda _: shared_key
        )


def vault_answer_text(world: VaultWorld) -> str:
    """Return a batchexecute answer carrying every vault entry of ``world``.

    Same reconstructed shape as the success fixture in
    ``tests/test_vault_web_api.py`` (``# reconstructed, MG-S5``): entry tuples
    ``key_b64, version, domain, epoch`` at ``inner[0][5]`` of the ``FAvFKc``
    frame. Blobs come from the factory at runtime, never as literals.
    """

    rows = [
        [
            base64.b64encode(blob).decode("ascii"),
            version,
            VAULT_DOMAIN,
            1_700_000_000 + version,
            None,
        ]
        for version, blob in world.wrapped_owner_keys.items()
    ]
    head: list[object] = [None] * 6
    head[5] = rows
    item = ["wrb.fr", "FAvFKc", json.dumps([head]), None, None, None, "generic"]
    body = json.dumps([item])
    return f")]}}'\n\n{len(body)}\n{body}\n"


def vault_answer(world: VaultWorld) -> dict[str, object]:
    """Return the callback value of ``FETCH_SCRIPT`` for a successful fetch."""

    return {"ok": True, "status": 200, "text": vault_answer_text(world)}
