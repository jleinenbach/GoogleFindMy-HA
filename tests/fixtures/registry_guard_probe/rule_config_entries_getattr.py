# tests/fixtures/registry_guard_probe/rule_config_entries_getattr.py
"""Violation probe for rule 3, string form: getattr on the shim."""


def offend(device: object, entry_id: str) -> bool:
    """Read the shim through getattr, which no attribute rule can see."""
    return entry_id in getattr(device, "config_entries", ())
