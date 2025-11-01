# custom_components/googlefindmy/example_data_provider.py
"""Example fixtures for development and manual testing."""

from __future__ import annotations

from typing import Final

_EXAMPLES: Final[dict[str, str]] = {
    "sample_identity_key": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "sample_owner_key": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
    "sample_device_id": "example_device_12345",
    "sample_encrypted_data": "example_encrypted_payload",
}


def get_example_data(key: str) -> str:
    """Return example data for the given key."""

    return _EXAMPLES.get(key, "")
