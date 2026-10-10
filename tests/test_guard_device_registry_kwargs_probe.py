# tests/test_guard_device_registry_kwargs_probe.py
"""Red probe for the device registry ratchet: every rule must be able to fail.

``tests/test_guard_device_registry_kwargs.py`` is green today, and a green guard
tells you nothing about whether it can ever go red.  This module runs the same
scanner against deliberately offending files under
``tests/fixtures/registry_guard_probe/`` and requires each rule to fire.

Doing this as a test rather than as a one-off hand check is the point: a hand
check documented in a commit body stops being true the moment somebody edits the
scanner, and nobody would notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_guard_device_registry_kwargs import scan_file

_PROBE_ROOT = Path(__file__).resolve().parent / "fixtures" / "registry_guard_probe"

#: ``(file, rule that must fire)``.  Rule 3 has two entries because attribute and
#: ``getattr`` form are structurally different nodes; the plan's finding KB-2 was
#: exactly a rule that covered the first and missed the second.
PROBES: tuple[tuple[str, str], ...] = (
    ("rule_kwargs.py", "kwargs"),
    ("rule_kwargs_string.py", "kwargs_string"),
    ("rule_config_entries.py", "config_entries"),
    ("rule_config_entries_getattr.py", "config_entries"),
    ("rule_devices.py", "devices"),
    ("rule_devices_getattr.py", "devices"),
    ("rule_async_get_device.py", "async_get_device"),
)


@pytest.mark.parametrize(("filename", "rule"), PROBES)
def test_each_rule_fires_on_its_probe(filename: str, rule: str) -> None:
    """The scanner reports the intended rule for the intended probe file."""
    path = _PROBE_ROOT / filename
    assert path.exists(), f"probe file {filename} is missing"

    findings, _ = scan_file(path, f"probe/{filename}")
    fired = {finding.rule for finding in findings}

    assert rule in fired, (
        f"{filename} must trigger rule {rule!r}, but the scanner only reported "
        f"{sorted(fired) or 'nothing'}. The rule cannot fail, so it protects nothing."
    )


def test_getattr_form_is_covered_by_the_attribute_rule() -> None:
    """Both shapes of rule 3 are found, not just the attribute one.

    Pinned separately because this is the failure the migration plan measured:
    eleven production sites used ``getattr(device, "config_entries", ...)`` and
    were invisible to a rule that only inspected ``ast.Attribute``.
    """
    attribute_findings, _ = scan_file(
        _PROBE_ROOT / "rule_config_entries.py", "probe/attribute.py"
    )
    getattr_findings, _ = scan_file(
        _PROBE_ROOT / "rule_config_entries_getattr.py", "probe/getattr.py"
    )

    assert any(item.rule == "config_entries" for item in attribute_findings)
    assert any(item.rule == "config_entries" for item in getattr_findings)
    assert any("getattr" in item.detail for item in getattr_findings), (
        "the getattr form must be reported as such, so the failure text says "
        "which shape to fix"
    )


def test_the_translator_is_exempt_but_only_it() -> None:
    """Exactly one path is allowed to keep speaking the old dialect.

    Without this the exemption could be widened by a typo in the constant and
    nobody would see it: the guard would simply stop reporting a whole file.
    """
    probe = _PROBE_ROOT / "rule_kwargs.py"

    exempt, _ = scan_file(probe, "coordinator/helpers/registry.py")
    assert not exempt, "the translator path must be exempt from rule 1"

    near_miss, _ = scan_file(probe, "coordinator/helpers/registry_new.py")
    assert near_miss, "only the exact translator path may be exempt"
