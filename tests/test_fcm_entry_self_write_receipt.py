# tests/test_fcm_entry_self_write_receipt.py
"""The update listener tells a user's credential change from the receiver's own write.

Both arrive as an ``async_update_entry`` that changes
``secrets_data["fcm_credentials"]``. A change the user made (an imported
``secrets.json``) has to become effective, which needs exactly one reload,
because the token cache is only seeded in ``async_setup_entry``. The receiver
writing back a registration it has just obtained must not reload: the live
cache already holds that value, and a reload would tear down the receiver that
produced it.

The listener is driven directly, not through a config flow: the flows schedule
their own reload, which would hide a listener that stays quiet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import CONF_GOOGLE_EMAIL, DATA_SECRET_BUNDLE
from tests.test_hass_data_layout import _prepare_async_setup_entry_harness


def _registration(token: str) -> dict[str, Any]:
    """Return a minimal FCM registration whose token identifies it."""

    return {
        "gcm": {"android_id": "1", "security_token": "2"},
        "fcm": {"registration": {"token": token}},
    }


async def _set_up_entry_with_bundle(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> tuple[Any, Any, Any, Callable[..., Any]]:
    """Set up an entry that keeps its bundle and return its update listener."""

    loop = asyncio.get_running_loop()
    harness = _prepare_async_setup_entry_harness(
        monkeypatch, stub_coordinator_factory, loop
    )
    integration = harness.integration
    entry = harness.entry
    hass = harness.hass

    entry.data = {
        CONF_GOOGLE_EMAIL: "user@example.com",
        DATA_SECRET_BUNDLE: {
            "username": "user@example.com",
            "fcm_credentials": _registration("setup-registration"),
        },
    }
    # A primed cache keeps the legacy migration from moving the bundle out of
    # the entry, so the listener sees the bundle the way a modern entry has it.
    harness.cache.values = {integration.username_string: "user@example.com"}

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry) is True
    assert DATA_SECRET_BUNDLE in entry.data
    assert len(entry._update_listeners) == 1
    return integration, entry, hass, entry._update_listeners[0]


@pytest.mark.asyncio
async def test_user_import_changing_only_fcm_credentials_schedules_one_reload(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A re-imported bundle that differs only in the registration reloads once."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )

    imported_bundle = {
        **entry.data[DATA_SECRET_BUNDLE],
        "fcm_credentials": _registration("imported-registration"),
    }
    entry.data = {**entry.data, DATA_SECRET_BUNDLE: imported_bundle}
    await notify(hass, entry)
    await notify(hass, entry)

    assert hass.config_entries.scheduled_reloads == [entry.entry_id], (
        "an imported registration has to become effective without a restart, "
        "and a second notification must not schedule a second reload"
    )


@pytest.mark.asyncio
async def test_receiver_self_write_schedules_no_reload(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The receiver writing back its own registration does not reload."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry(
        entry.entry_id, _registration("regenerated-registration")
    )

    assert len(hass.config_entries.entry_update_calls) == 1
    persisted = entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"]
    assert persisted["fcm"]["registration"]["token"] == "regenerated-registration"

    # Home Assistant notifies every update listener after an entry update.
    await notify(hass, entry)

    assert hass.config_entries.scheduled_reloads == []
