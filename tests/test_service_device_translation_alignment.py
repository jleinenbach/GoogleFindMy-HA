# tests/test_service_device_translation_alignment.py
"""Ensure the service device translation key is present across locales."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.googlefindmy.const import (
    SERVICE_DEVICE_TRANSLATION_KEY,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)


def _load_json(path: Path) -> Any:
    """Load JSON content from ``path`` using UTF-8 encoding."""

    return json.loads(path.read_text(encoding="utf-8"))


def test_service_device_translation_key_present() -> None:
    """Each locale must define the service device translation key."""

    base_dir = Path("custom_components/googlefindmy")
    translation_files = [
        base_dir / "strings.json",
        *sorted((base_dir / "translations").glob("*.json")),
    ]

    assert translation_files, "No translation files discovered"

    for path in translation_files:
        data = _load_json(path)
        assert isinstance(data, dict), f"{path} did not decode to a mapping"
        device_section = data.get("device")
        assert isinstance(
            device_section, dict
        ), f"{path} is missing the 'device' translation section"
        entry = device_section.get(SERVICE_DEVICE_TRANSLATION_KEY)
        assert (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"].strip()
        ), (
            f"{path} missing translated name for device key "
            f"{SERVICE_DEVICE_TRANSLATION_KEY!r}"
        )


def test_subentry_translation_titles_present() -> None:
    """Each locale must expose titles for tracker and service subentries."""

    base_dir = Path("custom_components/googlefindmy")
    translation_files = [
        base_dir / "strings.json",
        *sorted((base_dir / "translations").glob("*.json")),
    ]

    assert translation_files, "No translation files discovered"

    expected_keys = (
        SERVICE_SUBENTRY_KEY,
        TRACKER_SUBENTRY_KEY,
    )

    for path in translation_files:
        data = _load_json(path)
        assert isinstance(data, dict), f"{path} did not decode to a mapping"
        subentries = data.get("config_subentries")
        assert isinstance(
            subentries, dict
        ), f"{path} missing 'config_subentries' translations"
        for key in expected_keys:
            entry = subentries.get(key)
            assert (
                isinstance(entry, dict)
                and isinstance(entry.get("title"), str)
                and entry["title"].strip()
            ), f"{path} missing subentry title for key {key!r}"
