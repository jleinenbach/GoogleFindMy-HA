# tests/test_keybackup_response_parser.py
"""Regression coverage for the key backup response parser."""

from __future__ import annotations

import json
import re

import pytest

from custom_components.googlefindmy.KeyBackup.response_parser import (
    get_fmdn_shared_key,
)


def test_get_fmdn_shared_key_returns_latest_epoch_bytes() -> None:
    """Ensure the parser selects the byte payload from the highest epoch."""

    payload = json.dumps(
        {
            "finder_hw": [
                {"epoch": 3, "key": {"0": 10, "1": 20, "2": 30}},
                {"epoch": 4, "key": {"0": 40, "1": 50, "2": 60}},
            ]
        }
    )

    result = get_fmdn_shared_key(payload)

    assert result == bytes([40, 50, 60])


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        (json.dumps(["not", "an", "object"]), "Vault keys payload must be a JSON object."),
        (
            json.dumps({"unexpected": []}),
            "Vault keys JSON does not contain a finder_hw list.",
        ),
        (
            json.dumps({"finder_hw": [{"epoch": 1, "key": {"0": "bad"}}]}),
            "No suitable key found in the vault keys.",
        ),
    ],
)
def test_get_fmdn_shared_key_rejects_malformed_payload(
    payload: str, expected_message: str
) -> None:
    """Verify malformed payloads raise ``ValueError`` with informative messages."""

    with pytest.raises(ValueError, match=re.escape(expected_message)):
        get_fmdn_shared_key(payload)


def test_get_fmdn_shared_key_raises_when_all_entries_filtered() -> None:
    """Ensure the parser raises when no usable entries remain after filtering."""

    payload = json.dumps(
        {
            "finder_hw": [
                {"epoch": "one", "key": {"0": 1}},
                {"epoch": 2, "key": [1, 2, 3]},
                "invalid",
            ]
        }
    )

    with pytest.raises(ValueError, match="No suitable key found in the vault keys."):
        get_fmdn_shared_key(payload)
