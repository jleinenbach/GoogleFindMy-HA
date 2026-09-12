# tests/fixtures/registry_guard_probe/rule_async_get_device.py
"""Violation probe for rule 5: async_get_device, direct and via getattr."""


def offend_direct(dev_reg: object, identifier: tuple[str, str]) -> object:
    """Look a device up by identifier set."""
    return dev_reg.async_get_device({identifier})  # type: ignore[attr-defined]


def offend_getattr(dev_reg: object, identifier: tuple[str, str]) -> object:
    """The dynamic form that hid four sites from the first sweep."""
    lookup = getattr(dev_reg, "async_get_device", None)
    return lookup({identifier}) if lookup else None
