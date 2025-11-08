# tests/test_config_flow_subentry_sync.py
"""Tests validating config flow subentry creation and updates."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import (
    ConfigEntrySubEntryManager,
    ConfigEntrySubentryDefinition,
    config_flow,
)
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AUTH_METHOD,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DOMAIN,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_OPTIONS_SCHEMA_VERSION,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    service_device_identifier,
)
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return a deterministic config_subentry_id for the given entry/key pair."""

    return f"{entry_id}-{key}-subentry"


class _ConfigEntriesManagerStub:
    """Stub mimicking Home Assistant's config entries manager."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.entry_updates: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.setup_calls: list[str] = []

    def async_entries(self, domain: str | None = None) -> list[Any]:
        if domain and domain != DOMAIN:
            return []
        return [self._entry]

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self._entry.entry_id:
            return self._entry
        return None

    def async_get_subentries(self, entry_id: str) -> list[ConfigSubentry]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        return list(entry.subentries.values())

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True

    def async_update_entry(self, entry: _EntryStub, **kwargs: Any) -> None:
        assert entry is self._entry
        payload = dict(kwargs)
        self.entry_updates.append(payload)
        if "data" in payload:
            entry.data = payload["data"]
        if "options" in payload:
            entry.options = payload["options"]
        if "version" in payload:
            entry.version = payload["version"]

    def async_create_subentry(
        self,
        entry: _EntryStub,
        *,
        data: dict[str, Any],
        title: str,
        unique_id: str | None,
        subentry_type: str,
        translation_key: str | None = None,
    ) -> ConfigSubentry:
        assert entry is self._entry
        subentry = ConfigSubentry(
            data=MappingProxyType(dict(data)),
            subentry_type=subentry_type,
            title=title,
            unique_id=unique_id,
            subentry_id=_stable_subentry_id(entry.entry_id, data["group_key"]),
            translation_key=translation_key,
        )
        return self.async_add_subentry(entry, subentry)

    def async_add_subentry(
        self, entry: _EntryStub, subentry: ConfigSubentry
    ) -> ConfigSubentry:
        assert entry is self._entry
        if isinstance(subentry.unique_id, str):
            for existing in entry.subentries.values():
                if existing is subentry:
                    continue
                if existing.unique_id == subentry.unique_id:
                    raise data_entry_flow.AbortFlow("already_configured")

        entry.subentries[subentry.subentry_id] = subentry
        self.created.append(
            {
                "data": dict(subentry.data),
                "title": subentry.title,
                "unique_id": subentry.unique_id,
                "subentry_type": subentry.subentry_type,
                "config_subentry_id": subentry.subentry_id,
                "translation_key": getattr(subentry, "translation_key", None),
                "object": subentry,
            }
        )
        return subentry

    def async_update_subentry(
        self,
        entry: _EntryStub,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
        translation_key: str | None = None,
    ) -> None:
        assert entry is self._entry
        if unique_id is not None:
            for existing in entry.subentries.values():
                if existing is subentry:
                    continue
                if existing.unique_id == unique_id:
                    raise data_entry_flow.AbortFlow("already_configured")
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        if translation_key is not None:
            subentry.translation_key = translation_key
        self.updated.append(
            {
                "data": dict(data),
                "title": title,
                "unique_id": unique_id,
                "config_subentry_id": subentry.subentry_id,
                "subentry": subentry,
                "translation_key": translation_key,
            }
        )

    async def async_remove_subentry(
        self, entry: _EntryStub, *, subentry_id: str
    ) -> bool:
        assert entry is self._entry
        removed = self._entry.subentries.pop(subentry_id, None)
        if removed is None:
            return False
        self.removed.append(subentry_id)
        return True


class _HassStub:
    """Home Assistant stub exposing config entry helpers to the flow."""

    def __init__(self, entry: _EntryStub) -> None:
        self.config_entries = _ConfigEntriesManagerStub(entry)
        self.data: dict[str, Any] = {DOMAIN: {"entries": {entry.entry_id: entry}}}

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


class _EntryStub:
    """Lightweight config entry stub with mutable subentries."""

    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.title = "Find My"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.subentries: dict[str, ConfigSubentry] = {}
        self.runtime_data = SimpleNamespace()
        self.version = 1


def _build_flow(entry: _EntryStub) -> config_flow.ConfigFlow:
    flow = config_flow.ConfigFlow()
    hass = _HassStub(entry)
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}
    flow._auth_data = {
        DATA_AUTH_METHOD: "manual",
        CONF_OAUTH_TOKEN: "token",
        CONF_GOOGLE_EMAIL: "owner@example.com",
    }
    flow._available_devices = [("Device", "dev-1")]
    flow.unique_id = None  # type: ignore[attribute-defined-outside-init]
    flow._unique_id = None  # type: ignore[attr-defined]

    async def _set_unique_id(value: str | None) -> None:
        flow._unique_id = value  # type: ignore[attr-defined]

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
    return flow


@pytest.mark.asyncio
async def test_device_selection_creates_feature_groups_with_flags() -> None:
    """Sync helper should create service and tracker subentries with expected flags."""

    entry = _EntryStub()
    flow = _build_flow(entry)
    context_map = flow._ensure_subentry_context()

    await flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
        entry,
        options_payload={
            OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
            OPT_GOOGLE_HOME_FILTER_ENABLED: False,
            OPT_ENABLE_STATS_ENTITIES: True,
        },
        defaults={
            OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
        },
        context_map=context_map,
    )
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert len(manager.created) == 2, "both service and tracker subentries should be created"

    def _record_for(key: str) -> dict[str, Any]:
        for record in manager.created:
            if record["data"]["group_key"] == key:
                return record
        raise AssertionError(f"Subentry with key {key} not created")

    service_record = _record_for(SERVICE_SUBENTRY_KEY)
    tracker_record = _record_for(TRACKER_SUBENTRY_KEY)

    service_payload = service_record["data"]
    tracker_payload = tracker_record["data"]

    assert service_record["subentry_type"] == SUBENTRY_TYPE_SERVICE
    assert tracker_record["subentry_type"] == SUBENTRY_TYPE_TRACKER
    assert service_record["unique_id"] == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"
    assert service_record["translation_key"] == SERVICE_SUBENTRY_KEY

    assert service_payload["features"] == sorted(SERVICE_FEATURE_PLATFORMS)
    assert "visible_device_ids" not in service_payload

    assert tracker_payload["features"] == sorted(TRACKER_FEATURE_PLATFORMS)
    assert all(isinstance(feature, str) for feature in tracker_payload["features"])
    assert all(feature == feature.lower() for feature in tracker_payload["features"])
    assert tracker_payload["visible_device_ids"] == ["dev-1"]

    assert tracker_payload["has_google_home_filter"] is False
    flags = tracker_payload["feature_flags"]
    assert flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] is False
    assert flags[OPT_GOOGLE_HOME_FILTER_ENABLED] is False
    assert flags[OPT_ENABLE_STATS_ENTITIES] is True
    assert tracker_record["translation_key"] == TRACKER_SUBENTRY_KEY


@pytest.mark.asyncio
async def test_subentry_manager_deduplicates_colliding_tracker_entries() -> None:
    """ConfigEntrySubEntryManager should remove duplicates before retrying updates."""

    entry = _EntryStub()
    tracker_unique_id = f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}"
    canonical = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "feature_flags": {"example": True},
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Primary trackers",
        unique_id=tracker_unique_id,
        subentry_id=_stable_subentry_id(entry.entry_id, "tracker-primary"),
    )
    duplicate = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "feature_flags": {"stale": True},
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Duplicate trackers",
        unique_id=tracker_unique_id,
        subentry_id=_stable_subentry_id(entry.entry_id, "tracker-duplicate"),
    )
    entry.subentries[canonical.subentry_id] = canonical
    entry.subentries[duplicate.subentry_id] = duplicate

    hass = _HassStub(entry)
    manager = ConfigEntrySubEntryManager(hass, entry)

    tracker_definition = ConfigEntrySubentryDefinition(
        key=TRACKER_SUBENTRY_KEY,
        title="Google Find My devices",
        data={
            "feature_flags": {},
            "features": sorted(TRACKER_FEATURE_PLATFORMS),
            "visible_device_ids": ["dev-1"],
        },
        subentry_type=SUBENTRY_TYPE_TRACKER,
        unique_id=tracker_unique_id,
    )
    service_definition = ConfigEntrySubentryDefinition(
        key=SERVICE_SUBENTRY_KEY,
        title="Google Find My service",
        data={"features": sorted(SERVICE_FEATURE_PLATFORMS)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
    )

    await manager.async_sync([tracker_definition, service_definition])

    tracker_subentries = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.data.get("group_key") == TRACKER_SUBENTRY_KEY
    ]
    assert len(tracker_subentries) == 1
    tracker = tracker_subentries[0]
    assert tracker.unique_id == tracker_unique_id
    assert manager.get(TRACKER_SUBENTRY_KEY) is tracker
    assert duplicate.subentry_id in hass.config_entries.removed


@pytest.mark.asyncio
async def test_device_selection_updates_existing_feature_group() -> None:
    """Sync helper should update an existing subentry with new feature flags."""

    entry = _EntryStub()
    existing = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "feature_flags": {},
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Google Find My devices",
        unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY),
    )
    entry.subentries[existing.subentry_id] = existing

    flow = _build_flow(entry)
    context_map = flow._ensure_subentry_context()
    context_map[TRACKER_SUBENTRY_KEY] = existing.subentry_id

    await flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
        entry,
        options_payload={
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            OPT_GOOGLE_HOME_FILTER_ENABLED: True,
            OPT_ENABLE_STATS_ENTITIES: False,
        },
        defaults={
            OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
        },
        context_map=context_map,
    )
    manager = flow.hass.config_entries  # type: ignore[assignment]
    # Service subentry should have been created alongside updating the tracker
    created_service = next(
        record for record in manager.created if record["subentry_type"] == SUBENTRY_TYPE_SERVICE
    )
    assert created_service["data"]["group_key"] == SERVICE_SUBENTRY_KEY
    assert created_service["unique_id"] == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"
    assert created_service["translation_key"] == SERVICE_SUBENTRY_KEY

    assert manager.updated, "tracker subentry should be updated"
    payload = manager.updated[-1]["data"]
    assert payload["group_key"] == TRACKER_SUBENTRY_KEY
    assert payload["has_google_home_filter"] is True
    flags = payload["feature_flags"]
    assert flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert flags[OPT_GOOGLE_HOME_FILTER_ENABLED] is True
    assert flags[OPT_ENABLE_STATS_ENTITIES] is False
    assert manager.updated[-1]["translation_key"] == TRACKER_SUBENTRY_KEY


def test_service_device_binding_clears_stale_subentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service device updates must clear stale config_subentry_id bindings."""

    entry = _EntryStub()
    hass = SimpleNamespace()

    expected_identifiers = {service_device_identifier(entry.entry_id)}

    class _RegistryStub:
        """Capture device-registry updates issued by the binding helper."""

        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_get_device(self, *args: Any, **kwargs: Any) -> SimpleNamespace | None:
            if args:
                identifiers = args[0]
            else:
                identifiers = kwargs.get("identifiers")
            assert identifiers == expected_identifiers
            return SimpleNamespace(id="service-device", config_subentry_id="stale-id")

        def async_update_device(self, **kwargs: Any) -> None:
            self.updated.append(dict(kwargs))

    registry = _RegistryStub()

    monkeypatch.setattr(config_flow.dr, "async_get", lambda hass_arg: registry)

    config_flow.ConfigFlow._ensure_service_device_binding(
        hass,
        entry,
        coordinator=None,
        service_config_subentry_id=None,
    )

    assert registry.updated, "service device update should be issued"
    payload = registry.updated[-1]
    assert payload == {
        "device_id": "service-device",
        "config_subentry_id": None,
    }
    assert "add_config_entry_id" not in payload


def test_service_device_binding_sets_add_config_entry_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service device binding must use add_config_entry_id when subentries exist."""

    entry = _EntryStub()
    entry.service_subentry_id = "service-subentry"
    hass = SimpleNamespace()

    expected_identifiers = {
        service_device_identifier(entry.entry_id),
        (DOMAIN, f"{entry.entry_id}:{entry.service_subentry_id}:service"),
    }

    class _RegistryStub:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_get_device(
            self, *args: Any, **kwargs: Any
        ) -> SimpleNamespace | None:
            if args:
                identifiers = args[0]
            else:
                identifiers = kwargs.get("identifiers")
            assert identifiers == expected_identifiers
            return SimpleNamespace(id="service-device", config_subentry_id=None)

        def async_update_device(self, **kwargs: Any) -> None:
            self.updated.append(dict(kwargs))

    registry = _RegistryStub()

    monkeypatch.setattr(config_flow.dr, "async_get", lambda hass_arg: registry)

    config_flow.ConfigFlow._ensure_service_device_binding(
        hass,
        entry,
        coordinator=None,
        service_config_subentry_id=entry.service_subentry_id,
    )

    assert registry.updated, "service device update should be issued"
    payload = registry.updated[-1]
    assert payload == {
        "device_id": "service-device",
        "config_subentry_id": entry.service_subentry_id,
        "add_config_entry_id": entry.entry_id,
    }


def test_service_device_binding_retries_with_legacy_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct binding calls should retry with legacy kwargs on TypeError."""

    entry = _EntryStub()
    entry.service_subentry_id = "service-subentry"
    hass = SimpleNamespace()

    expected_identifiers = {
        service_device_identifier(entry.entry_id),
        (DOMAIN, f"{entry.entry_id}:{entry.service_subentry_id}:service"),
    }

    class _RegistryStub:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_get_device(
            self, *args: Any, **kwargs: Any
        ) -> SimpleNamespace | None:
            if args:
                identifiers = args[0]
            else:
                identifiers = kwargs.get("identifiers")
            assert identifiers == expected_identifiers
            return SimpleNamespace(id="service-device", config_subentry_id=None)

        def async_update_device(self, **kwargs: Any) -> None:
            self.updated.append(dict(kwargs))
            if "add_config_entry_id" in kwargs:
                raise TypeError("unexpected keyword argument 'add_config_entry_id'")

    registry = _RegistryStub()

    monkeypatch.setattr(config_flow.dr, "async_get", lambda hass_arg: registry)

    config_flow.ConfigFlow._ensure_service_device_binding(
        hass,
        entry,
        coordinator=None,
        service_config_subentry_id=entry.service_subentry_id,
    )

    assert registry.updated == [
        {
            "device_id": "service-device",
            "config_subentry_id": entry.service_subentry_id,
            "add_config_entry_id": entry.entry_id,
        },
        {
            "device_id": "service-device",
            "config_subentry_id": entry.service_subentry_id,
            "config_entry_id": entry.entry_id,
        },
    ]


@pytest.mark.asyncio
async def test_subentry_manager_adopts_existing_owner_on_repeated_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated unique_id collisions should adopt the existing owner subentry."""

    entry = _EntryStub()
    shared_unique_id = f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"
    owner = ConfigSubentry(
        data=MappingProxyType({"group_key": "service-legacy"}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Legacy service",
        unique_id=shared_unique_id,
        subentry_id=_stable_subentry_id(entry.entry_id, "service-owner"),
    )
    existing = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service placeholder",
        unique_id=f"{shared_unique_id}-old",
        subentry_id=_stable_subentry_id(entry.entry_id, "service-existing"),
    )
    entry.subentries[owner.subentry_id] = owner
    entry.subentries[existing.subentry_id] = existing

    hass = _HassStub(entry)
    manager = ConfigEntrySubEntryManager(hass, entry)

    definition = ConfigEntrySubentryDefinition(
        key=SERVICE_SUBENTRY_KEY,
        title="Google Find My service",
        data={"features": sorted(SERVICE_FEATURE_PLATFORMS)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        unique_id=shared_unique_id,
    )

    adoption_calls: list[tuple[str, str, dict[str, Any]]] = []
    original_adopt = ConfigEntrySubEntryManager._async_adopt_existing_unique_id

    async def _instrumented_adopt(
        self: ConfigEntrySubEntryManager,
        key: str,
        definition: ConfigEntrySubentryDefinition,
        unique_id: str,
        payload: dict[str, Any],
    ) -> ConfigSubentry:
        adoption_calls.append((key, unique_id, dict(payload)))
        return await original_adopt(self, key, definition, unique_id, payload)

    monkeypatch.setattr(
        ConfigEntrySubEntryManager,
        "_async_adopt_existing_unique_id",
        _instrumented_adopt,
    )

    await manager.async_sync([definition])

    assert adoption_calls, "adoption helper should be invoked after repeated collision"
    adopted = manager.get(SERVICE_SUBENTRY_KEY)
    assert adopted is owner
    assert dict(owner.data)["group_key"] == SERVICE_SUBENTRY_KEY
    assert owner.title == definition.title
    assert hass.config_entries.updated[-1]["unique_id"] is None
    assert hass.config_entries.updated[-1]["data"]["features"] == definition.data["features"]


@pytest.mark.asyncio
async def test_subentry_manager_adoption_missing_owner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adoption should raise a HomeAssistantError when no owner exists."""

    entry = _EntryStub()
    existing = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Google Find My service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}-legacy",
        subentry_id=_stable_subentry_id(entry.entry_id, "service-legacy"),
    )
    entry.subentries[existing.subentry_id] = existing

    hass = _HassStub(entry)
    manager = ConfigEntrySubEntryManager(hass, entry)

    definition = ConfigEntrySubentryDefinition(
        key=SERVICE_SUBENTRY_KEY,
        title="Google Find My service",
        data={"features": sorted(SERVICE_FEATURE_PLATFORMS)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
    )

    def _always_abort(*args: Any, **kwargs: Any) -> None:
        raise data_entry_flow.AbortFlow("already_configured")

    monkeypatch.setattr(hass.config_entries, "async_update_subentry", _always_abort)

    with pytest.raises(HomeAssistantError):
        await manager.async_sync([definition])


@pytest.mark.asyncio
async def test_subentry_manager_preserves_adopted_owner_during_cleanup() -> None:
    """Adopted subentries must not be removed via stale alias cleanup."""

    entry = _EntryStub()
    shared_unique_id = f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"
    owner = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Primary service",
        unique_id=shared_unique_id,
        subentry_id=_stable_subentry_id(entry.entry_id, "service-owner"),
    )
    entry.subentries[owner.subentry_id] = owner

    hass = _HassStub(entry)
    manager = ConfigEntrySubEntryManager(hass, entry)
    manager._managed["service-legacy"] = owner  # type: ignore[attr-defined]
    manager._cleanup["service-legacy"] = None  # type: ignore[attr-defined]

    definition = ConfigEntrySubentryDefinition(
        key=SERVICE_SUBENTRY_KEY,
        title="Google Find My service",
        data={"features": sorted(SERVICE_FEATURE_PLATFORMS)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        unique_id=shared_unique_id,
    )

    await manager.async_sync([definition])

    assert manager.get(SERVICE_SUBENTRY_KEY) is owner
    assert "service-legacy" not in manager._managed  # type: ignore[attr-defined]
    assert hass.config_entries.removed == []


def test_supported_subentry_types_disable_manual_additions() -> None:
    """Config flow should not expose manual subentry factories to Home Assistant."""

    entry = _EntryStub()
    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(entry)

    assert mapping == {}


@pytest.mark.asyncio
async def test_async_step_migrate_creates_subentries_and_moves_options() -> None:
    """Migration flow should consolidate options and sync feature subentries."""

    entry = _EntryStub()
    entry.version = 0
    entry.data = {
        CONF_GOOGLE_EMAIL: "Legacy@Example.com",
        CONF_OAUTH_TOKEN: "token",
        DATA_AUTH_METHOD: "secrets_json",
        OPT_LOCATION_POLL_INTERVAL: 900,
        OPT_DEVICE_POLL_DELAY: 8,
    }
    entry.options = {
        OPT_MIN_ACCURACY_THRESHOLD: 75,
        OPT_OPTIONS_SCHEMA_VERSION: 1,
    }

    flow = config_flow.ConfigFlow()
    hass = _HassStub(entry)
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow.unique_id = None  # type: ignore[attribute-defined-outside-init]
    flow._unique_id = None  # type: ignore[attr-defined]
    flow._available_devices = []  # type: ignore[attr-defined]
    flow.config_entry = entry  # type: ignore[assignment]

    result = await flow.async_step_migrate(entry)

    assert result["type"] == "form"
    assert entry.version == config_flow.ConfigFlow.VERSION
    assert OPT_LOCATION_POLL_INTERVAL not in entry.data
    assert OPT_DEVICE_POLL_DELAY not in entry.data
    assert entry.data[CONF_GOOGLE_EMAIL] == "legacy@example.com"
    assert entry.data[CONF_OAUTH_TOKEN] == "token"

    options = entry.options
    assert options[OPT_LOCATION_POLL_INTERVAL] == 900
    assert options[OPT_DEVICE_POLL_DELAY] == 8
    assert options[OPT_MIN_ACCURACY_THRESHOLD] == 75
    assert options[OPT_OPTIONS_SCHEMA_VERSION] == 2
    assert options[OPT_IGNORED_DEVICES] == {}

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert len(manager.created) == 2
    assert any(
        record["data"]["group_key"] == SERVICE_SUBENTRY_KEY for record in manager.created
    )
    assert any(
        record["data"]["group_key"] == TRACKER_SUBENTRY_KEY for record in manager.created
    )
    assert manager.entry_updates and manager.entry_updates[-1].get(
        "version"
    ) == config_flow.ConfigFlow.VERSION

    placeholders = flow.context.get("title_placeholders", {})
    assert placeholders.get("email") == "legacy@example.com"

    confirm = await flow.async_step_migrate_complete({})
    assert confirm["type"] == "abort"
    assert confirm["reason"] == "migration_successful"
