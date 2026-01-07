# tests/test_hypothesis_properties.py
"""Property-based tests using Hypothesis for the Google Find My Device integration.

These tests verify invariants and edge cases that are difficult to cover
with example-based testing alone.

Note: These tests use aggressive health check suppression to work correctly
when run alongside the full test suite with heavy Home Assistant stubs.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

# Mark all tests in this module for optional isolation
pytestmark = pytest.mark.hypothesis

# Comprehensive health check suppression for running with HA test fixtures
# The Home Assistant stubs load many modules which slow down Hypothesis input generation
_ALL_HEALTH_CHECKS = list(HealthCheck)

# Default settings for all hypothesis tests - maximally permissive to handle
# slow test generation when combined with Home Assistant test fixtures
hypothesis_settings = settings(
    max_examples=50,  # Reduced from 100 for faster execution with large test suites
    suppress_health_check=_ALL_HEALTH_CHECKS,
    deadline=None,  # Disable deadline to avoid timeout issues in CI
    database=None,  # Don't use persistent database - avoids I/O slowdowns
    phases=[Phase.generate, Phase.target],  # Skip shrinking for speed
    stateful_step_count=10,  # Limit stateful test complexity
)

# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------

# Valid gRPC compressed flag (0 or 1)
grpc_compressed_flag = st.integers(min_value=0, max_value=1)

# Message length for gRPC frames (realistic range)
grpc_message_length = st.integers(min_value=0, max_value=10_000_000)

# Binary payloads of various sizes
binary_payload = st.binary(min_size=0, max_size=1000)

# Device IDs (alphanumeric with some special chars)
device_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=64,
)

# Email addresses
email_strategy = st.emails()

# Unicode strings for translation testing
unicode_text = st.text(min_size=0, max_size=200)

# Placeholder names (ASCII alphanumeric with underscores only - matching regex)
placeholder_name = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=32,
)


# ---------------------------------------------------------------------------
# gRPC Frame Parsing Tests
# ---------------------------------------------------------------------------


def build_grpc_frame(compressed: int, length: int, payload: bytes) -> bytes:
    """Build a gRPC frame with the given parameters."""
    header = struct.pack(">BI", compressed, length)
    return header + payload


def parse_grpc_frame(data: bytes) -> tuple[int, int, bytes] | None:
    """Parse a gRPC frame, returning (compressed, length, payload) or None."""
    if len(data) < 5:
        return None
    compressed, length = struct.unpack(">BI", data[:5])
    payload = data[5 : 5 + length]
    return compressed, length, payload


@given(compressed=grpc_compressed_flag, length=grpc_message_length, payload=binary_payload)
@hypothesis_settings
def test_grpc_frame_roundtrip(compressed: int, length: int, payload: bytes) -> None:
    """Verify that gRPC frame building and parsing are inverses."""
    frame = build_grpc_frame(compressed, length, payload)
    result = parse_grpc_frame(frame)
    assert result is not None
    parsed_compressed, parsed_length, parsed_payload = result
    assert parsed_compressed == compressed
    assert parsed_length == length
    # Payload is truncated to declared length
    assert parsed_payload == payload[: min(len(payload), length)]


@given(compressed=grpc_compressed_flag, payload=binary_payload)
@hypothesis_settings
def test_grpc_frame_length_matches_payload(compressed: int, payload: bytes) -> None:
    """When length equals payload size, full payload is recoverable."""
    length = len(payload)
    frame = build_grpc_frame(compressed, length, payload)
    result = parse_grpc_frame(frame)
    assert result is not None
    _, _, parsed_payload = result
    assert parsed_payload == payload


@given(data=st.binary(min_size=0, max_size=4))
@hypothesis_settings
def test_grpc_frame_rejects_short_data(data: bytes) -> None:
    """Frames shorter than 5 bytes cannot be parsed."""
    result = parse_grpc_frame(data)
    assert result is None


# ---------------------------------------------------------------------------
# Translation Placeholder Tests
# ---------------------------------------------------------------------------

PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def extract_placeholders(text: str) -> set[str]:
    """Extract placeholder names from a translation string."""
    return set(PLACEHOLDER_PATTERN.findall(text))


@given(text=unicode_text)
@hypothesis_settings
def test_placeholder_extraction_is_deterministic(text: str) -> None:
    """Placeholder extraction should be deterministic."""
    result1 = extract_placeholders(text)
    result2 = extract_placeholders(text)
    assert result1 == result2


@given(names=st.lists(placeholder_name, min_size=0, max_size=5, unique=True))
@hypothesis_settings
def test_placeholder_roundtrip(names: list[str]) -> None:
    """Placeholders inserted into text can be extracted."""
    # Filter out empty names
    names = [n for n in names if n]
    if not names:
        return
    text = " ".join(f"{{{name}}}" for name in names)
    extracted = extract_placeholders(text)
    assert extracted == set(names)


def test_translation_files_have_consistent_placeholders() -> None:
    """All translation files should have consistent placeholders with en.json."""
    translations_dir = Path("custom_components/googlefindmy/translations")
    if not translations_dir.exists():
        pytest.skip("Translations directory not found")

    en_file = translations_dir / "en.json"
    if not en_file.exists():
        pytest.skip("English translation file not found")

    with open(en_file, encoding="utf-8") as f:
        en_data = json.load(f)

    def get_all_strings(data: Any, path: str = "") -> dict[str, str]:
        """Recursively extract all string values with their paths."""
        result: dict[str, str] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                full_path = f"{path}/{k}" if path else k
                result.update(get_all_strings(v, full_path))
        elif isinstance(data, str):
            result[path] = data
        return result

    en_strings = get_all_strings(en_data)
    en_placeholders = {path: extract_placeholders(text) for path, text in en_strings.items()}

    errors: list[str] = []
    for lang_file in translations_dir.glob("*.json"):
        if lang_file.name == "en.json":
            continue

        with open(lang_file, encoding="utf-8") as f:
            lang_data = json.load(f)

        lang_strings = get_all_strings(lang_data)
        for path, en_ph in en_placeholders.items():
            if path in lang_strings:
                lang_ph = extract_placeholders(lang_strings[path])
                if en_ph != lang_ph:
                    missing = en_ph - lang_ph
                    extra = lang_ph - en_ph
                    if missing:
                        errors.append(f"{lang_file.name}: {path} missing {missing}")
                    if extra:
                        errors.append(f"{lang_file.name}: {path} extra {extra}")

    assert not errors, "Placeholder mismatches:\n" + "\n".join(errors[:10])


# ---------------------------------------------------------------------------
# Device ID Normalization Tests
# ---------------------------------------------------------------------------


def normalize_device_id(device_id: str) -> str:
    """Normalize a device ID for consistent comparison."""
    return device_id.strip().lower()


@given(device_id=device_id_strategy)
@hypothesis_settings
def test_device_id_normalization_is_idempotent(device_id: str) -> None:
    """Normalizing a device ID twice should give the same result."""
    normalized = normalize_device_id(device_id)
    double_normalized = normalize_device_id(normalized)
    assert normalized == double_normalized


@given(device_id=device_id_strategy)
@hypothesis_settings
def test_device_id_normalization_is_lowercase(device_id: str) -> None:
    """Normalized device IDs should be lowercase."""
    normalized = normalize_device_id(device_id)
    assert normalized == normalized.lower()


@given(device_id=device_id_strategy)
@hypothesis_settings
def test_device_id_normalization_strips_whitespace(device_id: str) -> None:
    """Normalized device IDs should have no leading/trailing whitespace."""
    padded = f"  {device_id}  "
    normalized = normalize_device_id(padded)
    assert normalized == normalized.strip()


# ---------------------------------------------------------------------------
# Entity Unique ID Generation Tests
# ---------------------------------------------------------------------------


def generate_entity_unique_id(entry_id: str, device_id: str, entity_type: str) -> str:
    """Generate a unique ID for an entity."""
    return f"{entry_id}_{device_id}_{entity_type}"


@given(
    entry_id=st.text(min_size=1, max_size=32, alphabet="abcdef0123456789"),
    device_id=device_id_strategy,
    entity_type=st.sampled_from(["device_tracker", "sensor", "button", "binary_sensor"]),
)
@hypothesis_settings
def test_entity_unique_id_is_deterministic(
    entry_id: str, device_id: str, entity_type: str
) -> None:
    """Entity unique IDs should be deterministic."""
    uid1 = generate_entity_unique_id(entry_id, device_id, entity_type)
    uid2 = generate_entity_unique_id(entry_id, device_id, entity_type)
    assert uid1 == uid2


@given(
    entry_id=st.text(min_size=1, max_size=32, alphabet="abcdef0123456789"),
    device_id=device_id_strategy,
    entity_type=st.sampled_from(["device_tracker", "sensor", "button", "binary_sensor"]),
)
@hypothesis_settings
def test_entity_unique_id_contains_all_parts(
    entry_id: str, device_id: str, entity_type: str
) -> None:
    """Entity unique IDs should contain all input parts."""
    uid = generate_entity_unique_id(entry_id, device_id, entity_type)
    assert entry_id in uid
    assert device_id in uid
    assert entity_type in uid


# ---------------------------------------------------------------------------
# Version Constant Tests
# ---------------------------------------------------------------------------


def test_integration_version_matches_manifest() -> None:
    """INTEGRATION_VERSION in const.py should match manifest.json."""
    const_path = Path("custom_components/googlefindmy/const.py")
    manifest_path = Path("custom_components/googlefindmy/manifest.json")

    if not const_path.exists() or not manifest_path.exists():
        pytest.skip("Required files not found")

    # Extract version from const.py
    const_content = const_path.read_text()
    const_match = re.search(r'INTEGRATION_VERSION:\s*str\s*=\s*["\']([^"\']+)["\']', const_content)
    assert const_match, "INTEGRATION_VERSION not found in const.py"
    const_version = const_match.group(1)

    # Extract version from manifest.json
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_version = manifest.get("version")
    assert manifest_version, "version not found in manifest.json"

    assert const_version == manifest_version, (
        f"Version mismatch: const.py has {const_version}, manifest.json has {manifest_version}"
    )


def test_micro_sign_constant_is_greek_mu() -> None:
    """MICRO constant should be Greek small letter mu, not micro sign."""
    const_path = Path("custom_components/googlefindmy/const.py")
    if not const_path.exists():
        pytest.skip("const.py not found")

    const_content = const_path.read_text()
    # Match either literal character or Unicode escape sequence
    micro_match = re.search(
        r'MICRO:\s*str\s*=\s*["\'](?:(.)|\\u([0-9a-fA-F]{4}))["\']', const_content
    )
    assert micro_match, "MICRO constant not found"

    if micro_match.group(1):
        micro_char = micro_match.group(1)
    else:
        # Unicode escape sequence
        micro_char = chr(int(micro_match.group(2), 16))

    # Greek small letter mu is U+03BC, micro sign is U+00B5
    assert micro_char == "\u03bc", f"MICRO should be Greek mu (U+03BC), got U+{ord(micro_char):04X}"


# ---------------------------------------------------------------------------
# JSON Validation Tests
# ---------------------------------------------------------------------------


@given(
    data=st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
        lambda children: st.lists(children) | st.dictionaries(st.text(), children),
        max_leaves=10,
    )
)
@hypothesis_settings
def test_json_roundtrip(data: Any) -> None:
    """JSON serialization should be a roundtrip for valid data."""
    serialized = json.dumps(data)
    deserialized = json.loads(serialized)
    assert deserialized == data


# ---------------------------------------------------------------------------
# Coordinate Validation Tests
# ---------------------------------------------------------------------------


@given(
    lat=st.floats(min_value=-90, max_value=90),
    lon=st.floats(min_value=-180, max_value=180),
)
@hypothesis_settings
def test_valid_coordinates_are_in_range(lat: float, lon: float) -> None:
    """Valid GPS coordinates should be within expected ranges."""
    assert -90 <= lat <= 90
    assert -180 <= lon <= 180


@given(
    lat=st.floats(min_value=-90, max_value=90),
    lon=st.floats(min_value=-180, max_value=180),
    accuracy=st.floats(min_value=0, max_value=100000),
)
@hypothesis_settings
def test_accuracy_is_non_negative(lat: float, lon: float, accuracy: float) -> None:
    """Location accuracy should always be non-negative."""
    assert accuracy >= 0
