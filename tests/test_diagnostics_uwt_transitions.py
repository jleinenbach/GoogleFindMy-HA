# tests/test_diagnostics_uwt_transitions.py
"""Diagnostics ``uwt_transitions`` block (protection-mode changes).

The EID resolver records each change of a device's unwanted-tracking
protection mode it sees, per config entry. The config-entry diagnostics carry that
history so a user can attach it to an issue without configuring logging.
Users post this block publicly, so the tests drive the real
``async_get_config_entry_diagnostics`` and pin, by a difference test, that
the block adds no registry id and no canonical id to the dump: the id values
of the test devices in the whole dump are the same set with and without the
block, and none of them occurs inside the block.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.eid_resolver import (
    MODERN_FRAME_TYPE,
    EIDMatch,
    GoogleFindMyEIDResolver,
    ObservationClock,
    UwtSource,
    _FlagsReading,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_ble_battery_sensor import _make_resolver

_ENTRY_A = "entry-uwt-a"
_ENTRY_B = "entry-uwt-b"
# Distinctive values so a substring search over the dump cannot hit by chance.
_CANONIC_A1 = "canonic-a1-3c9e71d0"
_CANONIC_A2 = "canonic-a2-8b04f6e2"
_CANONIC_B1 = "canonic-b1-d51a0c7f"
_REGISTRY = {
    _CANONIC_A1: "registry-a1-6f2d9e04",
    _CANONIC_A2: "registry-a2-0a7c33b1",
    _CANONIC_B1: "registry-b1-9e4417c8",
}
_ID_VALUES = (*_REGISTRY, *_REGISTRY.values())
_EVENT_KEYS = {
    "device_index",
    "kind",
    "old",
    "new",
    "source",
    "flags",
    "frame",
    "payload_len",
    "poll_age_s",
    "prev_seen_age_s",
    "observed",
    "seq",
}


def _patch_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_integration(_hass: Any, _domain: str) -> SimpleNamespace:
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )


def _observe(
    resolver: GoogleFindMyEIDResolver, entry: str, canonic: str, mode: bool, mono: float
) -> None:
    resolver._note_uwt_observation(
        [EIDMatch(_REGISTRY[canonic], entry, canonic, 0, False)],
        mode=mode,
        source=UwtSource.FLAGS_BIT,
        reading=_FlagsReading(mode, "framed", 0x01 if mode else 0x00),
        frame_type=MODERN_FRAME_TYPE,
        payload_len=29,
        clock=ObservationClock(monotonic=mono, wall=1_800_000_000.0 + mono),
    )


def _resolver_with_history() -> GoogleFindMyEIDResolver:
    resolver = _make_resolver()
    _observe(resolver, _ENTRY_A, _CANONIC_A2, False, 10.0)
    _observe(resolver, _ENTRY_A, _CANONIC_A1, True, 20.0)
    _observe(resolver, _ENTRY_A, _CANONIC_A2, True, 30.0)
    _observe(resolver, _ENTRY_B, _CANONIC_B1, True, 40.0)
    return resolver


async def _dump(
    monkeypatch: pytest.MonkeyPatch, entry_id: str, resolver: object | None
) -> dict[str, Any]:
    _patch_loader(monkeypatch)
    entry = make_config_entry(
        entry_id=entry_id,
        domain=DOMAIN,
        runtime_data=SimpleNamespace(coordinator=None),
    )
    domain_data: dict[str, Any] = {}
    if resolver is not None:
        domain_data[DATA_EID_RESOLVER] = resolver
    hass = SimpleNamespace(data={DOMAIN: domain_data})
    return await diagnostics.async_get_config_entry_diagnostics(hass, entry)


def _ids_in(text: str) -> set[str]:
    return {value for value in _ID_VALUES if value in text}


@pytest.mark.asyncio
async def test_block_adds_no_id_value_to_the_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Difference test (K12): the block carries positions, never ids."""
    payload = await _dump(monkeypatch, _ENTRY_A, _resolver_with_history())

    block = payload["uwt_transitions"]
    without_block = {k: v for k, v in payload.items() if k != "uwt_transitions"}
    assert _ids_in(json.dumps(payload)) == _ids_in(json.dumps(without_block))
    assert _ids_in(json.dumps(block)) == set()
    assert block["events"], "the difference test needs events to bite"


@pytest.mark.asyncio
async def test_events_use_the_fixed_keys_and_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = await _dump(monkeypatch, _ENTRY_A, _resolver_with_history())
    block = payload["uwt_transitions"]
    assert (block["capacity"], block["dropped"]) == (200, 0)
    assert block["info_lines_per_device_hour"] == 12
    events = block["events"]
    assert all(set(event) == _EVENT_KEYS for event in events)
    # Sorted canonical ids: a1 -> 0, a2 -> 1.
    assert [(e["device_index"], e["old"], e["new"]) for e in events] == [
        (1, "unknown", "off"),
        (0, "unknown", "on"),
        (1, "off", "on"),
    ]
    assert events[0]["observed"] == "2027-01-15T08:00:10Z"
    forbidden = {"latitude", "longitude", "email", "name", "canonical_id"}
    assert not forbidden & {key for event in events for key in event}


@pytest.mark.asyncio
async def test_events_of_another_entry_are_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _resolver_with_history()
    payload_a = await _dump(monkeypatch, _ENTRY_A, resolver)
    payload_b = await _dump(monkeypatch, _ENTRY_B, resolver)
    assert len(payload_a["uwt_transitions"]["events"]) == 3
    assert [e["device_index"] for e in payload_b["uwt_transitions"]["events"]] == [0]


@pytest.mark.asyncio
async def test_block_is_empty_without_a_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = await _dump(monkeypatch, _ENTRY_A, None)
    assert payload["uwt_transitions"] == {
        "capacity": None,
        "dropped": 0,
        "info_lines_per_device_hour": None,
        "events": [],
    }


@pytest.mark.asyncio
async def test_block_is_empty_when_the_resolver_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def uwt_transition_snapshot(self, entry_id: str) -> object:
            raise RuntimeError("resolver state unavailable")

    payload = await _dump(monkeypatch, _ENTRY_A, _Broken())
    assert payload["uwt_transitions"]["events"] == []
