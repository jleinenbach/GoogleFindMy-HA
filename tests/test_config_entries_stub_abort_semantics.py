# tests/test_config_entries_stub_abort_semantics.py
"""Pin the config-entries double to Home Assistant's ``updates`` semantics.

``ConfigFlow._abort_if_unique_id_configured`` in Home Assistant merges its
``updates`` payload *flat* into the entry::

    self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})

The double in ``tests/helpers/config_entries_stub.py`` used to splat the payload
as keyword arguments instead (``async_update_entry(entry, **updates)``). That is
strictly more forgiving: a nested ``{"data": {...}}`` payload looks correct under
the double and silently becomes a stray ``data`` key inside ``entry.data`` under
the real core. A discovery defect lived behind exactly that gap, so the semantics
are pinned here rather than left to the callers that happen to exercise them.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from tests.helpers.config_entries_stub import install_config_entries_stubs


def _make_flow_class() -> type[Any]:
    """Return a freshly stubbed ``ConfigFlow`` class, isolated per test."""

    module = ModuleType("homeassistant.config_entries.__stub_for_test__")
    install_config_entries_stubs(module)
    return module.ConfigFlow  # type: ignore[attr-defined,no-any-return]


class _Entry:
    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.unique_id = "account-1"
        self.data: dict[str, Any] = {"google_email": "user@example.com", "token": "old"}


class _ConfigEntries:
    def __init__(self, entry: _Entry) -> None:
        self._entry = entry
        self.updated: list[tuple[Any, dict[str, Any]]] = []

    def async_entries(self, _domain: str | None = None) -> list[_Entry]:
        return [self._entry]

    def async_update_entry(self, target: Any, **kwargs: Any) -> None:
        self.updated.append((target, kwargs))
        if "data" in kwargs:
            target.data = dict(kwargs["data"])


class _Hass:
    def __init__(self, entry: _Entry) -> None:
        self.config_entries = _ConfigEntries(entry)


def _run_abort(updates: dict[str, Any]) -> tuple[_Entry, _ConfigEntries]:
    entry = _Entry()
    hass = _Hass(entry)
    flow_cls = _make_flow_class()
    flow = flow_cls()
    flow.hass = hass
    flow.unique_id = entry.unique_id
    flow._abort_if_unique_id_configured(updates=updates, reload=False)
    return entry, hass.config_entries


def test_stub_merges_updates_flat_into_entry_data() -> None:
    """A flat payload must merge, leaving untouched keys in place."""

    entry, manager = _run_abort({"token": "new"})

    assert manager.updated, "the double did not update the entry at all"
    _target, kwargs = manager.updated[0]
    assert set(kwargs) == {"data"}, (
        "Home Assistant passes the merged mapping as the single ``data`` keyword"
    )
    assert entry.data["token"] == "new"
    assert entry.data["google_email"] == "user@example.com"


def test_stub_does_not_absorb_a_nested_data_payload() -> None:
    """A nested payload must land as a stray key, exactly as the real core does.

    This is the failure the double used to hide. It is asserted, not fixed: the
    double's job is to reproduce Home Assistant, and production code must hand
    over a flat payload.
    """

    entry, _manager = _run_abort({"data": {"token": "new"}})

    assert entry.data["token"] == "old", (
        "a nested payload must NOT update the credential"
    )
    assert entry.data["data"] == {"token": "new"}, (
        "a nested payload lands as a stray 'data' key inside entry.data"
    )
