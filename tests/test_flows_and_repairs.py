# tests/test_flows_and_repairs.py
"""Verify config flows, diagnostics, system health, and repair hooks."""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_SUPPORT_MODULES = {
    "config_flow.py",
    "diagnostics.py",
    "system_health.py",
}


def test_support_modules_exist(integration_root: Path) -> None:
    """Ensure the integration ships the expected support modules."""

    present = {path.name for path in integration_root.iterdir() if path.is_file()}
    missing = sorted(REQUIRED_SUPPORT_MODULES - present)
    assert not missing, f"missing modules: {missing}"


def test_config_flow_uses_modern_patterns(integration_root: Path) -> None:
    """config_flow.py must rely on async helpers and duplicate-entry guards."""

    config_flow = (integration_root / "config_flow.py").read_text(encoding="utf-8")
    assert "async_get_clientsession" in config_flow
    assert "_abort_if_unique_id_configured" in config_flow
    assert (
        "entry.runtime_data" in config_flow or "multiple config entries" in config_flow
    )


def test_diagnostics_exports_entry_handler(integration_root: Path) -> None:
    """diagnostics.py must expose the config entry diagnostics coroutine."""

    diagnostics = (integration_root / "diagnostics.py").read_text(encoding="utf-8")
    assert "async_get_config_entry_diagnostics" in diagnostics
    assert "TO_REDACT" in diagnostics


def test_system_health_registers_domain(integration_root: Path) -> None:
    """system_health.py must provide an async_register entry point."""

    system_health = (integration_root / "system_health.py").read_text(encoding="utf-8")
    assert "async_register_info" in system_health
    assert "async_get_system_health_info" in system_health


def test_repairs_hooks_and_translations(integration_root: Path) -> None:
    """Repair issue helpers must align with translations and issue registry usage."""

    init_text = (integration_root / "__init__.py").read_text(encoding="utf-8")
    assert "issue_registry" in init_text
    assert "ir.async_create_issue" in init_text
    assert "duplicate_account_entries" in init_text

    translations = json.loads(
        (integration_root / "translations" / "en.json").read_text(encoding="utf-8")
    )
    issues = translations.get("issues", {})
    assert "duplicate_account_entries" in issues
    assert "multiple_config_entries" in issues
