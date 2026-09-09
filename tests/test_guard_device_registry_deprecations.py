# tests/test_guard_device_registry_deprecations.py
"""Gate: no device registry deprecation may reach Core unnoticed.

This runs a **counted** set of registry operations against real Home Assistant
and fails as soon as one of them raises a ``report_usage`` this repository has
not explicitly accepted.  The counted set is the point: the gate reports "no
unexpected deprecation", and that sentence is only worth as much as the set of
operations behind it.

The set mirrors what production reaches on a core that reports, which is
2026.9 and newer.  Twelve operations:

* one for call pattern A (move) and one for pattern B (ensure ownership),
* three for pattern C (detach), all three kept as reporter evidence since
  AP-13; the armed and the unarmed form take different Core branches and are
  therefore listed separately,
* six for the places that reach ``async_get_device``, none of which is a
  reachable call site any more since AP-16, and
* one for the deprecated ``devices`` mapping.

The counts of *reachable* sites shrink with every work package while the count
of *operations* stays at twelve; see the paragraph below on why the migrated
ones stay.

The production sites are named by enclosing function rather than by line
number: line numbers drift with every unrelated edit, and a stale reference is
worse than none.

Eleven of the twelve carry no production site any more: AP-12 moved every
ownership and lookup call in ``coordinator/registry.py`` onto intents, AP-13 did
the same for ``services.py``, AP-15 for ``config_flow.py`` and AP-16 for
``__init__.py``. The twelfth, the ``devices`` mapping, still has three sites
(``coordinator/registry.py``, ``coordinator/subentry.py``, ``diagnostics.py``). Their operations
stay, and that is deliberate. They do not prove that the fork still
makes the call; they prove that *Core still reports it*, which is what keeps the
canary and the dead-entry check honest and what will catch a regression that
brings the old form back. They are marked ``migrated in AP-12``, ``migrated in AP-13``, ``migrated in
AP-15`` or ``migrated in AP-16`` in place of a site.

**A caveat about what the dead-entry check can and cannot see.** It asks whether
one of the twelve *test operations* still triggers the report, not whether
production still makes the call. A migrated entry therefore stays green by
construction, which is the point -- but it also means the ``reason`` text is the
only thing telling a reader where the fork stands, and nothing checks it. Read
the ratchet in ``tests/test_guard_device_registry_kwargs.py`` for the current
number of production sites; it is measured, this text is written.

Two structural safeguards keep a green run from being vacuous:

* a **canary** performs a known-deprecated call and requires the recorder to see
  it -- if the wiring from ``tests/helpers/deprecation_recorder.py`` is ever
  lost, the canary fails while the gate itself would go quietly green, and
* the allowlist is checked for **dead entries**, so it cannot silently grow into
  a list of things that stopped happening years ago.

Home Assistant 2026.8 changed the *behaviour* but shipped no ``report_usage``
for these APIs; the reports only appear in 2026.9.  On a version without the
reporter every operation below is silent, so the gate skips instead of claiming
a clean bill of health it cannot support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.helpers import deprecation_recorder as recorder_helpers

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError:  # pragma: no cover - environment guard
    pytest.fail(
        "pytest-homeassistant-custom-component must be installed to run the "
        "device registry deprecation gate.",
        pytrace=False,
    )

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """The gate is only meaningful against the real device registry."""


@dataclass(frozen=True)
class AcceptedDeprecation:
    """One deprecation this repository knowingly still triggers."""

    key: str
    #: Substring identifying the report; matched against ``report.what``.
    needle: str
    reason: str
    #: The work package after which this entry must disappear.
    resolved_by: str


#: Every deprecation the integration may still raise, each with a reason and an
#: expiry.  An entry that no longer matches anything is a dead entry and fails
#: ``test_allowlist_has_no_dead_entries``.
ACCEPTED_DEPRECATIONS: tuple[AcceptedDeprecation, ...] = (
    AcceptedDeprecation(
        key="ownership_kwargs",
        needle="add_config_entry_id",
        reason=(
            "No production site names these kwargs any more (measured: the "
            "ratchet in tests/test_guard_device_registry_kwargs.py holds zero "
            "kwargs findings). What keeps this entry alive is the gate's own "
            "operation _pattern_a, not production: the gate only runs from core "
            "2026.9 on, and there the planner emits the new_* pair, so the "
            "legacy translator never speaks these names under a reporter. See "
            "the module docstring on what the dead-entry check can see."
        ),
        resolved_by="AP-17",
    ),
    AcceptedDeprecation(
        key="async_get_device",
        needle="`device_registry.async_get_device`",
        reason=(
            "No production site calls the deprecated lookup any more. The "
            "shared resolver landed in AP-14 with the identity.py site, AP-12 "
            "moved the two coordinator/registry.py sites onto it, AP-13 the two "
            "in services.py, AP-15 the one in config_flow.py and AP-16 the last "
            "one in __init__.py. The resolver's own legacy branch is not a site "
            "either: it only runs below core 2026.8, which does not report at "
            "all. Kept as reporter evidence, see the module docstring."
        ),
        resolved_by="AP-17",
    ),
    AcceptedDeprecation(
        key="devices_mapping",
        needle="`device_registry.devices`",
        reason=(
            "Three `devices` sites remain, in coordinator/registry.py, "
            "coordinator/subentry.py and diagnostics.py; AP-16 removed the ones "
            "in __init__.py. They are migrated last."
        ),
        resolved_by="AP-17",
    ),
)


def _entries(hass: Any) -> tuple[Any, Any, str, str]:
    """Create two config entries, the first with one subentry."""
    from homeassistant.config_entries import ConfigSubentryData

    owner = MockConfigEntry(
        domain="googlefindmy",
        subentries_data=[
            ConfigSubentryData(
                data={}, subentry_type="test", title="tracker", unique_id="tracker"
            )
        ],
    )
    owner.add_to_hass(hass)
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    (subentry_id,) = tuple(owner.subentries)
    return owner, other, owner.entry_id, subentry_id


def _operations(hass: Any) -> list[tuple[str, str, Any]]:
    """Return ``(id, production site, callable)`` for every covered operation.

    Twelve entries, composed exactly as the module docstring lists them: one
    for pattern A, one for pattern B, three for pattern C, six ``async_get_device``
    reach points and one ``devices`` mapping site.  Several share an underlying
    Core API on purpose -- the list enumerates *production sites*, so that
    removing one site visibly shrinks it.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    owner, other, entry_id, subentry_id = _entries(hass)

    def _device(tag: str) -> Any:
        return registry.async_get_or_create(
            config_entry_id=entry_id,
            config_subentry_id=subentry_id,
            identifiers={("googlefindmy", f"gate-{tag}")},
            name=f"gate probe {tag}",
        )

    def _pattern_a() -> None:
        device = _device("a")
        registry.async_update_device(device.id, add_config_entry_id=other.entry_id)
        registry.async_update_device(
            device.id,
            remove_config_entry_id=entry_id,
            remove_config_subentry_id=subentry_id,
        )

    def _pattern_b() -> None:
        device = _device("b")
        registry.async_update_device(device.id, add_config_entry_id=entry_id)

    def _pattern_c(tag: str) -> Any:
        def _run() -> None:
            device = _device(tag)
            registry.async_update_device(
                device.id,
                remove_config_entry_id=entry_id,
                remove_config_subentry_id=None,
            )

        return _run

    def _get_device(tag: str) -> Any:
        def _run() -> None:
            registry.async_get_device({("googlefindmy", f"gate-lookup-{tag}")})

        return _run

    def _devices_mapping() -> None:
        list(registry.devices.values())

    return [
        (
            "pattern_a_move",
            "migrated in AP-12; kept as reporter evidence",
            _pattern_a,
        ),
        (
            "pattern_b_ensure",
            "migrated in AP-12; kept as reporter evidence",
            _pattern_b,
        ),
        (
            "pattern_c_unarmed_removal",
            "migrated in AP-12; kept as reporter evidence",
            _pattern_c("c1"),
        ),
        (
            "pattern_c_armed_removal",
            "migrated in AP-12; kept as reporter evidence",
            _pattern_c("c2"),
        ),
        (
            "pattern_c_services_125",
            "migrated in AP-13; kept as reporter evidence",
            _pattern_c("c3"),
        ),
        (
            "get_device_services_354",
            "migrated in AP-13; kept as reporter evidence",
            _get_device("s354"),
        ),
        (
            "get_device_services_377",
            "migrated in AP-13; kept as reporter evidence",
            _get_device("s377"),
        ),
        (
            "get_device_migrated_config_flow",
            "migrated in AP-15; kept as reporter evidence",
            _get_device("cf6976"),
        ),
        (
            "get_device_migrated_service",
            "migrated in AP-12; kept as reporter evidence",
            _get_device("r689"),
        ),
        (
            "get_device_migrated_hub",
            "migrated in AP-12; kept as reporter evidence",
            _get_device("r1424"),
        ),
        (
            "get_device_init_4549",
            "migrated in AP-16; kept as reporter evidence",
            _get_device("i4549"),
        ),
        (
            "devices_mapping",
            "coordinator/registry.py, coordinator/subentry.py and "
            "diagnostics.py, three sites",
            _devices_mapping,
        ),
    ]


def _prepare(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    recorder: recorder_helpers.DeprecationRecorder,
) -> Any:
    """Skip if this Core version is silent, otherwise bind and return the registry.

    Binding has to happen against ``type(registry)``: under
    ``use_real_homeassistant_modules`` the live registry class carries the
    globals of an earlier module object, so the autouse fixture's patch of
    ``sys.modules`` does not reach it.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    if not recorder_helpers.reporter_available(registry):
        pytest.skip(
            "this Home Assistant version raises no report_usage for the device "
            "registry APIs (behaviour changed in 2026.8, reporting added in "
            "2026.9); the gate would be vacuously green"
        )
    recorder_helpers.bind_into(monkeypatch, recorder, type(registry))
    return registry


def test_canary_proves_the_recorder_is_listening(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """A known-deprecated call must be observed, or this whole module is void."""
    assert (
        "homeassistant.helpers.device_registry"
        in device_registry_deprecations.bound_modules
    ), "the recorder is not bound into the device registry module"

    registry = _prepare(hass, monkeypatch, device_registry_deprecations)

    device_registry_deprecations.clear()
    recorder_helpers.call_from_integration_frame(
        registry.async_get_device, {("googlefindmy", "canary")}
    )

    assert device_registry_deprecations.matching("async_get_device"), (
        "the canary call was not recorded"
    )


def test_no_unexpected_deprecation_is_raised(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """Run every covered operation and reject anything not on the allowlist."""
    _prepare(hass, monkeypatch, device_registry_deprecations)

    operations = _operations(hass)
    assert len(operations) == 12, (
        "the covered set changed; update the count and the module docstring so "
        "the gate's scope stays stated rather than assumed"
    )

    unexpected: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for name, site, run in operations:
        device_registry_deprecations.clear()
        recorder_helpers.call_from_integration_frame(run)
        for report in device_registry_deprecations.reports:
            accepted = next(
                (
                    entry
                    for entry in ACCEPTED_DEPRECATIONS
                    if entry.needle in report.what
                ),
                None,
            )
            if accepted is None:
                unexpected.append((name, site, report.what))
            else:
                seen.add(accepted.key)

    assert not unexpected, "unexpected device registry deprecations:\n" + "\n".join(
        f"  {name} ({site}): {what}" for name, site, what in unexpected
    )

    print("\nCurrently accepted device registry deprecations:")
    for entry in ACCEPTED_DEPRECATIONS:
        status = "observed" if entry.key in seen else "not observed"
        print(f"  {entry.key} [{status}] until {entry.resolved_by}: {entry.reason}")


def test_allowlist_has_no_dead_entries(
    hass: Any,
    monkeypatch: pytest.MonkeyPatch,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """Every allowlist entry must still be triggered by a covered operation.

    Without this an entry outlives the code that caused it, and the allowlist
    stops describing the repository.  It is the mirror image of the gate: the
    gate catches deprecations nobody accepted, this catches acceptances nobody
    needs.
    """
    _prepare(hass, monkeypatch, device_registry_deprecations)

    seen: set[str] = set()
    for _name, _site, run in _operations(hass):
        device_registry_deprecations.clear()
        recorder_helpers.call_from_integration_frame(run)
        for report in device_registry_deprecations.reports:
            for entry in ACCEPTED_DEPRECATIONS:
                if entry.needle in report.what:
                    seen.add(entry.key)

    dead = [entry.key for entry in ACCEPTED_DEPRECATIONS if entry.key not in seen]
    assert not dead, (
        f"allowlist entries no longer triggered by any covered operation: {dead}. "
        "Remove them, or extend the covered set if the operation was dropped."
    )
