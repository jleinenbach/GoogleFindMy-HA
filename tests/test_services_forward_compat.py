# tests/test_services_forward_compat.py
"""Validate service registration wrappers and translation compatibility."""

from __future__ import annotations

import json
import re
from pathlib import Path

EXPECTED_SERVICES = {
    "locate_device",
    "locate_external",
    "play_sound",
    "stop_sound",
    "refresh_device_urls",
    "rebuild_registry",
}


def _top_level_yaml_keys(text: str) -> set[str]:
    """Extract top-level keys from a simple YAML document."""

    keys: set[str] = set()
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            candidate = line.rstrip(":").strip()
            if candidate:
                keys.add(candidate)
    return keys


def test_services_yaml_matches_expected_entries(integration_root: Path) -> None:
    """services.yaml should list the expected Home Assistant service names."""

    services_yaml = (integration_root / "services.yaml").read_text(encoding="utf-8")
    top_level_keys = _top_level_yaml_keys(services_yaml)
    assert EXPECTED_SERVICES <= top_level_keys


TRANSLATION_KEY_PATTERN = re.compile(r"translation_key=\"([a-z0-9_]+)\"")


def test_services_module_exports_registration(integration_root: Path) -> None:
    """services.py must expose async_register_services and aligned translations."""

    services_module = (integration_root / "services.py").read_text(encoding="utf-8")
    assert "async def async_register_services" in services_module
    assert "ServiceValidationError" in services_module

    used_keys = set(TRANSLATION_KEY_PATTERN.findall(services_module))
    translations = json.loads(
        (integration_root / "translations" / "en.json").read_text(encoding="utf-8")
    )

    def _collect_keys(obj: object) -> set[str]:
        if isinstance(obj, dict):
            keys = set(obj.keys())
            for value in obj.values():
                keys |= _collect_keys(value)
            return keys
        if isinstance(obj, (list, tuple)):
            keys: set[str] = set()
            for item in obj:
                keys |= _collect_keys(item)
            return keys
        return set()

    available_keys = _collect_keys(translations)
    missing = sorted(key for key in used_keys if key not in available_keys)
    assert not missing, f"missing service translations: {missing}"


def test_services_module_scans_runtime_entries(integration_root: Path) -> None:
    """The services helper should iterate runtime_data containers for active entries."""

    services_module = (integration_root / "services.py").read_text(encoding="utf-8")
    assert "def _iter_runtimes" in services_module
    assert "entry.runtime_data" in services_module
    assert "hass.data.setdefault(DOMAIN" in services_module
