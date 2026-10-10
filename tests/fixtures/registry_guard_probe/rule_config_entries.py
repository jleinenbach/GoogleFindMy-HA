# tests/fixtures/registry_guard_probe/rule_config_entries.py
"""Violation probe for rule 3: the DeviceEntry.config_entries shim."""


def offend(device: object) -> int:
    """Read the shim that now always yields exactly one entry."""
    return len(device.config_entries)  # type: ignore[attr-defined]
