from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    """Load JSON content from ``path`` using UTF-8 encoding."""

    return json.loads(path.read_text(encoding="utf-8"))


def test_semantic_location_steps_and_errors_present() -> None:
    """All locales must expose semantic location step labels and duplicate-name error."""

    base_dir = Path("custom_components/googlefindmy")
    translation_files = [
        base_dir / "strings.json",
        *sorted((base_dir / "translations").glob("*.json")),
    ]

    assert translation_files, "No translation files discovered"

    required_steps = {
        "semantic_locations_menu": ("title", "description", "menu_options"),
        "semantic_location_edit": ("title", "description", "data"),
    }
    required_fields = ("semantic_name", "latitude", "longitude", "accuracy")

    for path in translation_files:
        data = _load_json(path)
        assert isinstance(data, dict), f"{path} did not decode to a mapping"

        options = data.get("options")
        assert isinstance(options, dict), f"{path} missing 'options' translations"

        steps = options.get("step")
        assert isinstance(steps, dict), f"{path} missing 'options.step' translations"

        for step_key, expected_fields in required_steps.items():
            step_block = steps.get(step_key)
            assert isinstance(step_block, dict), f"{path} missing step {step_key!r}"
            for field in expected_fields:
                value = step_block.get(field)
                assert isinstance(value, (dict, str)), (
                    f"{path} missing field {field!r} for {step_key!r}"
                )

        edit_block = steps["semantic_location_edit"]
        edit_data = edit_block.get("data")
        assert isinstance(edit_data, dict), f"{path} missing data for semantic edit step"
        for field in required_fields:
            value = edit_data.get(field)
            assert isinstance(value, str) and value.strip(), (
                f"{path} missing label for semantic edit field {field!r}"
            )

        errors = options.get("error")
        assert isinstance(errors, dict), f"{path} missing 'options.error' translations"
        duplicate_entry = errors.get("duplicate_name")
        assert isinstance(duplicate_entry, str) and duplicate_entry.strip(), (
            f"{path} missing duplicate_name error translation"
        )
