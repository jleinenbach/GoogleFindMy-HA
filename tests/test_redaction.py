# tests/test_redaction.py
"""Tests for the dependency-free redaction helpers in ``redaction``."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from custom_components.googlefindmy import redaction
from custom_components.googlefindmy.redaction import (
    REDACTED,
    async_redact_data,
    describe_payload,
)


def test_redacts_matching_keys_at_any_depth() -> None:
    data = {
        "keep": "visible",
        "vaultKeys": "super-secret",
        "nested": {"vaultKeys": "also-secret", "other": 1},
        "listed": [{"vaultKeys": "third"}],
    }

    result = async_redact_data(data, {"vaultKeys"})

    assert result["keep"] == "visible"
    assert result["vaultKeys"] == REDACTED
    assert result["nested"]["vaultKeys"] == REDACTED
    assert result["nested"]["other"] == 1
    assert result["listed"][0]["vaultKeys"] == REDACTED
    # the input is left untouched
    assert data["vaultKeys"] == "super-secret"


def test_passes_through_scalars_and_keeps_empty_values() -> None:
    assert async_redact_data("plain", {"plain"}) == "plain"
    assert async_redact_data({"vaultKeys": None}, {"vaultKeys"}) == {"vaultKeys": None}
    assert async_redact_data({"vaultKeys": ""}, {"vaultKeys"}) == {"vaultKeys": ""}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abcdef", "str len=6"),
        (b"ab", "bytes len=2"),
        ({"a": 1}, "dict len=1"),
        ([], "list len=0"),
        (None, "None"),
        (123, "int"),
    ],
)
def test_describe_payload_reports_shape_never_content(
    value: object, expected: str
) -> None:
    assert describe_payload(value) == expected


def test_describe_payload_never_echoes_the_value() -> None:
    secret = "0123456789abcdef" * 4
    assert secret not in describe_payload(secret)


def test_module_stays_free_of_home_assistant_imports() -> None:
    """The CLI login run imports this module; HA must not come with it.

    Guards the layering reason this module exists at all: ``diagnostics`` pulls
    ``homeassistant.config_entries``, ``homeassistant.core`` and both registries
    at module level.
    """

    tree = ast.parse(Path(redaction.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "homeassistant" not in imported
    assert imported <= {"__future__", "collections", "typing"}
