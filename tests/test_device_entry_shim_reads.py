# tests/test_device_entry_shim_reads.py
"""The shared ownership accessors never read a reporting DeviceEntry shim.

Home Assistant 2026.10 reports every read of ``DeviceEntry.config_entries``,
``config_entries_subentries`` and ``primary_config_entry``
(``_report_deprecated_config_entries_property`` in
``homeassistant/helpers/device_registry.py``, ``breaks_in_ha_version=
"2027.10.0"``).  For a custom integration that is a report on every call, logged
once per call site; from 2027.10 it is a failure.  The integration reaches device ownership through three
shared accessors in ``coordinator/helpers/registry.py``; this module runs them
against a real device entry with a recorder bound into the registry module and
requires zero reports.

The static side lives in ``tests/test_guard_device_registry_kwargs.py`` (rules 3
and 3b): it sees call sites no test exercises, but it cannot see what a helper
reads at run time.  This module is the run-time side.

A recorder that observes nothing looks exactly like code that triggers nothing,
so the canary below must see a direct shim read first.  On a core below 2026.10
the module skips instead of passing vacuously: 2026.8 and 2026.9 have the shims
but do not report them, and below 2026.8 (the declared minimum 2025.9.1 among
them) ``config_entries`` is a plain attrs slot rather than a property, so there
is nothing to report and no getter to inspect.  The skip is decided before any
device is created, from the registry module itself.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator.helpers.registry import (
    device_belongs_to_entry,
    device_owning_entry_ids,
    extract_subentry_links,
)
from tests.helpers import deprecation_recorder as recorder_helpers

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError:  # pragma: no cover - environment guard
    pytest.fail(
        "pytest-homeassistant-custom-component must be installed to run the "
        "DeviceEntry shim read test.",
        pytrace=False,
    )

pytest_plugins = ("pytest_homeassistant_custom_component",)

# Explicit marker required by tests/test_guard_async_test_marker.py.
pytestmark = pytest.mark.asyncio

_SHIM_NEEDLES = (
    "DeviceEntry.config_entries",
    "DeviceEntry.primary_config_entry",
)


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """Only the real device registry reports; the conftest stubs do not."""


def _shim_reports(recorder: recorder_helpers.DeprecationRecorder) -> list[Any]:
    """Reports about any of the three ownership shims.

    ``DeviceEntry.config_entries`` is a prefix of the subentries shim's name, so
    one needle covers both.
    """
    return [
        report
        for report in recorder.reports
        if any(needle in report.what for needle in _SHIM_NEEDLES)
    ]


def _prepare(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    recorder: recorder_helpers.DeprecationRecorder,
    *,
    subentry: bool,
) -> tuple[Any, str, str | None]:
    """Create a device, bind the recorder into its module, prove it listens.

    Returns ``(device, entry_id, subentry_id)``.  Skips on a core whose device
    entries do not report their shims.
    """
    from homeassistant.helpers import device_registry as dr

    if not hasattr(dr, "_report_deprecated_config_entries_property"):
        pytest.skip(
            "this Home Assistant version does not report the DeviceEntry "
            "ownership shims (reporting added in 2026.10); the check would be "
            "vacuously green"
        )

    from homeassistant.config_entries import ConfigSubentryData

    entry = MockConfigEntry(
        domain="googlefindmy",
        subentries_data=[
            ConfigSubentryData(
                data={}, subentry_type="test", title="tracker", unique_id="tracker"
            )
        ],
    )
    entry.add_to_hass(hass)
    (subentry_id,) = tuple(entry.subentries)
    target_subentry = subentry_id if subentry else None

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=target_subentry,
        identifiers={("googlefindmy", f"shim-probe-{subentry}")},
        name="shim probe",
    )

    # The module check above can read a different module object than the one
    # the live class came from (see ``deprecation_recorder.bind_into``); the
    # shim must be a property on the class that is actually in use.
    shim = inspect.getattr_static(type(device), "config_entries")
    assert isinstance(shim, property), "DeviceEntry.config_entries is no property"

    label = recorder_helpers.bind_into(monkeypatch, recorder, type(device))
    assert label is not None, "the recorder could not be bound into DeviceEntry"

    recorder.clear()
    recorder_helpers.call_from_integration_frame(lambda: device.config_entries)
    assert _shim_reports(recorder), "the canary shim read was not recorded"
    recorder.clear()

    return device, entry.entry_id, target_subentry


@pytest.mark.parametrize("subentry", [False, True], ids=["entry_root", "subentry"])
async def test_shared_accessors_read_no_shim(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
    subentry: bool,
) -> None:
    """All three accessors answer correctly and report nothing."""
    device, entry_id, subentry_id = _prepare(
        hass, monkeypatch, device_registry_deprecations, subentry=subentry
    )

    links = recorder_helpers.call_from_integration_frame(
        extract_subentry_links, device, entry_id
    )
    belongs = recorder_helpers.call_from_integration_frame(
        device_belongs_to_entry, device, entry_id
    )
    owners = recorder_helpers.call_from_integration_frame(
        device_owning_entry_ids, device
    )
    foreign = recorder_helpers.call_from_integration_frame(
        extract_subentry_links, device, "some-other-entry"
    )

    assert links == {subentry_id}
    assert belongs is True
    assert owners == (entry_id,)
    assert foreign == set()
    assert not _shim_reports(device_registry_deprecations), (
        "a shared ownership accessor read a DeviceEntry compatibility shim: "
        + "; ".join(
            report.what for report in _shim_reports(device_registry_deprecations)
        )
    )
