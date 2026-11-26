# tests/test_coordinator_cache_proto3_defaults.py
"""Regression tests: proto3 default values must not overwrite cached values.

proto3 scalar and bytes fields have no presence, so an unset int32 field reads
as 0 and an unset bytes field reads as b"". The location cache took such a
default for a real value on both write paths, ``update_device_cache`` and a
direct ``_persist_anchor_metadata`` call (the poll cycle), and so replaced a
stored pair date, owner-key version or key blob. A nested
``deviceRegistration.pairDate`` was judged by the cache instead of by the new
payload. Expected values are literals on purpose; the tests do not import the
helper module they protect.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_eid_data_flow import _create_test_coordinator

_DEVICE = "test-device"
_EIK = bytes(range(48))


def _coordinator(resets: list[str] | None = None) -> Any:
    """Return a coordinator with the real cache merge and stubbed side paths.

    Reuses the shared EID data-flow harness, which keeps the real
    ``update_device_cache`` and ``_persist_anchor_metadata`` code, and swaps in
    the canonical config-entry stub required by ``tests/AGENTS.md``.
    """
    coordinator = _create_test_coordinator()
    coordinator.config_entry = make_config_entry(entry_id="test_entry_123")
    coordinator._is_on_hass_loop = lambda: True
    coordinator._normalize_identity_key = (
        GoogleFindMyCoordinator._normalize_identity_key
    )
    coordinator._apply_weighted_location_fusion = lambda device_id, slot: True
    coordinator._apply_semantic_mapping = lambda slot: None
    coordinator._track_device_interval = lambda device_id, timestamp: None
    coordinator._is_significant_update = lambda device_id, slot: True
    coordinator._ensure_device_name_cache = lambda: {}
    coordinator.increment_stat = lambda stat: None
    coordinator._record_semantic_label = lambda slot, device_id: None
    coordinator.claim_report_for_tally = lambda device_id, slot: False
    coordinator._expire_coarse_fix = lambda device_id, slot: None
    coordinator._schedule_inline_eid_resolver_refresh = lambda device_id: None
    coordinator._schedule_eid_resolver_refresh = lambda: None
    recorded: list[str] = [] if resets is None else resets
    coordinator._reset_resolver_offset = recorded.append
    return coordinator


def _fix(last_seen: int, **fields: Any) -> dict[str, Any]:
    """Return a location report with neutral test coordinates."""
    return {"latitude": 1.0, "longitude": 2.0, "last_seen": last_seen, **fields}


def _row(coordinator: Any) -> dict[str, Any]:
    """Return the cached row of the test device."""
    row = coordinator._device_location_data[_DEVICE]
    assert isinstance(row, dict)
    return row


def test_update_keeps_cached_values_against_proto3_defaults() -> None:
    """Defaults in a newer report leave the cached anchor, version and EIK."""
    coordinator = _coordinator()
    coordinator._device_location_data[_DEVICE] = _fix(
        100,
        owner_key_version=3,
        pair_date=1700000000,
        encrypted_identity_key=_EIK,
    )

    coordinator.update_device_cache(
        _DEVICE,
        _fix(200, owner_key_version=0, pair_date=0, encrypted_identity_key=b""),
    )

    row = _row(coordinator)
    assert row.get("owner_key_version") == 3
    assert row.get("pair_date") == 1700000000
    assert row.get("encrypted_identity_key") == _EIK


def test_update_does_not_report_key_change_for_empty_eik() -> None:
    """An empty EIK next to an unchanged version is no key change."""
    resets: list[str] = []
    coordinator = _coordinator(resets)
    coordinator._device_location_data[_DEVICE] = _fix(
        100, owner_key_version=3, encrypted_identity_key=_EIK
    )

    coordinator.update_device_cache(
        _DEVICE, _fix(200, owner_key_version=3, encrypted_identity_key=b"")
    )

    assert resets == []


def test_update_drops_default_held_by_cache_and_payload() -> None:
    """A default in both cache and report leaves the key without a value."""
    coordinator = _coordinator()
    coordinator._device_location_data[_DEVICE] = _fix(
        100, pair_date=0, ownerKeyVersion=0
    )

    coordinator.update_device_cache(_DEVICE, _fix(200, pair_date=0))

    row = _row(coordinator)
    for key in ("pair_date", "pairDate", "owner_key_version", "ownerKeyVersion"):
        assert row.get(key) is None, key


@pytest.mark.parametrize(
    ("key", "valid", "default"),
    [
        ("pair_date", 1700000000, 0),
        ("secrets_creation_date", 1700000000, 0),
        ("owner_key_version", 3, 0),
        ("encrypted_identity_key", _EIK, b""),
        ("identity_key", "ab" * 16, ""),
    ],
)
def test_persist_keeps_valid_value_against_default(
    key: str, valid: object, default: object
) -> None:
    """A direct metadata write with a default keeps the earlier value."""
    coordinator = _coordinator()

    coordinator._persist_anchor_metadata(_DEVICE, {key: valid})
    coordinator._persist_anchor_metadata(_DEVICE, {key: default})

    assert _row(coordinator).get(key) == valid


@pytest.mark.parametrize("cached", [1783203555, 0])
def test_nested_pair_date_replaces_cached_value(cached: int) -> None:
    """A set nested pairDate replaces the cached pair date on a direct write."""
    coordinator = _coordinator()
    coordinator._persist_anchor_metadata(_DEVICE, {"pair_date": cached})

    coordinator._persist_anchor_metadata(
        _DEVICE, {"deviceRegistration": {"pairDate": 1783293855}}
    )

    assert _row(coordinator).get("pair_date") == 1783293855


def test_update_nested_pair_date_replaces_cached_value() -> None:
    """A set nested pairDate replaces the cached pair date on the merge path."""
    coordinator = _coordinator()
    coordinator.update_device_cache(_DEVICE, _fix(100, pair_date=1783203555))

    coordinator.update_device_cache(
        _DEVICE, _fix(200, deviceRegistration={"pairDate": 1783293855})
    )

    assert _row(coordinator).get("pair_date") == 1783293855


def test_update_nested_default_pair_date_keeps_cached_value() -> None:
    """A nested pairDate of 0 is no new pairing date on the merge path either."""
    coordinator = _coordinator()
    coordinator.update_device_cache(_DEVICE, _fix(100, pair_date=1783203555))

    coordinator.update_device_cache(
        _DEVICE, _fix(200, deviceRegistration={"pairDate": 0})
    )

    assert _row(coordinator).get("pair_date") == 1783203555


def test_update_keeps_set_camel_value_next_to_default() -> None:
    """A default snake_case key must not shadow its set camelCase twin."""
    coordinator = _coordinator()

    coordinator.update_device_cache(
        _DEVICE, _fix(100, pair_date=0, pairDate=1700000000)
    )

    assert _row(coordinator).get("pair_date") == 1700000000


def test_flat_pair_date_wins_over_nested_in_same_payload() -> None:
    """A set flat pair date wins over a nested one in the same payload."""
    coordinator = _coordinator()

    coordinator._persist_anchor_metadata(
        _DEVICE,
        {"pair_date": 1700000000, "deviceRegistration": {"pairDate": 1800000000}},
    )

    assert _row(coordinator).get("pair_date") == 1700000000


@pytest.mark.parametrize(
    ("path", "key", "old", "new"),
    [
        ("update", "owner_key_version", 3, 5),
        ("persist", "encrypted_identity_key", _EIK, bytes(range(1, 49))),
        ("persist", "encrypted_identity_key", None, "ab" * 24),
        ("persist", "owner_key_version", None, "3"),
        ("persist", "deviceRegistration", None, {"other": "value"}),
        ("persist", "identity_key_candidates", None, [b"\x01" * 32]),
    ],
)
def test_non_default_values_pass_unchanged(
    path: str, key: str, old: object, new: object
) -> None:
    """Every value that is not a default is stored unchanged, type included."""
    coordinator = _coordinator()
    if path == "update":
        coordinator._device_location_data[_DEVICE] = _fix(100, **{key: old})
        coordinator.update_device_cache(_DEVICE, _fix(200, **{key: new}))
    else:
        if old is not None:
            coordinator._persist_anchor_metadata(_DEVICE, {key: old})
        coordinator._persist_anchor_metadata(_DEVICE, {key: new})

    stored = _row(coordinator).get(key)
    assert stored == new
    assert type(stored) is type(new)
