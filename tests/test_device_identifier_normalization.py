# tests/test_device_identifier_normalization.py
"""Regression coverage for device identifier normalization flows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentry

from custom_components.googlefindmy import (
    ConfigEntrySubEntryManager,
    _migrate_entry_identifier_namespaces,
    async_remove_config_entry_device,
)
from custom_components.googlefindmy.const import (
    DOMAIN,
    OPT_IGNORED_DEVICES,
    OPT_OPTIONS_SCHEMA_VERSION,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
    coerce_ignored_mapping,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.config_flow import ConfigEntriesDomainUniqueIdLookupMixin

IGNORED_AT_PRIMARY = 1234
IGNORED_AT_SECONDARY = 42
SCHEMA_VERSION = 2


class _ConfigEntriesStub(ConfigEntriesDomainUniqueIdLookupMixin):
    """Minimal config_entries manager capturing updates."""

    def __init__(self) -> None:
        self.updated_entries: list[dict[str, object] | None] = []
        self.updated_subentries: list[tuple[str, dict[str, object] | None]] = []
        self.stored_entries: list[SimpleNamespace] = []

    def async_update_entry(
        self,
        entry: SimpleNamespace,
        *,
        options: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        if options is not None:
            entry.options = options
        self.updated_entries.append(options)

    def async_update_subentry(
        self,
        entry: SimpleNamespace,
        subentry: ConfigSubentry,
        *,
        data: dict[str, object] | None = None,
        translation_key: str | None = None,
        **_: object,
    ) -> bool:
        if data is not None:
            entry.subentries[subentry.subentry_id] = ConfigSubentry(
                data=data,
                subentry_type=subentry.subentry_type,
                title=subentry.title,
                unique_id=subentry.unique_id,
                subentry_id=subentry.subentry_id,
                translation_key=translation_key,
            )
        self.updated_subentries.append((subentry.subentry_id, data))
        return True


def test_async_remove_config_entry_device_normalizes_identifier(monkeypatch) -> None:
    """Removal hook uses canonical IDs for purge and ignored device metadata."""

    config_entries = _ConfigEntriesStub()
    coordinator = object.__new__(GoogleFindMyCoordinator)
    coordinator.purge_device = MagicMock()  # type: ignore[attr-defined]

    entry = SimpleNamespace(
        entry_id="entry-1",
        options={
            OPT_IGNORED_DEVICES: {
                "device-123": {
                    "name": "Legacy Device",
                    "aliases": ["Legacy Alias"],
                    "ignored_at": 777,
                    "source": "registry",
                }
            }
        },
        title="Test Entry",
        runtime_data=coordinator,
    )

    config_entries.stored_entries.append(entry)

    hass = SimpleNamespace(
        config_entries=config_entries,
        data={
            DOMAIN: {
                "entries": {entry.entry_id: SimpleNamespace(coordinator=coordinator)}
            }
        },
    )

    device_entry = SimpleNamespace(
        config_entries={entry.entry_id},
        identifiers=(
            (DOMAIN, "entry-1:tracker:device-123"),
            (DOMAIN, "entry-1:device-123"),
        ),
        name_by_user=None,
        name="Device Name",
    )

    monkeypatch.setattr(
        "custom_components.googlefindmy.time.time",
        lambda: 1234,
    )

    assert (
        asyncio.run(async_remove_config_entry_device(hass, entry, device_entry)) is True
    )

    coordinator.purge_device.assert_called_once_with("device-123")  # type: ignore[attr-defined]
    assert config_entries.updated_entries, (
        "Expected options update with canonical identifier"
    )

    updated_options = entry.options
    ignored = updated_options[OPT_IGNORED_DEVICES]
    assert "device-123" in ignored
    assert "entry-1:device-123" not in ignored
    assert "entry-1:tracker:device-123" in ignored["device-123"]["aliases"]

    metadata = ignored["device-123"]
    assert metadata["name"] == "Device Name"
    assert metadata["ignored_at"] == IGNORED_AT_PRIMARY
    assert "Legacy Device" in metadata["aliases"]
    assert "Legacy Alias" in metadata["aliases"]
    assert updated_options[OPT_OPTIONS_SCHEMA_VERSION] == SCHEMA_VERSION


def test_migrate_entry_identifier_namespaces_updates_subentries() -> None:
    """Migration strips entry prefix from options and subentry visibility lists."""

    config_entries = _ConfigEntriesStub()

    entry = SimpleNamespace(
        entry_id="entry-2",
        options={
            OPT_IGNORED_DEVICES: {
                "entry-2:device-1": {
                    "name": "Legacy Name",
                    "aliases": ["Legacy Alias"],
                    "ignored_at": IGNORED_AT_SECONDARY,
                    "source": "imported",
                }
            }
        },
        subentries={},
        title="Entry 2",
    )

    config_entries.stored_entries.append(entry)

    subentry = ConfigSubentry(
        data={
            "group_key": TRACKER_SUBENTRY_KEY,
            "visible_device_ids": ["entry-2:device-1"],
        },
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core",
        unique_id="entry-2-core",
    )
    entry.subentries[subentry.subentry_id] = subentry

    hass = SimpleNamespace(config_entries=config_entries)

    _migrate_entry_identifier_namespaces(hass, entry)

    assert config_entries.updated_entries, "Expected migration to rewrite options"
    assert config_entries.updated_subentries, (
        "Expected migration to rewrite subentry data"
    )

    updated_options = entry.options
    ignored = updated_options[OPT_IGNORED_DEVICES]
    assert "device-1" in ignored
    metadata = ignored["device-1"]
    assert metadata["name"] == "Legacy Name"
    assert metadata["ignored_at"] == IGNORED_AT_SECONDARY
    assert "Legacy Alias" in metadata["aliases"]
    assert updated_options[OPT_OPTIONS_SCHEMA_VERSION] == SCHEMA_VERSION

    manager = ConfigEntrySubEntryManager(hass, entry)
    managed = manager.get(TRACKER_SUBENTRY_KEY)
    assert managed is not None
    assert tuple(managed.data.get("visible_device_ids", ())) == ("device-1",)


@pytest.mark.parametrize(
    "bad_value",
    [
        float("nan"),  # int(nan) -> ValueError
        float("inf"),  # int(inf) -> OverflowError
        float("-inf"),  # int(-inf) -> OverflowError
        True,  # bool is an int subclass; a truthy epoch of 1 is garbage
    ],
    ids=["nan", "inf", "-inf", "bool"],
)
def test_coerce_ignored_mapping_survives_invalid_ignored_at(bad_value: object) -> None:
    """An invalid ``ignored_at`` is dropped to the default, never raised.

    ``int()`` on a non-finite float raises (``ValueError``/``OverflowError``),
    which would break the ignored-devices coercion on a corrupt ``.storage``
    value; a ``bool`` would silently become the absurd epoch ``1``. Every one of
    these must instead fall back to the finite default without raising.
    """
    result, _changed = coerce_ignored_mapping(
        {"device-1": {"name": "Dev", "ignored_at": bad_value}}
    )

    assert "device-1" in result
    metadata = result["device-1"]
    assert metadata["name"] == "Dev"
    # ignored_at fell back to the finite _now_epoch() default: a positive epoch,
    # never NaN, never 0/None, and never the bool-derived 1 (a "return 0 instead
    # of None" or dropped-guard helper mutation would leave a wrong value here).
    ignored_at = metadata["ignored_at"]
    assert isinstance(ignored_at, int)
    assert ignored_at > 1
