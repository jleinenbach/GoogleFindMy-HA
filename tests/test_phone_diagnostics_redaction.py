# tests/test_phone_diagnostics_redaction.py
"""Diagnostics and device registry carry neither Android ID nor Wi-Fi name.

A phone on the phone route is addressed by its Android ID (kept in the API's
target table under its row id: the prefixed id of a phone listed without a
canonical ID, or the canonical ID of a phone listed with one that offers the
phone locate action) and reports the name of its Wi-Fi network (kept in the
coordinator's status store under the same id). Neither may reach a diagnostics
download, which users paste into public issues, nor the device registry.

Sentinel values only: the Android ID is invented, the Wi-Fi name is
``"Beispiel-Testnetz"``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import DOMAIN, PHONE_ID_PREFIX
from custom_components.googlefindmy.diagnostics import (
    TO_REDACT,
    TO_REDACT_PREFIXES,
    async_redact_data,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
    PhoneStatus,
)
from custom_components.googlefindmy.redaction import REDACTED
from custom_components.googlefindmy.sensor import (
    GoogleFindMyPhoneBatterySensor,
    GoogleFindMyPhoneWifiSensor,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_coordinator_automatic_polling import _DispatchingAPI, _make_coordinator

_ANDROID_ID = 0x1122334455667788
_SSID = "Beispiel-Testnetz"
_PHONE = PHONE_ID_PREFIX + "b" * 32
_OWNED = "cid-owned-diag"
_ROW_IDS = [_PHONE, _OWNED]
_NOW = 1_800_000_000.0


def _secrets() -> tuple[str, ...]:
    return (_SSID, str(_ANDROID_ID), f"{_ANDROID_ID:x}", f"{_ANDROID_ID:X}")


def test_redaction_list_names_the_phone_keys() -> None:
    for key in ("phone_wifi_ssid", "wifi_ssid", "phone_targets", "phone_status"):
        assert key in TO_REDACT


def test_phone_keys_are_redacted_wherever_they_appear() -> None:
    payload = {
        "phone_targets": {_PHONE: _ANDROID_ID},
        "_phone_targets": {_PHONE: _ANDROID_ID},
        "nested": [{"phone_wifi_ssid": _SSID, "wifi_ssid": f'"{_SSID}"'}],
        "phone_status": {_PHONE: {"wifi_ssid": _SSID, "battery_pct": 50}},
        "android_id": _ANDROID_ID,
    }
    redacted = async_redact_data(payload, TO_REDACT, TO_REDACT_PREFIXES)
    text = str(redacted)
    for secret in _secrets():
        assert secret not in text
    assert redacted["phone_targets"] == REDACTED


def test_phone_keys_with_a_canonical_id_key_are_redacted() -> None:
    """The table and the store keyed by a canonical ID are redacted as a whole.

    The redaction matches the key of the table, not the shape of its entries, so
    a canonical ID as key must not change anything; this pins that.
    """
    payload = {
        "_phone_targets": {_OWNED: _ANDROID_ID, _PHONE: _ANDROID_ID + 1},
        "phone_targets": {_OWNED: _ANDROID_ID},
        "_phone_status": {_OWNED: {"wifi_ssid": _SSID, "battery_pct": 55}},
        "phone_status": {_OWNED: {"wifi_ssid": _SSID}},
    }
    redacted = async_redact_data(payload, TO_REDACT, TO_REDACT_PREFIXES)
    text = str(redacted)
    for secret in _secrets():
        assert secret not in text
    assert _OWNED not in text
    assert all(redacted[key] == REDACTED for key in payload)


class _PhoneAPI(_DispatchingAPI):
    """API double with a filled target table, as after a device list."""

    def __init__(self, row_id: str = _PHONE) -> None:
        super().__init__()
        self._phone_targets = {row_id: _ANDROID_ID}
        self.sink: Any = None

    def set_phone_status_sink(self, sink: Any) -> None:
        self.sink = sink


@pytest.mark.asyncio
@pytest.mark.parametrize("row_id", _ROW_IDS)
async def test_diagnostics_dump_with_a_phone_carries_neither_value(
    monkeypatch: pytest.MonkeyPatch, row_id: str
) -> None:
    api = _PhoneAPI(row_id)
    coord = _make_coordinator(monkeypatch, options={}, api=api)  # type: ignore[arg-type]
    coord.async_update_listeners = lambda: None
    # Only the row of a phone without a canonical ID carries the mark (decoder).
    row: dict[str, Any] = {"id": row_id, "name": "Phone"}
    if row_id == _PHONE:
        row["locate_route"] = "phone"
    coord.data = [row]
    # Through the bound sink, as the locate path does it.
    api.sink(row_id, PhoneStatus(battery_pct=55, wifi_ssid=_SSID, observed_at=_NOW))
    assert coord.get_phone_status(row_id) is not None
    coord._device_location_data[row_id] = {
        "latitude": 1.0,
        "longitude": 2.0,
        "accuracy": 12.0,
        "last_seen": _NOW,
        "locate_route": "phone",
    }

    async def _fake_get_integration(_hass: Any, _domain: str) -> SimpleNamespace:
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )
    entry = make_config_entry(
        domain=DOMAIN,
        data={},
        runtime_data=SimpleNamespace(coordinator=coord),
    )
    hass = SimpleNamespace(data={DOMAIN: {}})

    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    text = str(payload)
    for secret in _secrets():
        assert secret not in text
    # The dump did see the coordinator (not a vacuous pass on an empty block).
    assert payload["coordinator"]["cache_items_count"] == 1


@pytest.mark.parametrize(
    "cls", [GoogleFindMyPhoneBatterySensor, GoogleFindMyPhoneWifiSensor]
)
@pytest.mark.parametrize("row_id", _ROW_IDS)
def test_device_registry_entry_of_a_phone_sensor_carries_neither_value(
    cls: type[Any], row_id: str
) -> None:
    status = PhoneStatus(battery_pct=55, wifi_ssid=_SSID, observed_at=_NOW)
    coordinator = SimpleNamespace(
        config_entry=make_config_entry(entry_id="entry-1"),
        is_device_present=lambda _did: True,
        is_device_visible_in_subentry=lambda _key, _did: True,
        async_update_listeners=lambda: None,
        get_subentry_snapshot=lambda _key: [{"id": row_id, "name": "Phone"}],
        get_phone_status=lambda _did: status,
        last_update_success=True,
    )
    entity = cls(
        coordinator,
        {"id": row_id, "name": "Phone"},
        subentry_key="core_tracking",
        subentry_identifier="tracker",
    )
    entity.hass = SimpleNamespace(data={})
    text = repr(entity.device_info) + repr(entity.unique_id)
    for secret in _secrets():
        assert secret not in text
    assert row_id in text
