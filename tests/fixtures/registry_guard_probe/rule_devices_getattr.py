# tests/fixtures/registry_guard_probe/rule_devices_getattr.py
"""Violation probe for rule 4, string form: getattr on the devices mapping."""


def offend(device_registry: object) -> object:
    """Read the mapping dynamically, the shape a deny list of names missed."""
    return getattr(device_registry, "devices", None)
