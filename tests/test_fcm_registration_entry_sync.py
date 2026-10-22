# tests/test_fcm_registration_entry_sync.py
"""Regenerated FCM credentials must stay durable in the config entry.

Regression for the live report: pressing the "regenerate FCM token" button
obtained a working registration, but it only reached the entry-scoped token
cache. The next setup re-seeded the entry's (stale) secrets bundle into that
cache and brought the dead registration back, so FCM delivery stopped again.

The receiver now writes a refreshed registration back into the entry's bundle,
which is the value ``async_setup_entry`` seeds from. The tests drive the real
credentials-update path and then simulate the next setup by re-running
``_async_save_secrets_data`` from the entry data.
"""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import CONF_GOOGLE_EMAIL, DATA_SECRET_BUNDLE
from tests.helpers.config_entries_stub import make_config_entry

ENTRY_ID = "entry-fcm-regeneration"


class _Cache:
    """Entry-scoped cache double covering both write surfaces in play."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, name: str) -> Any:
        return self.data.get(name)

    async def set(self, name: str, value: Any) -> None:
        self.data[name] = value

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        self.data[name] = value


class _ConfigEntries:
    """In-memory stand-in for Home Assistant's config-entry manager.

    ``async_update_entry`` mirrors the core: the in-memory entry changes now and
    the debounced store save happens later. Only the in-memory half matters for
    a reload that follows, which is the sequence the bug report describes.
    """

    def __init__(self, entry: Any) -> None:
        self._entries = {entry.entry_id: entry}
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def async_get_entry(self, entry_id: str) -> Any | None:
        return self._entries.get(entry_id)

    def async_update_entry(self, entry: Any, **kwargs: Any) -> None:
        self.update_calls.append((entry.entry_id, dict(kwargs)))
        data = kwargs.get("data")
        if data is not None:
            entry.data = dict(data)


def _integration_module() -> ModuleType:
    """Import the integration package, seeding the loader stub when needed."""

    if "homeassistant.loader" not in sys.modules:
        loader_module = ModuleType("homeassistant.loader")
        loader_module.async_get_integration = lambda *_args, **_kwargs: None
        sys.modules["homeassistant.loader"] = loader_module
    return import_module("custom_components.googlefindmy")


def _registration(token: str) -> dict[str, Any]:
    """Return a complete FCM credentials dict carrying ``token``."""

    return {
        "gcm": {"android_id": "1234567890", "security_token": "9876543210"},
        "fcm": {"registration": {"token": token}},
    }


@pytest.mark.asyncio
async def test_a_refreshed_registration_survives_the_next_setup() -> None:
    """A regenerated registration is written to the entry and survives reseeding.

    Without the fix the fresh value only reaches the cache: the entry keeps the
    dead registration, and the second ``_async_save_secrets_data`` call (the
    next setup) restores it, which is exactly the reported reload regression.
    """

    integration = _integration_module()
    stale = _registration("dead-registration")
    fresh = _registration("fresh-registration")
    entry = make_config_entry(
        entry_id=ENTRY_ID,
        data={
            CONF_GOOGLE_EMAIL: "user@example.com",
            # The config flow writes the same value both into the bundle and
            # beside it, so both copies are faced with the refresh.
            "fcm_credentials": stale,
            DATA_SECRET_BUNDLE: {
                "username": "user@example.com",
                "fcm_credentials": stale,
            },
        },
    )
    manager = _ConfigEntries(entry)
    cache = _Cache()

    # First setup: the entry's bundle seeds the live cache.
    await integration._async_save_secrets_data(cache, entry.data[DATA_SECRET_BUNDLE])
    assert cache.data["fcm_credentials"] == stale

    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace(config_entries=manager))
    receiver._entry_caches[ENTRY_ID] = cache

    # The registration succeeds and the client reports the refreshed creds.
    receiver._on_credentials_updated_for_entry(ENTRY_ID, fresh)
    if receiver._active_tasks:
        await asyncio.gather(*list(receiver._active_tasks))

    assert cache.data["fcm_credentials"] == fresh
    assert entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"] == fresh
    assert entry.data["fcm_credentials"] == fresh

    # Next setup: the seed reads the entry again and must not undo the refresh.
    await integration._async_save_secrets_data(cache, entry.data[DATA_SECRET_BUNDLE])
    stored = cache.data["fcm_credentials"]
    assert stored["fcm"]["registration"]["token"] == "fresh-registration"


def test_a_registration_refresh_is_not_a_credential_change() -> None:
    """The written-back registration must not schedule an entry reload.

    The live cache already carries the refreshed value, so the update listener
    has to stay silent; a reload would tear down the receiver that just obtained
    the registration. A change to a seeded bundle member still counts.
    """

    integration = _integration_module()
    bundle = {
        "username": "user@example.com",
        "shared_key": "shared-key-value",
        "fcm_credentials": _registration("dead-registration"),
    }
    refreshed = {**bundle, "fcm_credentials": _registration("fresh-registration")}
    changed_shared_key = {**bundle, "shared_key": "another-shared-key"}

    base_fingerprint = integration._credential_fingerprint({DATA_SECRET_BUNDLE: bundle})

    assert (
        integration._credential_fingerprint({DATA_SECRET_BUNDLE: refreshed})
        == base_fingerprint
    )
    assert (
        integration._credential_fingerprint({DATA_SECRET_BUNDLE: changed_shared_key})
        != base_fingerprint
    )
