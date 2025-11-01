# tests/test_config_schema.py
"""Ensure integration exposes a config-entry-only CONFIG_SCHEMA."""

from __future__ import annotations

import importlib

from voluptuous import Schema


def test_config_schema_is_defined() -> None:
    """CONFIG_SCHEMA must be provided for hassfest validation."""

    module = importlib.import_module("custom_components.googlefindmy")

    assert hasattr(module, "CONFIG_SCHEMA"), "CONFIG_SCHEMA is missing"
    assert isinstance(module.CONFIG_SCHEMA, Schema)
    # The schema should accept an empty configuration.
    assert module.CONFIG_SCHEMA({}) == {}
