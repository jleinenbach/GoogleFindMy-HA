# tests/fixtures/registry_guard_probe/rule_kwargs.py
"""Violation probe for rule 1: the deprecated ownership kwargs at a call site."""


def offend(dev_reg: object, device_id: str, entry_id: str) -> None:
    """Pass an ownership kwarg the way production did before 2026.8."""
    dev_reg.async_update_device(  # type: ignore[attr-defined]
        device_id, add_config_entry_id=entry_id
    )
