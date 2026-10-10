# tests/fixtures/registry_guard_probe/rule_kwargs_string.py
"""Violation probe for rule 2: the same names as string literals."""


def offend(kwargs: dict[str, object], entry_id: str) -> None:
    """Write an ownership kwarg through a dictionary, not a keyword."""
    kwargs.setdefault("remove_config_entry_id", entry_id)
