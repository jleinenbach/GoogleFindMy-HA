# tests/test_deprecation_recorder_wiring.py
"""Prove that the deprecation recorder is actually wired into real Core code.

Every gate built on the recorder reads its output as "no deprecation was
raised".  That reading is only valid if the recorder is in the call chain at
all: a recorder that is never invoked returns an empty list, which is
indistinguishable from a clean run.  These tests are that proof.

The failure mode they guard against is concrete and was measured: Core's
``device_registry`` binds ``report_usage`` by value at import time, and
``use_real_homeassistant_modules`` re-imports the whole ``homeassistant``
package.  Either fact alone is enough to detach a naively installed recorder.
"""

from __future__ import annotations

import pytest

from tests.helpers import deprecation_recorder as recorder_helpers


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """Run these tests against the real Home Assistant implementation."""


def test_recorder_is_bound_into_device_registry(
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """The recorder must reach the module that copies ``report_usage``.

    If Core ever changes how ``device_registry`` imports the symbol, this test
    fails loudly instead of letting every deprecation gate go quietly green.
    """
    assert (
        "homeassistant.helpers.device_registry"
        in device_registry_deprecations.bound_modules
    )


def test_deprecated_registry_call_is_recorded(
    hass: object,
    monkeypatch: pytest.MonkeyPatch,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """A deprecated Core call from an integration frame reaches the recorder."""
    from homeassistant.helpers import device_registry as dr

    if not recorder_helpers.reporter_available(dr.async_get(hass)):
        pytest.skip(
            "this Core version raises no report_usage for the device registry "
            "APIs (behaviour changed in 2026.8, reporting added in 2026.9)"
        )

    registry = dr.async_get(hass)
    bound = recorder_helpers.bind_into(
        monkeypatch, device_registry_deprecations, type(registry)
    )
    assert bound is not None, "the live registry class does not use report_usage"

    device_registry_deprecations.clear()
    result = recorder_helpers.call_from_integration_frame(
        registry.async_get_device, {("googlefindmy", "recorder-wiring-probe")}
    )

    assert result is None  # the probe identifier is not registered
    recorded = device_registry_deprecations.matching("async_get_device")
    assert len(recorded) == 1, (
        "expected exactly one report_usage call for async_get_device, got "
        f"{[report.what for report in device_registry_deprecations.reports]}"
    )
    assert recorded[0].from_integration, (
        "the call was not attributed to this integration; the synthetic frame "
        f"in {recorder_helpers.PROBE_FILENAME} did not take effect"
    )


def test_call_outside_integration_still_raises(
    hass: object,
    monkeypatch: pytest.MonkeyPatch,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """The recorder observes without changing behaviour.

    Core turns a deprecated call made outside ``custom_components/`` into a
    ``RuntimeError``.  Delegating rather than replacing keeps that true: a
    recorder that swallowed the exception would silence a real defect.  Two
    tests in this repository failed exactly that way until AP-09 migrated them
    to the scoped lookup, which is why the property is pinned here instead of
    being left to chance.
    """
    from homeassistant.helpers import device_registry as dr

    if not recorder_helpers.reporter_available(dr.async_get(hass)):
        pytest.skip(
            "no report_usage for the device registry APIs on this Core version, "
            "so there is nothing that could raise"
        )

    registry = dr.async_get(hass)
    recorder_helpers.bind_into(
        monkeypatch, device_registry_deprecations, type(registry)
    )

    device_registry_deprecations.clear()
    with pytest.raises(RuntimeError):
        registry.async_get_device({("googlefindmy", "recorder-wiring-probe")})

    assert device_registry_deprecations.matching("async_get_device")


def test_module_level_binding_alone_is_not_enough(
    hass: object,
    device_registry_deprecations: recorder_helpers.DeprecationRecorder,
) -> None:
    """Pin the reason ``bind_into`` exists.

    The autouse fixture already patched ``sys.modules`` including
    ``homeassistant.helpers.device_registry``.  If that were sufficient, the
    call below would be recorded without any extra binding.  It is not: the
    live registry class carries the globals of an earlier module object.  Should
    a future Core or plugin version remove that duplication, this test turns red
    and ``bind_into`` can be dropped -- it does not silently become dead code.
    """
    from homeassistant.helpers import device_registry as dr

    assert (
        "homeassistant.helpers.device_registry"
        in device_registry_deprecations.bound_modules
    )
    if not recorder_helpers.reporter_available(dr.async_get(hass)):
        pytest.skip(
            "without a reporter both bindings are silent, so the comparison "
            "between them carries no information"
        )

    registry = dr.async_get(hass)

    device_registry_deprecations.clear()
    recorder_helpers.call_from_integration_frame(
        registry.async_get_device, {("googlefindmy", "module-binding-probe")}
    )

    assert not device_registry_deprecations.matching("async_get_device"), (
        "the module-level patch now reaches the live registry; bind_into and "
        "this test are obsolete"
    )
