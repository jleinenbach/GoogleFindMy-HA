# tests/test_entry_data_nesting_cleanup.py
"""Cleanup of the nested ``data`` key an earlier discovery import left behind.

Discovery handed Home Assistant a nested ``{"data": {...}}`` payload, which the
core merged verbatim into ``entry.data`` (see
``tests/test_config_flow_discovery.py`` for the fix on the writing side). The
credentials therefore never reached the level that is read, and every further
import wrapped the whole mapping again, so the nesting grows by one level per
run. A live installation was found at depth 3.

The tests below cover the reading side: existing entries have to heal on their
own at the next start, without anyone editing ``.storage/core.config_entries``
by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import custom_components.googlefindmy as integration
from tests.helpers.config_entries_stub import make_config_entry

INIT_PATH = Path(integration.__file__)


class _ConfigEntriesRecorder:
    """Minimal manager stub recording the writes the cleanup performs."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def async_update_entry(self, target: Any, **kwargs: Any) -> bool:
        self.updates.append(kwargs)
        if "data" in kwargs:
            target.data = dict(kwargs["data"])
        return True


class _Hass:
    def __init__(self) -> None:
        self.config_entries = _ConfigEntriesRecorder()


def _nested(depth: int, *, token: str = "buried") -> dict[str, Any]:
    """Build the payload shape the defect produced at ``depth`` levels."""

    payload: dict[str, Any] = {"oauth_token": token, "secrets_data": {"k": "v"}}
    for _ in range(depth):
        payload = {"data": payload}
    return payload


def test_strip_reports_zero_and_returns_an_equal_mapping_when_clean() -> None:
    """A healthy entry must come back unchanged, so callers can skip the write."""

    data = {"google_email": "user@example.com", "oauth_token": "aas_et/CURRENT"}

    cleaned, depth = integration._strip_stray_nested_entry_data(data)

    assert depth == 0
    assert cleaned == data


def test_strip_removes_every_level_at_once() -> None:
    """Depth 3 is the shape found in the wild; one level is not enough.

    A fix that peels a single level would leave ``{"data": {...}}`` behind and
    pass a depth-1 test, which is why this case is the one that is asserted.
    """

    data = {"oauth_token": "aas_et/TOP", **_nested(3)}

    cleaned, depth = integration._strip_stray_nested_entry_data(data)

    assert depth == 3
    assert "data" not in cleaned
    assert cleaned == {"oauth_token": "aas_et/TOP"}


def test_strip_leaves_a_non_mapping_data_value_alone() -> None:
    """Only the nesting is removed, not any future setting that shares the name."""

    data = {"oauth_token": "aas_et/TOP", "data": "not a mapping"}

    cleaned, depth = integration._strip_stray_nested_entry_data(data)

    assert depth == 0
    assert cleaned == data


def test_cleanup_writes_once_and_is_idempotent() -> None:
    """The affected entry heals, and a second run has nothing left to do."""

    hass = _Hass()
    entry = make_config_entry(
        entry_id="affected-entry",
        data={"google_email": "user@example.com", **_nested(2)},
    )

    integration._async_strip_stray_nested_entry_data(hass, entry)  # type: ignore[arg-type]

    assert len(hass.config_entries.updates) == 1
    assert set(hass.config_entries.updates[0]) == {"data"}
    assert entry.data == {"google_email": "user@example.com"}

    integration._async_strip_stray_nested_entry_data(hass, entry)  # type: ignore[arg-type]

    assert len(hass.config_entries.updates) == 1, "a second run must not write again"


def test_cleanup_does_not_touch_a_healthy_entry() -> None:
    """No write at all, so an untouched entry keeps its modification watermark."""

    hass = _Hass()
    before = {"google_email": "user@example.com", "oauth_token": "aas_et/CURRENT"}
    entry = make_config_entry(entry_id="healthy-entry", data=dict(before))

    integration._async_strip_stray_nested_entry_data(hass, entry)  # type: ignore[arg-type]

    assert hass.config_entries.updates == []
    assert entry.data == before


def test_cleanup_is_wired_into_entry_setup() -> None:
    """A helper nobody calls would heal nothing.

    Asserted against the source rather than by driving a full entry setup: the
    point here is the wiring, and reading it is cheap and unambiguous.
    """

    tree = ast.parse(INIT_PATH.read_text(encoding="utf-8"))
    setup = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_setup_entry"
        ),
        None,
    )
    assert setup is not None

    called = {
        node.func.id
        for node in ast.walk(setup)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "_async_strip_stray_nested_entry_data" in called, (
        "async_setup_entry no longer runs the cleanup"
    )
