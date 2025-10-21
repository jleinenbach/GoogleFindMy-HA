# tests/test_fcm_receiver_guard.py
"""Regression tests for the shared FCM receiver guard."""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA


class _StubReceiver:
    """Stub FCM receiver lacking async registration methods."""

    def __init__(self) -> None:
        self.stop_calls = 0

    async def async_stop(self) -> None:
        """Record stop invocations for verification."""

        self.stop_calls += 1


def test_async_acquire_discards_invalid_cached_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached receiver without async registration methods is replaced."""

    hass = SimpleNamespace(data={DOMAIN: {}})
    stub = _StubReceiver()
    hass.data[DOMAIN]["fcm_receiver"] = stub

    loader_module = ModuleType("homeassistant.loader")
    loader_module.async_get_integration = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader_module)

    module = importlib.import_module("custom_components.googlefindmy.__init__")
    async_acquire_shared_fcm = module._async_acquire_shared_fcm

    recorded_getters: dict[str, Callable[[], object]] = {}

    def capture_loc(getter: Callable[[], object]) -> None:
        recorded_getters["loc"] = getter

    def capture_api(getter: Callable[[], object]) -> None:
        recorded_getters["api"] = getter

    monkeypatch.setattr(
        module,
        "loc_register_fcm_provider",
        capture_loc,
    )
    monkeypatch.setattr(
        module,
        "api_register_fcm_provider",
        capture_api,
    )

    async def _run() -> FcmReceiverHA:
        return await async_acquire_shared_fcm(hass)

    new_receiver = asyncio.run(_run())

    assert isinstance(new_receiver, FcmReceiverHA)
    assert hass.data[DOMAIN]["fcm_receiver"] is new_receiver
    assert stub.stop_calls == 1
    assert "loc" in recorded_getters
    assert "api" in recorded_getters
    assert recorded_getters["loc"]() is new_receiver
    assert recorded_getters["api"]() is new_receiver
    assert hass.data[DOMAIN]["fcm_refcount"] == 1
