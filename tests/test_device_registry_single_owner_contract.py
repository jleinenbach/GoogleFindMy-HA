# tests/test_device_registry_single_owner_contract.py
"""Differential test: the single-owner double against real Home Assistant.

``tests/helpers/single_owner_device_registry.py`` encodes what this repository
believes Core 2026.8+ does with device ownership.  A belief is not a fact, so
every case below runs twice -- once against the double, once against the real
``homeassistant.helpers.device_registry`` -- and the two results are compared
after **every** step, not only at the end.

Two cases carry the load for the migration plan's decision D-14:

* ``remove_config_entry_id`` on the owning entry with ``remove_config_subentry_id
  =None`` **deletes** the device when it sits at the entry root, and
* the very same call is a **no-op** when the device sits inside a subentry.

Both patterns exist verbatim in this integration today, which is why a
``DETACH`` intent must compare both levels instead of mapping straight onto
``async_remove_device``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers import deprecation_recorder as recorder_helpers
from tests.helpers import single_owner_device_registry as sod

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError:  # pragma: no cover - environment guard
    pytest.fail(
        "pytest-homeassistant-custom-component must be installed to run the "
        "single-owner differential test.",
        pytrace=False,
    )

pytest_plugins = ("pytest_homeassistant_custom_component",)

# Explicit marker required by tests/test_guard_async_test_marker.py: the
# repository runs asyncio_mode="auto", which collects unmarked async tests
# silently, so the marker is stated rather than relied upon.
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """Compare against the real device registry, not the conftest stubs."""


#: Symbolic names used in the cases; resolved per world to real identifiers.
ENTRY_A = "ENTRY_A"
ENTRY_B = "ENTRY_B"
SUB_A1 = "SUB_A1"
SUB_A2 = "SUB_A2"
SUB_B1 = "SUB_B1"
UNKNOWN = "UNKNOWN_ID"

IDENTIFIER = ("googlefindmy", "single-owner-contract")

#: ``(name, start_subentry, [step_kwargs, ...])``.  ``start_subentry`` is the
#: subentry the device is created in; ``None`` means "at the entry root".
CASES: list[tuple[str, str | None, list[dict[str, Any]]]] = [
    ("add_only_arms_a_move", SUB_A1, [{"add_config_entry_id": ENTRY_B}]),
    (
        "add_already_owner_is_noop",
        SUB_A1,
        [{"add_config_entry_id": ENTRY_A, "add_config_subentry_id": SUB_A1}],
    ),
    ("new_entry_moves_immediately", SUB_A1, [{"new_config_entry_id": ENTRY_B}]),
    ("new_subentry_moves_immediately", SUB_A1, [{"new_config_subentry_id": SUB_A2}]),
    (
        "remove_at_entry_root_deletes",
        None,
        [{"remove_config_entry_id": ENTRY_A, "remove_config_subentry_id": None}],
    ),
    (
        "remove_with_mismatched_subentry_is_noop",
        SUB_A1,
        [{"remove_config_entry_id": ENTRY_A, "remove_config_subentry_id": None}],
    ),
    (
        "remove_with_matching_subentry_deletes",
        SUB_A1,
        [{"remove_config_entry_id": ENTRY_A, "remove_config_subentry_id": SUB_A1}],
    ),
    (
        "add_then_remove_completes_the_move",
        None,
        [
            {"add_config_entry_id": ENTRY_B, "add_config_subentry_id": SUB_B1},
            {"remove_config_entry_id": ENTRY_A, "remove_config_subentry_id": None},
        ],
    ),
    (
        "remove_of_foreign_entry_is_noop",
        SUB_A1,
        [{"remove_config_entry_id": ENTRY_B}],
    ),
    (
        "new_combined_with_add_raises",
        SUB_A1,
        [{"new_config_entry_id": ENTRY_B, "add_config_entry_id": ENTRY_B}],
    ),
    (
        "new_combined_with_remove_raises",
        SUB_A1,
        [{"new_config_entry_id": ENTRY_B, "remove_config_entry_id": ENTRY_A}],
    ),
    (
        "add_subentry_without_entry_raises",
        SUB_A1,
        [{"add_config_subentry_id": SUB_A2}],
    ),
    (
        "remove_subentry_without_entry_raises",
        SUB_A1,
        [{"remove_config_subentry_id": SUB_A1}],
    ),
    ("add_unknown_entry_raises", SUB_A1, [{"add_config_entry_id": UNKNOWN}]),
    ("new_unknown_entry_raises", SUB_A1, [{"new_config_entry_id": UNKNOWN}]),
    (
        "add_foreign_subentry_raises",
        SUB_A1,
        [{"add_config_entry_id": ENTRY_B, "add_config_subentry_id": SUB_A2}],
    ),
]


def _snapshot(device: Any, symbols: dict[str, str]) -> dict[str, Any]:
    """Normalise a device entry into a world-independent comparable dict."""
    if device is None:
        return {"exists": False}

    def sym(value: Any) -> Any:
        return symbols.get(value, value)

    return {
        "exists": True,
        "config_entry_id": sym(device.config_entry_id),
        "config_subentry_id": sym(device.config_subentry_id),
        "config_entries": sorted(sym(item) for item in device.config_entries),
        "config_entries_subentries": {
            sym(entry): sorted((sym(sub) if sub is not None else None) for sub in subs)
            for entry, subs in device.config_entries_subentries.items()
        },
        "composite_device_id": device.composite_device_id,
    }


def _run_case(
    steps: list[dict[str, Any]],
    *,
    resolve: dict[str, Any],
    update: Any,
    get: Any,
    device_id: str,
    error_type: type[Exception],
) -> list[dict[str, Any]]:
    """Apply ``steps`` and snapshot the device after each one."""
    results: list[dict[str, Any]] = []
    inverse = {value: key for key, value in resolve.items() if isinstance(value, str)}
    for step in steps:
        kwargs = {key: resolve.get(value, value) for key, value in step.items()}
        try:
            update(device_id, **kwargs)
        except error_type as err:
            # The full message is compared, with every world-specific identifier
            # replaced by its symbolic name.  Core embeds generated entry ids
            # ("01M1ZY..."), the double uses readable ones; normalising instead
            # of truncating keeps the whole sentence under test.
            message = str(err)
            for real, symbol in inverse.items():
                message = message.replace(real, symbol)
            results.append({"error": message})
            continue
        except NotImplementedError:  # pragma: no cover - double gap, fail loudly
            raise
        results.append(_snapshot(get(device_id), inverse))
    return results


def _build_double() -> tuple[sod.SingleOwnerDeviceRegistry, dict[str, Any]]:
    """Return a double plus the symbol table for its identifiers."""
    registry = sod.SingleOwnerDeviceRegistry()
    registry.add_config_entry("entry-a", {"sub-a1", "sub-a2"})
    registry.add_config_entry("entry-b", {"sub-b1"})
    return registry, {
        ENTRY_A: "entry-a",
        ENTRY_B: "entry-b",
        SUB_A1: "sub-a1",
        SUB_A2: "sub-a2",
        SUB_B1: "sub-b1",
        UNKNOWN: "no-such-id",
    }


@pytest.mark.parametrize(("name", "start_subentry", "steps"), CASES, ids=lambda v: v)
async def test_double_matches_core(
    hass: Any,
    name: str,
    start_subentry: str | None,
    steps: list[dict[str, Any]],
) -> None:
    """Every case produces the same observable outcome in both worlds."""
    double, double_symbols = _build_double()
    double_device = double.add_device(
        identifiers={IDENTIFIER},
        config_entry_id="entry-a",
        config_subentry_id=(double_symbols[start_subentry] if start_subentry else None),
    )
    double_result = _run_case(
        steps,
        resolve=double_symbols,
        update=double.async_update_device,
        get=double.async_get,
        device_id=double_device.id,
        error_type=sod.SingleOwnerError,
    )

    core_result = _run_case_against_core(hass, start_subentry, steps)

    assert double_result == core_result, (
        f"case {name!r} diverges between the single-owner double and real Core"
    )


def _run_case_against_core(
    hass: Any, start_subentry: str | None, steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run the same steps against the real device registry."""
    from homeassistant.config_entries import ConfigSubentryData
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr

    def _subentries(*titles: str) -> list[ConfigSubentryData]:
        return [
            ConfigSubentryData(
                data={}, subentry_type="test", title=title, unique_id=title
            )
            for title in titles
        ]

    entry_a = MockConfigEntry(
        domain="googlefindmy", subentries_data=_subentries("a1", "a2")
    )
    entry_a.add_to_hass(hass)
    entry_b = MockConfigEntry(domain="other", subentries_data=_subentries("b1"))
    entry_b.add_to_hass(hass)

    sub_a1, sub_a2 = sorted(
        entry_a.subentries, key=lambda sid: entry_a.subentries[sid].unique_id or ""
    )
    (sub_b1,) = tuple(entry_b.subentries)

    symbols: dict[str, Any] = {
        ENTRY_A: entry_a.entry_id,
        ENTRY_B: entry_b.entry_id,
        SUB_A1: sub_a1,
        SUB_A2: sub_a2,
        SUB_B1: sub_b1,
        UNKNOWN: "no-such-id",
    }

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry_a.entry_id,
        config_subentry_id=symbols[start_subentry] if start_subentry else None,
        identifiers={IDENTIFIER},
        name="contract probe",
    )

    def _update(target_id: str, **kwargs: Any) -> Any:
        """Call Core from a frame it attributes to this integration.

        The four ownership kwargs are deprecated on 2026.9+, and Core turns a
        deprecated call made from outside ``custom_components/`` into a
        ``RuntimeError`` before doing any work.  Without the synthetic
        integration frame this test would only ever measure that guard and never
        reach the behaviour it means to compare.
        """
        return recorder_helpers.call_from_integration_frame(
            registry.async_update_device, target_id, **kwargs
        )

    # The exception class must come from the namespace the registry itself uses.
    # Measured on Core 2026.9.1: under ``use_real_homeassistant_modules`` the
    # live registry class carries the globals of an earlier module object, so
    # ``homeassistant.exceptions.HomeAssistantError`` imported here is a
    # *different* class than the one raised, and ``except`` would not catch it.
    # Same root cause as ``deprecation_recorder.bind_into``.
    live_error = registry.async_update_device.__func__.__globals__.get(
        "HomeAssistantError", HomeAssistantError
    )

    return _run_case(
        steps,
        resolve=symbols,
        update=_update,
        get=registry.async_get,
        device_id=device.id,
        error_type=live_error,
    )


async def test_detach_deletes_only_when_both_levels_match(hass: Any) -> None:
    """Pin the outcome D-14 depends on, not just the agreement of two worlds.

    ``test_double_matches_core`` proves double and Core behave alike; it cannot
    prove they behave *correctly*, because both could be wrong in the same way.
    This test states the expected outcome directly against real Core:

    * device at the entry root + ``remove_config_subentry_id=None`` -> deleted,
    * device inside a subentry + ``remove_config_subentry_id=None`` -> untouched.

    Measured before the migration: eight call sites passed
    ``remove_config_entry_id``, five of them with
    ``remove_config_subentry_id=None``, and three of those five armed a move with
    ``add_config_entry_id`` in the same call, which turns the removal into a move
    instead of a deletion. The remaining two carried the deletion risk. **Both
    are gone**: AP-12 moved ``coordinator/registry.py`` onto intents and AP-13
    ``services.py``. Re-measured after AP-13, exactly one production site outside
    the legacy translator still names the keyword, ``__init__.py`` in the
    subentry removal path, and it passes a real subentry id. Take the number from
    the ratchet in ``tests/test_guard_device_registry_kwargs.py``, which is
    measured on every run, rather than from this paragraph. The other
    three sites pass a real subentry id and are unaffected.
    """
    core = _run_case_against_core(
        hass,
        None,
        [{"remove_config_entry_id": ENTRY_A, "remove_config_subentry_id": None}],
    )
    assert core == [{"exists": False}], (
        "a device owned at the entry root must be deleted by pattern C"
    )

    kept = _run_case_against_core(
        hass,
        SUB_A1,
        [{"remove_config_entry_id": ENTRY_A, "remove_config_subentry_id": None}],
    )
    assert kept[0]["exists"] is True, (
        "a device inside a subentry must survive pattern C with a mismatched subentry"
    )
    assert kept[0]["config_entry_id"] == ENTRY_A
    assert kept[0]["config_subentry_id"] == SUB_A1


async def test_shims_report_exactly_one_owner(hass: Any) -> None:
    """The compatibility shims collapse to a single entry on 2026.8+.

    Eight production sites read ``DeviceEntry.config_entries`` as an attribute
    and twelve more through ``getattr``; any of them that evaluates ``len(...)``
    or a set difference has silently lost its predicate.  Both numbers are the
    per-rule totals of ``KNOWN_VIOLATIONS`` in
    ``tests/test_guard_device_registry_kwargs.py``.
    """
    snapshot = _run_case_against_core(
        hass, SUB_A1, [{"new_config_subentry_id": SUB_A2}]
    )
    assert snapshot[0]["config_entries"] == [ENTRY_A]
    assert snapshot[0]["config_entries_subentries"] == {ENTRY_A: [SUB_A2]}


async def test_scoped_lookup_ignores_a_device_owned_by_another_entry(
    hass: Any,
) -> None:
    """The scoped lookup is narrower than ``async_get_device``, on purpose.

    ``async_get_device`` searched by identifier alone and would return a device
    that carries the identifier under *any* config entry.
    ``async_get_device_by_identifier`` is scoped to one entry and returns
    ``None`` instead.

    That narrowing is wanted, but it is not free: ``__init__.py``'s relink
    migration path can legitimately meet a device that still carries an
    unscoped identifier under an older entry.  Pinning the behaviour here, away
    from the production code, keeps AP-14 and AP-16 from quietly changing it
    back or quietly relying on the old breadth.
    """
    from homeassistant.helpers import device_registry as dr

    owner = MockConfigEntry(domain="googlefindmy")
    owner.add_to_hass(hass)
    stranger = MockConfigEntry(domain="other")
    stranger.add_to_hass(hass)

    registry = dr.async_get(hass)
    unscoped = ("googlefindmy", "legacy-unscoped-identifier")
    registry.async_get_or_create(
        config_entry_id=stranger.entry_id,
        identifiers={unscoped},
        name="device left behind by an older entry",
    )

    assert registry.async_get_device_by_identifier(unscoped, owner.entry_id) is None
    assert (
        registry.async_get_device_by_identifier(unscoped, stranger.entry_id) is not None
    )
