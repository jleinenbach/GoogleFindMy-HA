# tests/test_tracker_attribute_translation_parity.py
"""Guard sibling device_tracker entities against translation-key drift.

``GoogleFindMyDeviceTracker`` and ``GoogleFindMyLastLocationTracker`` share the
same ``_sync_location_attrs`` implementation, so they publish an identical set of
state attributes. Their ``strings.json`` (and every locale) entry must therefore
expose the same attribute keys; otherwise one entity renders raw attribute keys
instead of the localized label (Codex PR #1181 P3-1: ``plus_code`` reached the
``device`` tracker translation but not its ``last_location`` sibling).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BASE_DIR = Path("custom_components/googlefindmy")
_TRACKER_SIBLINGS = ("device", "last_location")


def _load_json(path: Path) -> Any:
    """Load JSON content from ``path`` using UTF-8 encoding."""

    return json.loads(path.read_text(encoding="utf-8"))


def _translation_files() -> list[Path]:
    """Return strings.json plus every locale translation file."""

    return [
        _BASE_DIR / "strings.json",
        *sorted((_BASE_DIR / "translations").glob("*.json")),
    ]


def test_sibling_tracker_state_attribute_keys_match() -> None:
    """The two device_tracker entities must share identical attribute keys.

    They inherit the same ``_sync_location_attrs`` and thus emit the same
    attributes; a divergence means one sibling exposes an untranslated key.
    """

    for path in _translation_files():
        data = _load_json(path)
        trackers = data.get("entity", {}).get("device_tracker", {})
        key_sets = {}
        for name in _TRACKER_SIBLINGS:
            attrs = trackers.get(name, {}).get("state_attributes", {})
            key_sets[name] = set(attrs)
        device_keys = key_sets["device"]
        last_location_keys = key_sets["last_location"]
        assert device_keys == last_location_keys, (
            f"{path.name}: device_tracker siblings expose different "
            f"state_attribute keys. "
            f"device-only={sorted(device_keys - last_location_keys)}, "
            f"last_location-only={sorted(last_location_keys - device_keys)}"
        )


def test_plus_code_attribute_translated_on_both_trackers() -> None:
    """Both trackers must localize the ``plus_code`` attribute in every locale."""

    for path in _translation_files():
        data = _load_json(path)
        trackers = data.get("entity", {}).get("device_tracker", {})
        for name in _TRACKER_SIBLINGS:
            attrs = trackers.get(name, {}).get("state_attributes", {})
            assert "plus_code" in attrs, (
                f"{path.name}: device_tracker.{name} is missing the "
                f"'plus_code' attribute translation."
            )
