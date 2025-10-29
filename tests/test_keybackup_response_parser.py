# tests/test_keybackup_response_parser.py
"""Regression coverage for selecting the newest FMDN shared key."""

from __future__ import annotations

import json

from custom_components.googlefindmy.KeyBackup import response_parser


def test_get_fmdn_shared_key_returns_highest_epoch() -> None:
    """Ensure the parser returns the key bytes belonging to the highest epoch."""
    payload = json.dumps(
        {
            "finder_hw": [
                {"epoch": 1, "key": {"0": 1, "1": 2}},
                {"epoch": 2, "key": {"0": 3, "1": 4}},
                {"epoch": 3, "key": {"0": 5, "1": 6}},
            ]
        }
    )

    result = response_parser.get_fmdn_shared_key(payload)

    assert result == bytes([5, 6])
