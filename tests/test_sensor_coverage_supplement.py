# tests/test_sensor_coverage_supplement.py
"""Coverage supplement for sensor.py branches introduced by the last_seen fix.

Primary file for AP4 (per-file >95% coverage of ``sensor.py``). This module
closes the branch matrix of the AP1-modified
``GoogleFindMyLastSeenSensor.extra_state_attributes`` property that the bugfix
regression test (``test_last_seen_sensor_no_map_attributes.py``) does not yet
exercise, in particular the *position-less row* path where the coordinate strip
is a no-op but the diagnostic attributes must still survive.

The exhaustive ``--cov-report=term-missing``-driven gap closure across the other
``sensor.py`` entity classes runs in the PR/CI environment (Python 3.13, real
Home Assistant); see AP4 DoD-1/DoD-2. The planning container cannot execute the
HA test suite, so this file focuses on the changed-code branches that are most
load-bearing for not regressing the file's coverage (risk F6).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.test_last_seen_sensor_no_map_attributes import _build_last_seen_sensor

# A decoder row with diagnostics but no resolvable position. ``_as_ha_attributes``
# omits latitude/longitude (both None), so the consumer's ``pop`` is a no-op while
# the diagnostic keys remain.
_POSITIONLESS_ROW: dict[str, Any] = {
    "id": "dev-1",
    "device_id": "dev-1",
    "name": "Pixel",
    "accuracy": 25.0,
    "last_seen": 1_700_000_000,
    "source_label": "Network",
    "source_rank": 2,
}


def test_last_seen_sensor_positionless_row_keeps_diagnostics(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A row without coordinates yields diagnostics and never adds lat/lon.

    Exercises the branch where the helper emits no coordinate keys: the strip is a
    no-op, the result is non-empty, and the TIMESTAMP sensor still surfaces its
    diagnostic attributes.
    """

    entity = _build_last_seen_sensor(
        _POSITIONLESS_ROW, deterministic_config_subentry_id
    )
    attrs = entity.extra_state_attributes

    assert attrs is not None
    assert "latitude" not in attrs
    assert "longitude" not in attrs
    assert "accuracy_m" in attrs
    assert "last_seen" in attrs
