# tests/test_icons_translation_map.py
"""Ensure icons.json covers all entity translation keys with expected shapes."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_PATH = REPO_ROOT / "custom_components" / "googlefindmy" / "icons.json"
TRANSLATIONS_PATH = (
    REPO_ROOT / "custom_components" / "googlefindmy" / "translations" / "en.json"
)


def test_icons_file_covers_entity_translation_keys() -> None:
    """Icons metadata should exist for each translated entity."""

    with ICONS_PATH.open("r", encoding="utf-8") as file:
        icons = json.load(file)

    with TRANSLATIONS_PATH.open("r", encoding="utf-8") as file:
        translations = json.load(file)

    entity_icons = icons.get("entity")
    assert isinstance(entity_icons, dict), "icons.json must define an 'entity' section"

    entity_translations = translations.get("entity")
    assert isinstance(entity_translations, dict), (
        "translations/en.json must define an 'entity' section"
    )

    # Ensure both files agree on the available entity domains.
    icon_domains = set(entity_icons)
    translation_domains = {
        domain
        for domain, value in entity_translations.items()
        if isinstance(value, dict)
    }

    missing_domains = translation_domains - icon_domains
    assert not missing_domains, (
        f"Missing icon sections for domains: {sorted(missing_domains)}"
    )

    extra_domains = icon_domains - translation_domains
    assert not extra_domains, (
        f"icons.json has unexpected domains: {sorted(extra_domains)}"
    )

    for domain, translated_entities in sorted(entity_translations.items()):
        if not isinstance(translated_entities, dict):
            continue
        icons_for_domain = entity_icons.get(domain)
        assert isinstance(icons_for_domain, dict), (
            f"Missing icon mapping for entity domain '{domain}'"
        )

        for key in sorted(translated_entities):
            icon_definition = icons_for_domain.get(key)
            assert icon_definition is not None, (
                f"No icon mapping found for {domain}.{key} in icons.json"
            )
            default_icon = icon_definition.get("default")
            assert isinstance(default_icon, str) and default_icon, (
                f"Default icon missing or empty for {domain}.{key}"
            )

    # Binary sensors expose dynamic icons based on their state; verify mappings exist.
    expected_state_icons = {
        ("binary_sensor", "polling"): {"on", "off"},
        ("binary_sensor", "nova_auth_status"): {"on", "off"},
    }

    for (domain, key), required_states in expected_state_icons.items():
        icon_definition = entity_icons[domain][key]
        state_icons = icon_definition.get("state")
        assert isinstance(state_icons, dict), (
            f"State icon mapping missing for {domain}.{key}"
        )
        missing_states = required_states - set(state_icons)
        assert not missing_states, (
            f"State icon mapping incomplete for {domain}.{key}: missing {sorted(missing_states)}"
        )
        for state, icon in state_icons.items():
            assert isinstance(icon, str) and icon, (
                f"State '{state}' icon empty for {domain}.{key}"
            )
