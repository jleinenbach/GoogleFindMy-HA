# tests/test_key_backup_response_parser.py
"""Tests for the key backup response parser selecting valid keys."""

from __future__ import annotations

import json

import pytest

from custom_components.googlefindmy.KeyBackup import response_parser


def test_get_fmdn_shared_key_selects_newest_valid_epoch() -> None:
    """Ensure the parser picks the newest valid epoch and ignores malformed entries."""
    payload = json.dumps(
        {
            "finder_hw": [
                {"epoch": 1, "key": {"0": 1, "1": 2}},
                {"epoch": 2, "key": {"0": 3, "1": 4}},
                {"epoch": 3, "key": {"0": 5, "1": "invalid"}},
                {"epoch": 6, "key": "not-a-dict"},
                {"epoch": 5, "key": {"0": 10, "1": 11}},
                "completely-invalid",
            ]
        }
    )

    result = response_parser.get_fmdn_shared_key(payload)

    assert result == bytes([10, 11])


def test_get_fmdn_shared_key_raises_when_no_valid_key_found() -> None:
    """Verify the parser raises when no usable key remains after validation."""
    payload = json.dumps(
        {
            "finder_hw": [
                {"epoch": "epoch", "key": {"0": 1}},
                {"epoch": 1, "key": [0, 1]},
                "still-invalid",
            ]
        }
    )

    with pytest.raises(ValueError, match="No suitable key"):
        response_parser.get_fmdn_shared_key(payload)
