# tests/test_google_home_filter.py

from __future__ import annotations

import logging
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import (
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
)


def _ensure_core_state() -> None:
    core_module = sys.modules.get("homeassistant.core")
    if core_module is None:
        core_module = ModuleType("homeassistant.core")
        sys.modules["homeassistant.core"] = core_module
    if not hasattr(core_module, "State"):
        class _State(SimpleNamespace):
            def __init__(self, entity_id: str, state: str, attributes: dict[str, Any]):
                super().__init__(entity_id=entity_id, state=state, attributes=attributes)

        core_module.State = _State  # type: ignore[attr-defined]
    if not hasattr(core_module, "callback"):
        core_module.callback = lambda func: func  # type: ignore[attr-defined]


class _StatesStub:
    """Lightweight state manager mimicking Home Assistant's interface."""

    def __init__(self, mapping: dict[str, SimpleNamespace]) -> None:
        self._mapping = mapping

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self._mapping.get(entity_id)

    def async_all(self, domain: str | None = None) -> list[SimpleNamespace]:
        if domain is None:
            return list(self._mapping.values())
        prefix = f"{domain}."
        return [state for key, state in self._mapping.items() if key.startswith(prefix)]


class _EntityRegistryStub:
    """Minimal entity-registry stub returning configured IDs."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def async_get_entity_id(self, _platform: str, _domain: str, unique_id: str) -> str | None:
        return self._mapping.get(unique_id)


def _ensure_zone_module() -> None:
    if "homeassistant.components.zone" in sys.modules:
        return
    zone_module = ModuleType("homeassistant.components.zone")
    zone_module.DOMAIN = "zone"
    sys.modules["homeassistant.components.zone"] = zone_module


def _ensure_event_helper() -> None:
    event_module = sys.modules.get("homeassistant.helpers.event")
    if event_module is None:
        event_module = ModuleType("homeassistant.helpers.event")
        sys.modules["homeassistant.helpers.event"] = event_module
    if not hasattr(event_module, "async_track_state_change_event"):
        event_module.async_track_state_change_event = (  # type: ignore[attr-defined]
            lambda *_args, **_kwargs: None
        )


def _ensure_entity_registry_module() -> ModuleType:
    helpers_pkg = sys.modules.get("homeassistant.helpers")
    if helpers_pkg is None:
        helpers_pkg = ModuleType("homeassistant.helpers")
        helpers_pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers"] = helpers_pkg
    module = sys.modules.get("homeassistant.helpers.entity_registry")
    if module is None:
        module = ModuleType("homeassistant.helpers.entity_registry")
        sys.modules["homeassistant.helpers.entity_registry"] = module
        setattr(helpers_pkg, "entity_registry", module)
    return module


def test_should_filter_detection_passive_zone(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    """Warn and skip substitution when zone.home is passive."""

    _ensure_core_state()
    _ensure_zone_module()
    _ensure_event_helper()
    entity_registry_module = _ensure_entity_registry_module()
    from custom_components.googlefindmy.google_home_filter import GoogleHomeFilter

    zone_state = SimpleNamespace(
        entity_id="zone.home",
        state="zoning",
        attributes={
            "latitude": 1.23,
            "longitude": 4.56,
            "passive": True,
        },
    )

    hass = SimpleNamespace()
    hass.states = _StatesStub({"zone.home": zone_state})
    hass.data = {}
    hass.config_entries = SimpleNamespace()

    registry = _EntityRegistryStub(mapping={})

    monkeypatch.setattr(entity_registry_module, "async_get", lambda _hass: registry)

    unsub_calls: list[tuple[Any, tuple[str, ...]]] = []

    def _track_state_change_event(hass_obj: Any, entity_ids: list[str], callback) -> None:
        unsub_calls.append((hass_obj, tuple(entity_ids)))
        return lambda: None

    monkeypatch.setattr(
        "custom_components.googlefindmy.google_home_filter.async_track_state_change_event",
        _track_state_change_event,
    )

    filter_config = {
        OPT_GOOGLE_HOME_FILTER_ENABLED: True,
        OPT_GOOGLE_HOME_FILTER_KEYWORDS: ["nest"],
    }

    gh_filter = GoogleHomeFilter(hass, filter_config)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        outcome = gh_filter.should_filter_detection("device-1", "Nest Speaker")

    assert outcome == (False, None)
    assert unsub_calls == [(hass, ("zone.home",))]
    assert any("zone.home is passive" in message for message in caplog.messages)

