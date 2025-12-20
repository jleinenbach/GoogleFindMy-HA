# tests/test_device_identity_contract.py
from __future__ import annotations

from dataclasses import fields

from custom_components.googlefindmy.coordinator import DeviceIdentity


def test_device_identity_contract_includes_timebase_fields() -> None:
    """Ensure DeviceIdentity exposes fields required for EID resolution."""

    required_keys = {"pair_date", "secrets_creation_date"}
    available_fields = tuple(field.name for field in fields(DeviceIdentity))
    missing = required_keys.difference(available_fields)

    assert not missing, (
        "CRITICAL SCHEMA MISMATCH: The `DeviceIdentity` NamedTuple in "
        "`coordinator.py` is missing fields required for EID timebase "
        f"calculation: {missing}. You MUST add these fields to the "
        "NamedTuple definition to fix this test."
    )
