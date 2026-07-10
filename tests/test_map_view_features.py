"""Feature coverage for the map view endpoint."""

from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.util.dt import UTC

from custom_components.googlefindmy import map_view as _map_view

map_view = _map_view

if getattr(map_view, "__file__", None) is None:
    importlib.invalidate_caches()
    map_view = importlib.reload(
        importlib.import_module("custom_components.googlefindmy.map_view")
    )
from custom_components.googlefindmy.const import (
    DOMAIN,
    map_token_hex_digest,
    map_token_secret_seed,
)
from tests.helpers.config_entries_stub import make_config_entry


class _StubConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


class _StubRegistryEntry:
    """Entity registry entry stub that optionally links to a device."""

    def __init__(
        self,
        *,
        entity_id: str,
        unique_id: str,
        config_entry_id: str,
        device_id: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id
        self.device_id = device_id
        self.platform = DOMAIN


class _StubEntityRegistry:
    def __init__(self, entries: list[_StubRegistryEntry]) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        for entry in self.entities.values():
            if (
                entry.platform == platform
                and entry.unique_id == unique_id
                and entry.entity_id.startswith(f"{domain}.")
            ):
                return entry.entity_id
        return None

    def async_get(self, entity_id: str) -> _StubRegistryEntry | None:
        return self.entities.get(entity_id)


class _StubDeviceEntry:
    """Device registry entry stub with user-configurable labels."""

    def __init__(
        self, *, name: str | None = None, name_by_user: str | None = None
    ) -> None:
        self.name = name
        self.name_by_user = name_by_user


class _StubDeviceRegistry:
    def __init__(self, devices: dict[str, _StubDeviceEntry]) -> None:
        self.devices = devices

    def async_get(
        self, device_id: str
    ) -> _StubDeviceEntry | None:  # pragma: no cover - passthrough
        return self.devices.get(device_id)


class _StubState:
    def __init__(self, *, latitude: float, longitude: float) -> None:
        self.attributes: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "gps_accuracy": 5,
            "semantic_name": "Office",
            "is_own_report": True,
        }
        self.state = "home"
        self.last_updated = datetime(2024, 1, 1, tzinfo=UTC)


class _StubHass:
    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {"core.uuid": "test-ha"}
        self.config_entries = _StubConfigEntries(entries)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _install_history_stub(
    monkeypatch: pytest.MonkeyPatch,
    entity_id: str,
    state: _StubState,
    *,
    calls: list[str] | None = None,
) -> None:
    history_module = ModuleType("homeassistant.components.recorder.history")

    def _get_significant_states(
        _hass: Any, _start: Any, _end: Any, _entity_ids: list[str]
    ) -> dict[str, list[_StubState]]:
        if calls is not None:
            calls.extend(_entity_ids)
        return {entity_id: [state]}

    history_module.get_significant_states = _get_significant_states  # type: ignore[attr-defined]

    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.history = history_module
    components_module = ModuleType("homeassistant.components")
    components_module.recorder = recorder_module

    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder.history", history_module
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder", recorder_module
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)


@pytest.mark.asyncio
async def test_get_missing_token_returns_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return 401 when no token is provided."""

    hass = _StubHass([])
    view = map_view.GoogleFindMyMapView(hass)

    response = await view.get(SimpleNamespace(query={}), device_id="device123")

    assert response.status == 401


@pytest.mark.asyncio
async def test_get_invalid_token_returns_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return 401 when token does not match any entry."""

    entry = make_config_entry(entry_id="entry-id", runtime_data=None)
    hass = _StubHass([entry])
    view = map_view.GoogleFindMyMapView(hass)

    response = await view.get(
        SimpleNamespace(query={"token": "invalid"}), device_id="device123"
    )

    assert response.status == 401


@pytest.mark.asyncio
async def test_get_authorized_includes_leaflet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return 200 HTML with Leaflet content when authorized."""

    device_id = "device123"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Test Device"}])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.device123",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id=entry.entry_id,
    )
    registry = _StubEntityRegistry([registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    state = _StubState(latitude=10.0, longitude=20.0)
    _install_history_stub(monkeypatch, registry_entry.entity_id, state)

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()


@pytest.mark.asyncio
async def test_get_skips_foreign_registry_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not leak history from a tracker bound to a different config entry."""

    device_id = "device456"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Foreign Device"}])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.device456",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id="other-entry",
    )
    registry = _StubEntityRegistry([registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    # Guard against history lookups when entity_id is rejected
    history_module = ModuleType("homeassistant.components.recorder.history")

    def _get_significant_states(
        _hass: Any, _start: Any, _end: Any, _entity_ids: list[str]
    ) -> dict[str, list[_StubState]]:
        raise AssertionError("history should not be queried for mismatched entries")

    history_module.get_significant_states = _get_significant_states  # type: ignore[attr-defined]
    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.history = history_module
    components_module = ModuleType("homeassistant.components")
    components_module.recorder = recorder_module

    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder.history", history_module
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder", recorder_module
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()


@pytest.mark.asyncio
async def test_get_prefers_matching_entry_over_foreign_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use registry entries scoped to the token's config entry even with competing IDs."""

    device_id = "device789"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Scoped Device"}])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    foreign_registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.foreign_device",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id="foreign-entry",
    )
    scoped_registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.scoped_device",
        unique_id=f"{DOMAIN}_{device_id}",
        config_entry_id=entry.entry_id,
    )
    registry = _StubEntityRegistry([foreign_registry_entry, scoped_registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    state = _StubState(latitude=15.0, longitude=25.0)
    history_calls: list[str] = []
    _install_history_stub(
        monkeypatch,
        scoped_registry_entry.entity_id,
        state,
        calls=history_calls,
    )

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()
    assert history_calls == [scoped_registry_entry.entity_id]


@pytest.mark.asyncio
async def test_registry_labels_fill_blank_coordinator_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback to device registry labels when coordinator names are blank or missing."""

    device_id = "device987"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "   "}])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.device987",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id=entry.entry_id,
        device_id="device-reg-id",
    )
    registry = _StubEntityRegistry([registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    device_registry = _StubDeviceRegistry(
        {"device-reg-id": _StubDeviceEntry(name_by_user="Registry Label")}
    )
    monkeypatch.setattr(map_view.dr, "async_get", lambda _hass: device_registry)

    state = _StubState(latitude=1.0, longitude=2.0)
    _install_history_stub(monkeypatch, registry_entry.entity_id, state)

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "registry label - location history" in response.text.lower()


@pytest.mark.asyncio
async def test_missing_registry_and_coordinator_use_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a placeholder name when neither coordinator nor registry provides labels."""

    device_id = "device000"
    coordinator = SimpleNamespace(data=[])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    registry = _StubEntityRegistry([])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(map_view.dr, "async_get", lambda _hass: _StubDeviceRegistry({}))

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "unknown device - location history" in response.text.lower()


@pytest.mark.asyncio
async def test_redirect_uses_relative_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect view should preserve query parameters on a relative URL."""

    hass = _StubHass([])
    view = map_view.GoogleFindMyMapRedirectView(hass)

    request = SimpleNamespace(query={"token": "abc", "start": "2024-01-01T00:00:00Z"})

    with pytest.raises(map_view.web.HTTPFound) as ctx:
        await view.get(request, device_id="device123")

    assert (
        ctx.value.location
        == "/api/googlefindmy/map/device123?token=abc&start=2024-01-01T00%3A00%3A00Z"
    )


def _install_multi_history_stub(
    monkeypatch: pytest.MonkeyPatch,
    entity_id: str,
    states: list[_StubState],
) -> None:
    """Install a recorder history stub returning multiple states for one entity."""

    history_module = ModuleType("homeassistant.components.recorder.history")

    def _get_significant_states(
        _hass: Any, _start: Any, _end: Any, _entity_ids: list[str]
    ) -> dict[str, list[_StubState]]:
        return {entity_id: list(states)}

    history_module.get_significant_states = _get_significant_states  # type: ignore[attr-defined]

    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.history = history_module
    components_module = ModuleType("homeassistant.components")
    components_module.recorder = recorder_module

    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder.history", history_module
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder", recorder_module
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)


async def _render_map_with_states(
    monkeypatch: pytest.MonkeyPatch,
    states: list[_StubState],
    query_extra: dict[str, str],
) -> Any:
    """Render the map view for the given history states and extra query params."""

    device_id = "device123"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Test Device"}])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", lambda: SimpleNamespace)

    registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.device123",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id=entry.entry_id,
    )
    registry = _StubEntityRegistry([registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    _install_multi_history_stub(monkeypatch, registry_entry.entity_id, states)

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    query = {"token": token, **query_extra}
    return await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query=query),
        device_id=device_id,
    )


def _precise_and_coarse_states() -> list[_StubState]:
    """One precise point (5 m) and one coarse point (150 m), distinct timestamps."""

    precise = _StubState(latitude=10.0, longitude=20.0)
    precise.attributes["gps_accuracy"] = 5
    precise.attributes["last_seen"] = 1704067200.0

    coarse = _StubState(latitude=50.0, longitude=60.0)
    coarse.attributes["gps_accuracy"] = 150
    coarse.attributes["last_seen"] = 1704067260.0

    return [precise, coarse]


@pytest.mark.asyncio
async def test_accuracy_filter_drops_points_worse_than_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A point coarser than the requested accuracy must not be rendered."""

    response = await _render_map_with_states(
        monkeypatch, _precise_and_coarse_states(), {"accuracy": "10"}
    )

    assert response.status == 200
    assert "showing 1 point" in response.text.lower()
    assert '"lat": 10.0' in response.text
    assert '"lat": 50.0' not in response.text


@pytest.mark.asyncio
async def test_accuracy_filter_zero_keeps_all_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default threshold of 0 disables the filter and keeps every point."""

    response = await _render_map_with_states(
        monkeypatch, _precise_and_coarse_states(), {"accuracy": "0"}
    )

    assert response.status == 200
    assert "showing 2 points" in response.text.lower()
    assert '"lat": 10.0' in response.text
    assert '"lat": 50.0' in response.text


@pytest.mark.asyncio
async def test_accuracy_filter_drops_points_with_missing_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A point without gps_accuracy is corrupted data and must be dropped.

    Missing accuracy is normalized to the conservative fallback radius
    (safe_accuracy), so it can no longer masquerade as a best-possible 0.0m
    point and slip past a tighter slider threshold.
    """

    unknown = _StubState(latitude=33.0, longitude=44.0)
    unknown.attributes.pop("gps_accuracy", None)
    unknown.attributes["last_seen"] = 1704067300.0

    response = await _render_map_with_states(monkeypatch, [unknown], {"accuracy": "10"})

    assert response.status == 200
    assert "showing 0 points" in response.text.lower()
    assert '"lat": 33.0' not in response.text


@pytest.mark.asyncio
async def test_accuracy_filter_drops_points_with_zero_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reported gps_accuracy of 0.0m is the Android error code, not a fix."""

    zeroed = _StubState(latitude=33.0, longitude=44.0)
    zeroed.attributes["gps_accuracy"] = 0.0
    zeroed.attributes["last_seen"] = 1704067300.0

    response = await _render_map_with_states(monkeypatch, [zeroed], {"accuracy": "10"})

    assert response.status == 200
    assert "showing 0 points" in response.text.lower()
    assert '"lat": 33.0' not in response.text


@pytest.mark.asyncio
async def test_accuracy_filter_drops_points_with_negative_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative gps_accuracy is corrupted; the raw value would wrongly pass.

    Before normalization ``-5 > 10`` is ``False`` so the point slipped through;
    safe_accuracy maps it to the fallback radius and the slider now drops it.
    """

    negative = _StubState(latitude=33.0, longitude=44.0)
    negative.attributes["gps_accuracy"] = -5.0
    negative.attributes["last_seen"] = 1704067300.0

    response = await _render_map_with_states(
        monkeypatch, [negative], {"accuracy": "10"}
    )

    assert response.status == 200
    assert "showing 0 points" in response.text.lower()
    assert '"lat": 33.0' not in response.text


@pytest.mark.asyncio
async def test_accuracy_filter_drops_points_with_nan_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NaN gps_accuracy must be dropped; ``NaN > threshold`` is always False.

    Without normalization the comparison silently admits NaN points under any
    slider; safe_accuracy rejects non-finite values to the fallback radius.
    """

    nan_state = _StubState(latitude=33.0, longitude=44.0)
    nan_state.attributes["gps_accuracy"] = float("nan")
    nan_state.attributes["last_seen"] = 1704067300.0

    response = await _render_map_with_states(
        monkeypatch, [nan_state], {"accuracy": "10"}
    )

    assert response.status == 200
    assert "showing 0 points" in response.text.lower()
    assert '"lat": 33.0' not in response.text


@pytest.mark.asyncio
async def test_invalid_accuracy_rendered_as_fallback_when_filter_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the filter off, an invalid point is kept but shown at the fallback.

    The point is no longer advertised as a pinpoint 0.0m fix; it carries the
    conservative 200m fallback radius, matching the live entity's accuracy.
    """

    unknown = _StubState(latitude=33.0, longitude=44.0)
    unknown.attributes.pop("gps_accuracy", None)
    unknown.attributes["last_seen"] = 1704067300.0

    response = await _render_map_with_states(monkeypatch, [unknown], {"accuracy": "0"})

    assert response.status == 200
    assert "showing 1 point" in response.text.lower()
    assert '"lat": 33.0' in response.text
    assert '"accuracy": 200.0' in response.text
    assert '"accuracy": 0.0' not in response.text


# --------------------------------------------------------------------------- #
# Full-coverage suite: token buckets, coordinator resolution, name resolution, #
# query-filter parsing and per-state fallbacks.                                #
# --------------------------------------------------------------------------- #


async def _render_view(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_data: Any,
    states: list[_StubState],
    query_extra: dict[str, str] | None = None,
    registry: Any = None,
    entity_id: str = "device_tracker.device123",
    device_id: str = "device123",
) -> Any:
    """Render the map view with full control over runtime data and registry.

    The helper authorizes with a static token, installs a recorder history
    stub for ``entity_id`` and resolves the coordinator class to
    ``SimpleNamespace`` so callers can drive the device-name resolution and
    history-parsing branches with hand-built stubs.
    """

    entry = make_config_entry(entry_id="entry-id", runtime_data=runtime_data)
    hass = _StubHass([entry])

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", lambda: SimpleNamespace)

    if registry is None:
        registry = _StubEntityRegistry(
            [
                _StubRegistryEntry(
                    entity_id=entity_id,
                    unique_id=f"{entry.entry_id}:{device_id}",
                    config_entry_id=entry.entry_id,
                )
            ]
        )
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    _install_multi_history_stub(monkeypatch, entity_id, states)

    token = map_token_hex_digest(
        map_token_secret_seed(hass.data["core.uuid"], entry.entry_id, False)
    )
    query = {"token": token, **(query_extra or {})}
    return await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query=query),
        device_id=device_id,
    )


def test_entry_accept_tokens_weekly_returns_two_buckets() -> None:
    """Weekly tokens accept both the current and the previous bucket."""

    hass = _StubHass([])

    weekly = map_view._entry_accept_tokens(hass, "entry-id", True)
    static = map_view._entry_accept_tokens(hass, "entry-id", False)

    assert len(weekly) == 2  # current and previous week
    assert len(static) == 1
    assert weekly.isdisjoint(static)


def test_resolve_coordinator_class_returns_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A populated cache short-circuits the lazy import."""

    monkeypatch.setattr(map_view, "_COORDINATOR_CLASS", SimpleNamespace)

    assert map_view._resolve_coordinator_class() is SimpleNamespace


@pytest.mark.asyncio
async def test_device_name_skipped_when_runtime_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without runtime data the coordinator name lookup is skipped."""

    response = await _render_view(
        monkeypatch,
        runtime_data=None,
        states=[_StubState(latitude=10.0, longitude=20.0)],
    )

    assert response.status == 200
    assert "Unknown Device" in response.text


@pytest.mark.asyncio
async def test_device_name_skipped_when_runtime_not_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime object without a coordinator attribute resolves no name."""

    response = await _render_view(
        monkeypatch,
        runtime_data=object(),
        states=[_StubState(latitude=10.0, longitude=20.0)],
    )

    assert response.status == 200
    assert "Unknown Device" in response.text


@pytest.mark.asyncio
async def test_device_name_loop_skips_non_matching_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator scan ignores other devices before the match."""

    coordinator = SimpleNamespace(
        data=[
            {"id": "other-device", "name": "Other"},
            {"id": "device123", "name": "Real Device"},
        ]
    )

    response = await _render_view(
        monkeypatch,
        runtime_data=coordinator,
        states=[_StubState(latitude=10.0, longitude=20.0)],
    )

    assert response.status == 200
    assert "Real Device" in response.text


@pytest.mark.asyncio
async def test_entity_entry_refetched_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved entity_id without a registry entry still drives history."""

    class _GhostEntityRegistry:
        """Resolves an entity_id but never returns a registry entry object."""

        entities: dict[str, Any] = {}

        def async_get_entity_id(
            self, _domain: str, _platform: str, _unique_id: str
        ) -> str:
            return "device_tracker.ghost"

        def async_get(self, _entity_id: str) -> None:
            return None

    response = await _render_view(
        monkeypatch,
        runtime_data=None,
        states=[_StubState(latitude=12.0, longitude=21.0)],
        registry=_GhostEntityRegistry(),
        entity_id="device_tracker.ghost",
    )

    assert response.status == 200
    assert "showing 1 point" in response.text.lower()


@pytest.mark.asyncio
async def test_time_filters_parsed_from_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid ISO start/end values populate the filter controls."""

    response = await _render_view(
        monkeypatch,
        runtime_data=SimpleNamespace(data=[{"id": "device123", "name": "Test"}]),
        states=[_StubState(latitude=10.0, longitude=20.0)],
        query_extra={
            "start": "2024-03-15T08:00:00Z",
            "end": "2024-03-16T09:30:00Z",
        },
    )

    assert response.status == 200
    assert 'value="2024-03-15T08:00"' in response.text
    assert 'value="2024-03-16T09:30"' in response.text


@pytest.mark.asyncio
async def test_time_filters_invalid_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable start value is swallowed and defaults are kept."""

    response = await _render_view(
        monkeypatch,
        runtime_data=SimpleNamespace(data=[{"id": "device123", "name": "Test"}]),
        states=[_StubState(latitude=10.0, longitude=20.0)],
        query_extra={"start": "not-a-date"},
    )

    assert response.status == 200
    assert 'value="not-a-date"' not in response.text


@pytest.mark.asyncio
async def test_naive_time_filters_get_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timezone-naive start/end values are coerced to UTC."""

    response = await _render_view(
        monkeypatch,
        runtime_data=SimpleNamespace(data=[{"id": "device123", "name": "Test"}]),
        states=[_StubState(latitude=10.0, longitude=20.0)],
        query_extra={
            "start": "2024-06-01T00:00:00",
            "end": "2024-06-02T00:00:00",
        },
    )

    assert response.status == 200
    assert 'value="2024-06-01T00:00"' in response.text
    assert 'value="2024-06-02T00:00"' in response.text


@pytest.mark.asyncio
async def test_accuracy_filter_invalid_defaults_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric accuracy value disables the filter."""

    response = await _render_map_with_states(
        monkeypatch, _precise_and_coarse_states(), {"accuracy": "abc"}
    )

    assert response.status == 200
    assert "showing 2 points" in response.text.lower()


@pytest.mark.asyncio
async def test_last_seen_non_string_falls_back_to_state_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-string last_seen is ignored and the state time is used."""

    state = _StubState(latitude=10.0, longitude=20.0)
    state.attributes["last_seen"] = ["not", "a", "scalar"]

    response = await _render_view(
        monkeypatch,
        runtime_data=SimpleNamespace(data=[{"id": "device123", "name": "Test"}]),
        states=[state],
    )

    assert response.status == 200
    assert "showing 1 point" in response.text.lower()


@pytest.mark.asyncio
async def test_last_seen_unparseable_string_falls_back_to_state_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A last_seen string that is neither float nor ISO is ignored."""

    state = _StubState(latitude=10.0, longitude=20.0)
    state.attributes["last_seen"] = "definitely-not-a-timestamp"

    response = await _render_view(
        monkeypatch,
        runtime_data=SimpleNamespace(data=[{"id": "device123", "name": "Test"}]),
        states=[state],
    )

    assert response.status == 200
    assert "showing 1 point" in response.text.lower()


@pytest.mark.asyncio
async def test_invalid_state_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state with a non-numeric coordinate is skipped, valid ones survive."""

    invalid = _StubState(latitude=None, longitude=20.0)
    valid = _StubState(latitude=77.0, longitude=88.0)

    response = await _render_view(
        monkeypatch,
        runtime_data=SimpleNamespace(data=[{"id": "device123", "name": "Test"}]),
        states=[invalid, valid],
    )

    assert response.status == 200
    assert "showing 1 point" in response.text.lower()
    assert '"lat": 77.0' in response.text


@pytest.mark.asyncio
async def test_redirect_missing_token_returns_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect view rejects requests without a token via HTTP 400."""

    view = map_view.GoogleFindMyMapRedirectView(_StubHass([]))

    response = await view.get(SimpleNamespace(query={}), device_id="device123")

    assert response.status == 400


# ---------------------------------------------------------------------------
# Accuracy-circle feature: server field + three-layer client rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_location_row_marks_estimated_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each row exposes accuracy_estimated: real measurement vs. fallback radius.

    The client needs this flag to draw accuracy circles only for real
    measurements. A real gps_accuracy yields False; the Android error code 0.0
    (normalized to the conservative fallback radius) yields True.
    """

    real = _StubState(latitude=10.0, longitude=20.0)
    real.attributes["gps_accuracy"] = 30
    real.attributes["last_seen"] = 1704067200.0

    fallback = _StubState(latitude=50.0, longitude=60.0)
    fallback.attributes["gps_accuracy"] = 0.0  # error code -> fallback radius
    fallback.attributes["last_seen"] = 1704067260.0

    response = await _render_map_with_states(
        monkeypatch, [real, fallback], {"accuracy": "0"}
    )

    assert response.status == 200
    # Both points survive the disabled filter; one real, one estimated.
    assert '"accuracy_estimated": false' in response.text
    assert '"accuracy_estimated": true' in response.text


@pytest.mark.asyncio
async def test_map_html_has_three_accuracy_layers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered JS carries dots-fade, ambient rings and the focus disc."""

    response = await _render_map_with_states(
        monkeypatch, _precise_and_coarse_states(), {"accuracy": "0"}
    )
    html = response.text

    # Two editorial/HA-sourced constants, each defined exactly once.
    assert html.count("var FOCUS_FILL_OPACITY = 0.12;") == 1
    assert html.count("var FADE_SPAN = 0.8;") == 1
    assert "var focusCircle = null;" in html

    # Layer 3: exactly one filled focus disc, coupled to the popup lifecycle.
    assert "function setFocus(loc, latlng)" in html
    assert "fillOpacity: FOCUS_FILL_OPACITY" in html
    assert "marker.on('popupopen'" in html
    assert "marker.on('popupclose'" in html

    # Layer 2: ambient rings are stroke-only (no area fill -> no alpha stacking).
    assert "fill: false" in html

    # autoPan:false prevents the auto-opened popup from panning the map.
    assert "{autoPan: false}" in html


@pytest.mark.asyncio
async def test_map_html_replaces_dead_marker_fade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dead/inverted fade is gone; the dot fade follows the HA rank model."""

    response = await _render_map_with_states(
        monkeypatch, _precise_and_coarse_states(), {"accuracy": "0"}
    )
    html = response.text

    # The old dead variable and the hardcoded fill opacity must be gone.
    assert "1.0 - (idx / locations.length * 0.5)" not in html
    assert "fillOpacity: 0.8" not in html

    # Dot fade uses the HA model and references FADE_SPAN (newest 1.0, oldest
    # 1 - FADE_SPAN == 0.2). The ambient ring floor is 0 (idx/(n-1)), a
    # deliberately different floor from the dots.
    assert "(1 - FADE_SPAN) + (idx / (n - 1)) * FADE_SPAN" in html
    assert "idx / (n - 1)" in html


@pytest.mark.asyncio
async def test_map_html_auto_focuses_newest_real_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The newest real-accuracy point auto-opens, as if it were clicked.

    Auto-focus is no longer coupled to the last index: every non-estimated point
    assigns ``autoFocusMarker`` and, because locations are sorted oldest->newest,
    the last assignment selects the newest real point even when the newest point
    overall is an estimated fallback (Codex #1124 finding 2).

    Mutation guard: dropping the negation removes this exact substring and turns
    the test red; re-coupling it to ``idx === n - 1`` is asserted against below.
    """

    response = await _render_map_with_states(
        monkeypatch, _precise_and_coarse_states(), {"accuracy": "0"}
    )
    html = response.text

    assert "if (!loc.accuracy_estimated) { autoFocusMarker = marker; }" in html
    assert "idx === n - 1 && !loc.accuracy_estimated" not in html
    assert "autoFocusMarker.openPopup();" in html


def _extract_locations_json(html: str) -> list[dict[str, Any]]:
    """Pull the `var locations = [...]` array back out of the rendered HTML."""

    match = re.search(r"var locations = (\[.*?\]);", html, re.DOTALL)
    assert match is not None, "locations array not found in rendered HTML"
    return json.loads(match.group(1))


@pytest.mark.asyncio
async def test_newest_point_can_be_estimated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the newest point is estimated, the row is flagged accordingly.

    Points are sorted ascending by last_seen, so the last array element is the
    newest. The client uses accuracy_estimated of that element to decide whether
    to auto-open a focus circle; here the newest is a fallback radius, so it must
    be marked estimated (no misleading auto-circle).
    """

    older_real = _StubState(latitude=10.0, longitude=20.0)
    older_real.attributes["gps_accuracy"] = 25
    older_real.attributes["last_seen"] = 1704067200.0

    newest_estimated = _StubState(latitude=50.0, longitude=60.0)
    newest_estimated.attributes["gps_accuracy"] = 0.0  # error code -> fallback
    newest_estimated.attributes["last_seen"] = 1704067260.0

    response = await _render_map_with_states(
        monkeypatch, [older_real, newest_estimated], {"accuracy": "0"}
    )

    assert response.status == 200
    locations = _extract_locations_json(response.text)
    assert len(locations) == 2
    assert locations[0]["accuracy_estimated"] is False
    assert locations[-1]["accuracy_estimated"] is True


@pytest.mark.asyncio
async def test_popup_coordinates_display_is_shortened_copy_stays_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Popup shows rounded coordinates; the copy button keeps full precision.

    The display line must read from ``coordDisplay`` (rounded via the named
    ``GFMY_COORD_DISPLAY_DECIMALS`` constant, then padding zeros stripped), while
    the coordinate copy handler must still serialize the raw ``loc.lat``/
    ``loc.lon`` without any ``toFixed``. Reverting the display back to
    ``coordStr`` (the mutation counter-proof) makes assertion (a)/(e) fail.
    """

    state = _StubState(latitude=10.8368252529521, longitude=49.7380204)
    state.attributes["gps_accuracy"] = 58
    state.attributes["last_seen"] = 1704067200.0

    response = await _render_map_with_states(monkeypatch, [state], {"accuracy": "0"})
    assert response.status == 200
    html = response.text

    # (a) display precision constant is defined at script level and set to 5
    assert "var GFMY_COORD_DISPLAY_DECIMALS = 5;" in html
    # (b) the coordinates display line reads coordDisplay, not the raw coordStr
    assert 'GFMY_LABELS.coordinates + "</b> " + coordDisplay +' in html
    # (c) mutation counter-proof: the display line must NOT use coordStr
    assert 'GFMY_LABELS.coordinates + "</b> " + coordStr +' not in html
    # (d) copy handler keeps the raw full-precision expression, no toFixed
    assert 'gfmyCopyToClipboard(loc.lat + ", " + loc.lon, coBtn)' in html
    # (e) display expression strips padding zeros via the outer Number(...)
    assert (
        "var coordDisplay = "
        "Number(Number(loc.lat).toFixed(GFMY_COORD_DISPLAY_DECIMALS)) + "
        '", " + '
        "Number(Number(loc.lon).toFixed(GFMY_COORD_DISPLAY_DECIMALS));"
    ) in html
    # (f) copy path is untouched by rounding: no toFixed inside the copy handler
    assert 'gfmyCopyToClipboard(loc.lat + ", " + loc.lon, coBtn).toFixed' not in html
