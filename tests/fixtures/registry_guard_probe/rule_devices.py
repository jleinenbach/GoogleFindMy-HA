# tests/fixtures/registry_guard_probe/rule_devices.py
"""Violation probe for rule 4: the DeviceRegistry.devices mapping."""


def offend(dev_reg: object) -> list[object]:
    """Iterate the mapping that 2026.8 deprecated."""
    return list(dev_reg.devices.values())  # type: ignore[attr-defined]
