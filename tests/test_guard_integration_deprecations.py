# tests/test_guard_integration_deprecations.py
"""Guard the register of integration-owned deprecations (``AGENTS.md`` section 8).

The register in ``AGENTS.md`` ("Integration-owned deprecations") turns an
announced removal into a contract duty; this guard turns the duty into a red
test that needs nobody to remember it. CI installs Home Assistant from
``poetry.lock``, which Dependabot keeps raising, so the PR that lifts the lock
to a version at or past the removal version, and every PR after it, fails
until the surface is removed or the constant is extended with a reason.

Four arms for the entry ``TRACKER_ATTRS_BREAKS_IN_HA_VERSION``:

(a) due: once the installed Home Assistant reaches the constant, the set-up
    main tracker must no longer publish ``location_age``, ``location_status``
    or ``coarse_latitude`` (behaviour, measured on the state machine, not on
    the source: a dead exclusion elsewhere could keep a text probe green);
(b) the register row names exactly the value of the constant;
(c) the repair issue takes ``breaks_in_ha_version`` from the constant, so
    issue, register and guard share one source;
(d) cleanup: once the tracker no longer publishes any of the keys, the
    constant and its row must go too.

The removal PR deletes the constant, the register row and arms (a), (b) and
(d) of this entry together; the guard file stays for later entries. The
decision rule itself is the pure ``_removal_due``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.googlefindmy.const import TRACKER_ATTRS_BREAKS_IN_HA_VERSION
from tests.test_device_tracker_tick_writes import (
    _coarse,
    _now,
    _row,
    _tracker,
    _wire_entity_write_path,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HELD_KEYS = ("location_age", "location_status", "coarse_latitude")
_DUE = (
    "Integration-owned deprecation due: remove the held device_tracker attributes "
    "(location_age, location_status, coarse_*), the repair issue "
    "tracker_attributes_frozen, TRACKER_ATTRS_BREAKS_IN_HA_VERSION and its register "
    "row in one PR, or extend the constant (AGENTS.md, Integration-owned "
    "deprecations)"
)


def _removal_due(installed: str, breaks: str) -> bool:
    """Return whether ``installed`` has reached the removal version ``breaks``."""

    from awesomeversion import AwesomeVersion

    return bool(AwesomeVersion(installed) >= AwesomeVersion(breaks))


@pytest.mark.parametrize(
    ("installed", "breaks", "due"),
    [
        ("2027.5.0", "2027.5.0", True),
        ("2027.5.1", "2027.5.0", True),
        ("2027.4.3", "2027.5.0", False),
        ("2027.5.0b0", "2027.5.0", False),
    ],
)
def test_removal_due(installed: str, breaks: str, due: bool) -> None:
    """A beta of the removal version is not yet due; the release is."""

    assert _removal_due(installed, breaks) is due


async def _published_held_keys(hass: HomeAssistant) -> set[str]:
    """Set up the main tracker on the state machine; return the held keys it shows."""

    entity = _tracker(_row(_now() - 30))
    entity.coordinator.coarse = _coarse(_now() - 10)
    entity.coordinator.config_entry.options["show_location_age"] = True
    _wire_entity_write_path(entity, hass)
    entity._handle_coordinator_update()
    await hass.async_block_till_done()
    state = hass.states.get(entity.entity_id)
    assert state is not None
    return {key for key in _HELD_KEYS if key in state.attributes}


@pytest.mark.asyncio
async def test_a_due_removal_is_done_or_extended(
    hass: HomeAssistant, use_real_homeassistant_modules: None
) -> None:
    """Arm (a): at or past the removal version the keys must be gone."""

    from homeassistant.const import __version__

    held = await _published_held_keys(hass)
    assert held, "the probe saw none of the keys; arm (d) decides what that means"
    if _removal_due(__version__, TRACKER_ATTRS_BREAKS_IN_HA_VERSION):
        pytest.fail(f"{_DUE}; still published: {sorted(held)}")


@pytest.mark.asyncio
async def test_a_removed_surface_takes_its_constant_and_row_along(
    hass: HomeAssistant, use_real_homeassistant_modules: None
) -> None:
    """Arm (d): no held key published, but the constant still stands."""

    if not await _published_held_keys(hass):
        pytest.fail(
            "The held tracker attributes are gone; delete "
            "TRACKER_ATTRS_BREAKS_IN_HA_VERSION, its register row in AGENTS.md "
            "and this entry's arms of tests/test_guard_integration_deprecations.py"
        )


def test_register_row_names_the_constant_value() -> None:
    """Arm (b): exactly one register row, with the value in ``const.py``."""

    text = (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    rows = re.findall(r'`TRACKER_ATTRS_BREAKS_IN_HA_VERSION = "([^"]+)"`', text)
    assert rows == [TRACKER_ATTRS_BREAKS_IN_HA_VERSION]


def test_repair_issue_takes_the_version_from_the_constant() -> None:
    """Arm (c): one source for issue, register and guard."""

    source = (
        _REPO_ROOT / "custom_components/googlefindmy/device_tracker.py"
    ).read_text(encoding="utf-8")
    passed: list[Any] = re.findall(r"breaks_in_ha_version\s*=\s*([^,\n)]+)", source)
    assert passed == ["TRACKER_ATTRS_BREAKS_IN_HA_VERSION"]
