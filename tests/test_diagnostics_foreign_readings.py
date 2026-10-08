# tests/test_diagnostics_foreign_readings.py
"""Diagnostics ``foreign_report_readings`` block (provisional P-256 readings).

The tracker records which provisional reading decrypted a crowdsourced P-256
report, or why none did. The config-entry diagnostics carry that finding so a
user can attach it to the feedback issue without enabling INFO logging. These
tests drive the real ``async_get_config_entry_diagnostics`` and pin three
properties: the block exists, it lists only devices of the dumped entry, and
the dump contains no canonical ID of a listed device. They use
``@pytest.mark.asyncio`` and ``await`` directly (tests/AGENTS.md: never
``asyncio.run`` in tests).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    P256_FOREIGN_READINGS,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.foreign_reading_tracker import (
    FOREIGN_READING_FEEDBACK_URL,
    FOREIGN_READING_TRACKER,
    foreign_device_key,
)
from tests.helpers.config_entries_stub import make_config_entry

_ENTRY_A = "entry-foreign-a"
_ENTRY_B = "entry-foreign-b"
# Distinctive values so a substring search over the dump cannot hit by chance.
_CANONIC_A1 = "canonic-a1-7f3e9c0d"
_CANONIC_A2 = "canonic-a2-51b8e2aa"
_CANONIC_B1 = "canonic-b1-0c4d6f19"
_READING_1 = P256_FOREIGN_READINGS[0]


def _patch_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize loader and registry lookups so the dump builds in isolation."""

    async def _fake_get_integration(_hass: Any, _domain: str) -> SimpleNamespace:
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )


async def _dump(monkeypatch: pytest.MonkeyPatch, entry_id: str) -> dict[str, Any]:
    """Return the redacted diagnostics payload of one config entry."""
    _patch_loader(monkeypatch)
    entry = make_config_entry(
        entry_id=entry_id,
        domain=DOMAIN,
        runtime_data=SimpleNamespace(coordinator=None),
    )
    hass = SimpleNamespace(data={DOMAIN: {}})
    return await diagnostics.async_get_config_entry_diagnostics(hass, entry)


def _fail(entry_id: str, canonic_id: str) -> None:
    FOREIGN_READING_TRACKER.note_all_failed(
        foreign_device_key(entry_id, canonic_id),
        _READING_1.curve.name,
        32,
        (_READING_1.reading_id,),
        "poll-1",
    )


@pytest.mark.asyncio
async def test_block_is_present_and_empty_without_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = await _dump(monkeypatch, _ENTRY_A)
    assert payload["foreign_report_readings"] == {
        "feedback_url": FOREIGN_READING_FEEDBACK_URL,
        "devices": [],
    }


@pytest.mark.asyncio
async def test_two_decrypted_devices_without_their_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for canonic_id in (_CANONIC_A2, _CANONIC_A1):
        FOREIGN_READING_TRACKER.note_success(
            foreign_device_key(_ENTRY_A, canonic_id), _READING_1, 32
        )

    payload = await _dump(monkeypatch, _ENTRY_A)

    devices = payload["foreign_report_readings"]["devices"]
    assert [device["index"] for device in devices] == [0, 1]
    for device in devices:
        assert device["reading"] == "p256/mod_n/nonce8"
        assert device["status"] == "decrypted"
        assert device["curve"] == "secp256r1"
        assert device["sx_len"] == 32
    dumped = json.dumps(payload)
    assert _CANONIC_A1 not in dumped
    assert _CANONIC_A2 not in dumped


@pytest.mark.asyncio
async def test_block_of_entry_a_lists_no_device_of_entry_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FOREIGN_READING_TRACKER.note_success(
        foreign_device_key(_ENTRY_A, _CANONIC_A1), _READING_1, 32
    )
    FOREIGN_READING_TRACKER.note_unsupported(
        foreign_device_key(_ENTRY_B, _CANONIC_B1), 24
    )

    payload_a = await _dump(monkeypatch, _ENTRY_A)
    payload_b = await _dump(monkeypatch, _ENTRY_B)

    (device_a,) = payload_a["foreign_report_readings"]["devices"]
    (device_b,) = payload_b["foreign_report_readings"]["devices"]
    assert device_a["status"] == "decrypted"
    assert device_b["status"] == "unsupported_length"
    assert _CANONIC_B1 not in json.dumps(payload_a)


@pytest.mark.asyncio
async def test_all_failed_has_no_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail(_ENTRY_A, _CANONIC_A1)

    payload = await _dump(monkeypatch, _ENTRY_A)

    (device,) = payload["foreign_report_readings"]["devices"]
    assert device == {
        "index": 0,
        "curve": "secp256r1",
        "sx_len": 32,
        "reading": None,
        "status": "all_failed",
    }
    assert _CANONIC_A1 not in json.dumps(payload)


@pytest.mark.asyncio
async def test_unsupported_length_has_no_curve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FOREIGN_READING_TRACKER.note_unsupported(
        foreign_device_key(_ENTRY_A, _CANONIC_A1), 24
    )

    payload = await _dump(monkeypatch, _ENTRY_A)

    (device,) = payload["foreign_report_readings"]["devices"]
    assert device == {
        "index": 0,
        "curve": None,
        "sx_len": 24,
        "reading": None,
        "status": "unsupported_length",
    }
    assert _CANONIC_A1 not in json.dumps(payload)
