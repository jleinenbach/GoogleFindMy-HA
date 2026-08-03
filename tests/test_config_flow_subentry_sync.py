# tests/test_config_flow_subentry_sync.py
"""Tests validating config flow subentry creation and updates."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError

import custom_components.googlefindmy as integration
from custom_components.googlefindmy import (
    ConfigEntrySubentryDefinition,
    ConfigEntrySubEntryManager,
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
    OPT_OPTIONS_SCHEMA_VERSION,
    OPT_SEMANTIC_LOCATIONS,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    service_device_identifier,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    set_config_flow_unique_id,
)

SCHEMA_VERSION = 2
DEFAULT_LOCATION_POLL_INTERVAL = 900
DEFAULT_DEVICE_POLL_DELAY = 8
EXPECTED_CREATED_SUBENTRIES = 2


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return a deterministic config_subentry_id for the given entry/key pair."""

    return f"{entry_id}-{key}-subentry"


def _subentry_store(entry: SimpleNamespace) -> dict[str, ConfigSubentry]:
    """Return the mutable subentry store behind ``entry.subentries``.

    Home Assistant hands ``ConfigEntry.subentries`` out as a
    ``MappingProxyType``; ``tests/AGENTS.md`` ("Read-only mapping attributes on
    entry doubles") therefore asks a double that models the attribute to expose
    the read-only view and keep the mutable store private. Doubles built by
    :func:`_entry_with_subentries` do exactly that and carry the store on
    ``_subentry_store``; the older doubles in this module still hand out a bare
    ``dict``, which is its own store. Writing through this helper keeps both
    shapes working, so the read-only view can be adopted per test instead of in
    one sweep over the file.
    """

    store = getattr(entry, "_subentry_store", None)
    if store is not None:
        return cast(dict[str, ConfigSubentry], store)
    return cast(dict[str, ConfigSubentry], entry.subentries)


def _entry_with_subentries(
    *subentries: ConfigSubentry, entry_id: str = "entry-1"
) -> SimpleNamespace:
    """Return an entry double whose ``subentries`` mirrors the core's shape.

    The read-only view is not cosmetic here: a production guard narrowed to
    ``dict`` is true for a plain-``dict`` double and false for every real entry,
    so the double would hide exactly the class of defect this module tests for.
    """

    store = {subentry.subentry_id: subentry for subentry in subentries}
    entry = make_config_entry(
        entry_id=entry_id,
        title="Find My",
        subentries=MappingProxyType(store),
        runtime_data=SimpleNamespace(),
    )
    entry._subentry_store = store
    return entry


def _legacy_twin(
    entry_id: str,
    *,
    subentry_type: str,
    group_key: str,
    visible_device_ids: tuple[str, ...] = (),
    unique_id: str | None = None,
) -> ConfigSubentry:
    """Return a subentry whose stored ``group_key`` diverges from its type."""

    data: dict[str, Any] = {"group_key": group_key, "feature_flags": {}}
    if visible_device_ids:
        data["visible_device_ids"] = list(visible_device_ids)
    return ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type=subentry_type,
        title="Legacy group",
        unique_id=unique_id if unique_id is not None else f"{entry_id}-{group_key}",
        subentry_id=_stable_subentry_id(entry_id, f"{subentry_type}-{group_key}"),
    )


async def _run_sync(
    flow: config_flow.ConfigFlow,
    entry: SimpleNamespace,
    context_map: dict[str, str | None],
) -> None:
    """Drive ``_async_sync_feature_subentries`` with the module's usual payload."""

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


def _stored_visible(subentry: ConfigSubentry) -> tuple[str, ...]:
    """Return the ``visible_device_ids`` a subentry ended up carrying."""

    raw = dict(subentry.data).get("visible_device_ids") or ()
    return tuple(raw)


class _ConfigEntriesManagerStub(ConfigEntriesDomainUniqueIdLookupMixin):
    """Stub mimicking Home Assistant's config entries manager."""

    def __init__(self, entry: SimpleNamespace) -> None:
        self._entry = entry
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.entry_updates: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.setup_calls: list[str] = []
        attach_config_entries_flow_manager(self)

    def async_entries(self, domain: str | None = None) -> list[Any]:
        if domain and domain != DOMAIN:
            return []
        return [self._entry]

    def async_entry_for_domain_unique_id(
        self, domain: str, unique_id: str
    ) -> SimpleNamespace | None:
        return cast(
            SimpleNamespace | None,
            super().async_entry_for_domain_unique_id(domain, unique_id),
        )

    def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
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

    def async_update_entry(self, entry: SimpleNamespace, **kwargs: Any) -> None:
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
        entry: SimpleNamespace,
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
        self, entry: SimpleNamespace, subentry: ConfigSubentry
    ) -> ConfigSubentry:
        assert entry is self._entry
        if isinstance(subentry.unique_id, str):
            for existing in entry.subentries.values():
                if existing is subentry:
                    continue
                if existing.unique_id == subentry.unique_id:
                    raise data_entry_flow.AbortFlow("already_configured")

        _subentry_store(entry)[subentry.subentry_id] = subentry
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
        entry: SimpleNamespace,
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
        self, entry: SimpleNamespace, *, subentry_id: str
    ) -> bool:
        assert entry is self._entry
        removed = _subentry_store(self._entry).pop(subentry_id, None)
        if removed is None:
            return False
        self.removed.append(subentry_id)
        return True


class _HassStub:
    """Home Assistant stub exposing config entry helpers to the flow."""

    def __init__(self, entry: SimpleNamespace) -> None:
        self.config_entries = _ConfigEntriesManagerStub(entry)
        self.data: dict[str, Any] = {DOMAIN: {"entries": {entry.entry_id: entry}}}

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


def _build_flow(entry: SimpleNamespace) -> config_flow.ConfigFlow:
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
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str | None) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
    return flow


@pytest.mark.asyncio
async def test_device_selection_creates_feature_groups_with_flags() -> None:
    """Sync helper should create service and tracker subentries with expected flags."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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
    assert len(manager.created) == 2, (
        "both service and tracker subentries should be created"
    )

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

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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


def test_subentry_manager_normalizes_group_keys_by_type() -> None:
    """Service/tracker subentries should use canonical group keys."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    service_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": "owner@example.com",
                "features": SERVICE_FEATURE_PLATFORMS,
            }
        ),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id="service-uid",
        subentry_id=_stable_subentry_id(entry.entry_id, "service-stale"),
        translation_key=SERVICE_SUBENTRY_KEY,
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": "owner@example.com",
                "features": TRACKER_FEATURE_PLATFORMS,
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id="tracker-uid",
        subentry_id=_stable_subentry_id(entry.entry_id, "tracker-stale"),
        translation_key=TRACKER_SUBENTRY_KEY,
    )
    entry.subentries = {
        service_subentry.subentry_id: service_subentry,
        tracker_subentry.subentry_id: tracker_subentry,
    }

    hass = _HassStub(entry)

    manager = ConfigEntrySubEntryManager(hass, entry)

    assert manager.get(SERVICE_SUBENTRY_KEY) is service_subentry
    assert manager.get(TRACKER_SUBENTRY_KEY) is tracker_subentry
    assert set(manager.managed_subentries) == {
        SERVICE_SUBENTRY_KEY,
        TRACKER_SUBENTRY_KEY,
    }


def test_reconfigure_context_prefers_canonical_subentry_keys() -> None:
    """Reconfigure context should seed IDs using canonical group keys."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    service_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": "jens.leinenbach@gmail.com",
                "features": SERVICE_FEATURE_PLATFORMS,
            }
        ),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id="service-uid",
        subentry_id=_stable_subentry_id(entry.entry_id, "service-stale"),
        translation_key=SERVICE_SUBENTRY_KEY,
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": "owner@example.com",
                "features": TRACKER_FEATURE_PLATFORMS,
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id="tracker-uid",
        subentry_id=_stable_subentry_id(entry.entry_id, "tracker-stale"),
        translation_key=TRACKER_SUBENTRY_KEY,
    )
    entry.subentries = {
        service_subentry.subentry_id: service_subentry,
        tracker_subentry.subentry_id: tracker_subentry,
    }

    flow = _build_flow(entry)

    mapping = flow._reset_reconfigure_subentry_context(entry)

    assert mapping[SERVICE_SUBENTRY_KEY] == service_subentry.subentry_id
    assert mapping[TRACKER_SUBENTRY_KEY] == tracker_subentry.subentry_id


@pytest.mark.asyncio
async def test_device_selection_updates_existing_feature_group() -> None:
    """Sync helper should update an existing subentry with new feature flags."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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
        record
        for record in manager.created
        if record["subentry_type"] == SUBENTRY_TYPE_SERVICE
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

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    entry.service_subentry_id = "service-subentry"
    hass = SimpleNamespace()

    expected_identifiers = {
        service_device_identifier(entry.entry_id),
        (DOMAIN, f"{entry.entry_id}:{entry.service_subentry_id}:service"),
    }

    class _RegistryStub:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_get_device(self, *args: Any, **kwargs: Any) -> SimpleNamespace | None:
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

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    entry.service_subentry_id = "service-subentry"
    hass = SimpleNamespace()

    expected_identifiers = {
        service_device_identifier(entry.entry_id),
        (DOMAIN, f"{entry.entry_id}:{entry.service_subentry_id}:service"),
    }

    class _RegistryStub:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_get_device(self, *args: Any, **kwargs: Any) -> SimpleNamespace | None:
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

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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
    assert (
        hass.config_entries.updated[-1]["data"]["features"]
        == definition.data["features"]
    )


@pytest.mark.asyncio
async def test_subentry_manager_adoption_missing_owner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adoption should raise a HomeAssistantError when no owner exists."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
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


def test_supported_subentry_types_returns_empty_to_hide_ui() -> None:
    """Config flow should return empty dict to hide manual subentry UI buttons."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(entry)

    # Must return empty dict to hide "Add hub feature group" and
    # "Add service feature group" buttons in the HA config entry UI.
    # Subentries are provisioned programmatically by the coordinator.
    assert mapping == {}


@pytest.mark.asyncio
async def test_async_step_migrate_creates_subentries_and_moves_options() -> None:
    """Migration flow should consolidate options and sync feature subentries."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    entry.version = 0
    entry.data = {
        CONF_GOOGLE_EMAIL: "Legacy@Example.com",
        CONF_OAUTH_TOKEN: "token",
        DATA_AUTH_METHOD: "secrets_json",
        OPT_LOCATION_POLL_INTERVAL: DEFAULT_LOCATION_POLL_INTERVAL,
        OPT_DEVICE_POLL_DELAY: DEFAULT_DEVICE_POLL_DELAY,
    }
    entry.options = {
        OPT_OPTIONS_SCHEMA_VERSION: 1,
    }

    flow = config_flow.ConfigFlow()
    hass = _HassStub(entry)
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)
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
    assert options[OPT_LOCATION_POLL_INTERVAL] == DEFAULT_LOCATION_POLL_INTERVAL
    assert options[OPT_DEVICE_POLL_DELAY] == DEFAULT_DEVICE_POLL_DELAY
    assert options[OPT_OPTIONS_SCHEMA_VERSION] == SCHEMA_VERSION
    assert options[OPT_IGNORED_DEVICES] == {}

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert len(manager.created) == EXPECTED_CREATED_SUBENTRIES
    assert any(
        record["data"]["group_key"] == SERVICE_SUBENTRY_KEY
        for record in manager.created
    )
    assert any(
        record["data"]["group_key"] == TRACKER_SUBENTRY_KEY
        for record in manager.created
    )
    assert (
        manager.entry_updates
        and manager.entry_updates[-1].get("version") == config_flow.ConfigFlow.VERSION
    )

    placeholders = flow.context.get("title_placeholders", {})
    assert placeholders.get("email") == "legacy@example.com"

    confirm = await flow.async_step_migrate_complete({})
    assert confirm["type"] == "abort"
    assert confirm["reason"] == "migration_successful"


@pytest.mark.asyncio
async def test_soft_migrate_data_to_options_tracks_option_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft migration should copy all declared option keys from data to options."""

    dynamic_option = "semantic_locations_v2"
    dynamic_value = {"home": "device-home"}
    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    entry.data = {dynamic_option: dynamic_value}

    class _ConfigEntriesStub:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_update_entry(self, target: Any, **kwargs: Any) -> None:
            self.updated.append(dict(kwargs))
            if "options" in kwargs:
                target.options = kwargs["options"]

    hass = SimpleNamespace(config_entries=_ConfigEntriesStub())

    monkeypatch.setattr(
        integration,
        "OPTION_KEYS",
        integration.OPTION_KEYS + (dynamic_option,),
    )

    await integration._async_soft_migrate_data_to_options(hass, entry)

    assert entry.options[dynamic_option] == dynamic_value
    assert hass.config_entries.updated[-1]["options"][dynamic_option] == dynamic_value


@pytest.mark.asyncio
async def test_soft_migrate_data_to_options_copies_semantic_locations() -> None:
    """Soft migration should move semantic location mappings into options."""

    entry = make_config_entry(
        entry_id="entry-1",
        title="Find My",
        subentries={},
        runtime_data=SimpleNamespace(),
    )
    semantic_locations = {"home": {"lat": 1.0, "lon": 2.0}}
    entry.data = {OPT_SEMANTIC_LOCATIONS: semantic_locations}

    class _ConfigEntriesStub:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_update_entry(self, target: Any, **kwargs: Any) -> None:
            self.updated.append(dict(kwargs))
            target.options = kwargs.get("options", target.options)

    hass = SimpleNamespace(config_entries=_ConfigEntriesStub())

    await integration._async_soft_migrate_data_to_options(hass, entry)

    assert entry.options[OPT_SEMANTIC_LOCATIONS] == semantic_locations
    assert hass.config_entries.updated[-1]["options"][OPT_SEMANTIC_LOCATIONS] == (
        semantic_locations
    )


def _context_map_for(
    flow: config_flow.ConfigFlow, entry: SimpleNamespace, path: str
) -> dict[str, str | None]:
    """Return the context map the named entry path hands to the sync helper.

    The two paths differ in what the resolver starts from, which is why every
    assertion below runs on both: ``migration`` leaves the map empty
    (``_ensure_subentry_context``), so only the fallback scan can match, while
    ``reconfigure`` pre-seeds it (``_reset_reconfigure_subentry_context``), so
    the first branch decides.
    """

    if path == "reconfigure":
        return cast(
            dict[str, "str | None"],
            flow._reset_reconfigure_subentry_context(entry),  # type: ignore[attr-defined]
        )
    return cast(
        dict[str, "str | None"],
        flow._ensure_subentry_context(),  # type: ignore[attr-defined]
    )


def _subentry_of_type(
    entry: SimpleNamespace, subentry_type: str, *, exclude: ConfigSubentry | None = None
) -> ConfigSubentry | None:
    """Return the first subentry of ``subentry_type``, ignoring ``exclude``."""

    for candidate in entry.subentries.values():
        if candidate is exclude:
            continue
        if candidate.subentry_type == subentry_type:
            return candidate
    return None


def _double_writes(manager: Any) -> list[str]:
    """Return subentry ids written twice under diverging group keys.

    This is the reconfigure pathology in its observable form: the seeder can
    register one object under both core keys, after which the service branch
    writes it with the id-less service payload and the tracker branch writes
    the very same object again, this time carrying devices.
    """

    seen: dict[str, set[str]] = {}
    for record in manager.updated:
        keys = seen.setdefault(record["config_subentry_id"], set())
        keys.add(str(record["data"].get("group_key")))
    return [subentry_id for subentry_id, keys in seen.items() if len(keys) > 1]


@pytest.mark.parametrize("twin_type", [SUBENTRY_TYPE_SERVICE, SUBENTRY_TYPE_HUB])
@pytest.mark.parametrize("path", ["migration", "reconfigure"])
@pytest.mark.asyncio
async def test_a_non_device_twin_storing_the_tracker_key_never_receives_devices(
    twin_type: str, path: str
) -> None:
    """A ``service``/``hub`` twin storing ``core_tracking`` is not the tracker group.

    ``NON_DEVICE_SUBENTRY_TYPES`` is the shared axis the offering side and the
    reading side already enforce. Resolving by stored key alone let this twin
    answer for the tracker group, and the ``if not tracker_visible`` fallback
    then filled a previously clean twin with every probed device id.
    """

    entry = _entry_with_subentries()
    twin = _legacy_twin(
        entry.entry_id, subentry_type=twin_type, group_key=TRACKER_SUBENTRY_KEY
    )
    _subentry_store(entry)[twin.subentry_id] = twin

    flow = _build_flow(entry)
    await _run_sync(flow, entry, _context_map_for(flow, entry, path))

    assert _stored_visible(twin) == (), (
        f"a {twin_type}-typed subentry must never end up holding device ids"
    )
    assert dict(twin.data)["group_key"] == SERVICE_SUBENTRY_KEY, (
        "the twin is folded onto the service key, mirroring the reading side"
    )
    tracker = _subentry_of_type(entry, SUBENTRY_TYPE_TRACKER, exclude=twin)
    assert tracker is not None, "the tracker group must exist in its own right"
    assert _stored_visible(tracker) == ("dev-1",), (
        "the probed devices belong to the tracker group"
    )
    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert _double_writes(manager) == [], (
        "no subentry may be written twice under diverging group keys"
    )


@pytest.mark.parametrize("path", ["migration", "reconfigure"])
@pytest.mark.asyncio
async def test_a_tracker_typed_subentry_storing_the_service_key_keeps_its_assignment(
    path: str,
) -> None:
    """The mirror direction loses data instead of adding it, so it is pinned too.

    A ``tracker``-typed subentry that stores the *service* key used to be
    resolved as the service group and overwritten with the id-less service
    payload, which dropped the assignment it held.

    The seeded id sits deliberately **outside** the probe set
    (``_available_devices`` holds only ``dev-1``). Seeding ``dev-1`` would make
    the assertion pass under a resolver that empties the subentry and refills it
    from the probe, which is precisely the failure mode this pins against; only
    an id the probe cannot reproduce tells "kept" apart from "restored".
    """

    entry = _entry_with_subentries()
    twin = _legacy_twin(
        entry.entry_id,
        subentry_type=SUBENTRY_TYPE_TRACKER,
        group_key=SERVICE_SUBENTRY_KEY,
        visible_device_ids=("dev-legacy",),
    )
    _subentry_store(entry)[twin.subentry_id] = twin

    flow = _build_flow(entry)
    await _run_sync(flow, entry, _context_map_for(flow, entry, path))

    assert _stored_visible(twin) == ("dev-legacy",), (
        "a tracker-typed subentry must not lose its assignment to key drift"
    )
    assert dict(twin.data)["group_key"] == TRACKER_SUBENTRY_KEY, (
        "the twin answers for the tracker group, which is what its type says"
    )
    service = _subentry_of_type(entry, SUBENTRY_TYPE_SERVICE, exclude=twin)
    assert service is not None, "the service group must exist in its own right"
    assert _stored_visible(service) == ()


@pytest.mark.asyncio
async def test_folding_a_twin_does_not_abort_on_the_unique_id_it_leaves_behind() -> (
    None
):
    """The type axis turns an update into a create, and a create can collide.

    With a real service subentry present, the exact stored key wins and the
    ``hub`` twin is left alone -- keeping the ``{entry_id}-core_tracking``
    unique id that the tracker group is about to be created under.
    ``ConfigEntries.async_add_subentry`` raises ``AbortFlow`` on that
    collision, and the reconfigure path has no ``try`` around the sync, so an
    unhandled collision would trade a data defect for a dead flow.
    """

    entry = _entry_with_subentries()
    service = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Google Find Hub Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, SERVICE_SUBENTRY_KEY),
    )
    twin = _legacy_twin(
        entry.entry_id,
        subentry_type=SUBENTRY_TYPE_HUB,
        group_key=TRACKER_SUBENTRY_KEY,
    )
    assert twin.unique_id == f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}", (
        "the collision under test has to be the one the tracker create wants"
    )
    store = _subentry_store(entry)
    store[service.subentry_id] = service
    store[twin.subentry_id] = twin

    flow = _build_flow(entry)
    await _run_sync(flow, entry, _context_map_for(flow, entry, "migration"))

    assert _stored_visible(twin) == (), "the hub twin still holds no devices"
    tracker = _subentry_of_type(entry, SUBENTRY_TYPE_TRACKER)
    assert tracker is not None, "the tracker group is created despite the collision"
    assert tracker.unique_id == f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}", (
        "the canonical id belongs to the canonical group: five sites outside "
        "this flow reconcile against exactly that spelling"
    )
    assert twin.unique_id == f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}-legacy", (
        "the legacy holder is the one displaced, and deterministically so"
    )
    assert _stored_visible(tracker) == ("dev-1",)


@pytest.mark.asyncio
async def test_a_left_alone_twin_is_not_swept_up_as_a_stale_core_group() -> None:
    """The cleanup classified by stored key alone, which the type axis broke.

    Before the sync resolver read ``subentry_type``, a mis-keyed twin was always
    resolved by its stored key and therefore always ended up in ``context_map``,
    which kept it out of the stale list by accident. Once a real service
    subentry can win the exact-key match, the twin is left alone -- and
    ``async_remove_subentry`` would clear its device and entity registry
    bindings along with it.
    """

    entry = _entry_with_subentries()
    service = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Google Find Hub Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, SERVICE_SUBENTRY_KEY),
    )
    twin = _legacy_twin(
        entry.entry_id,
        subentry_type=SUBENTRY_TYPE_HUB,
        group_key=TRACKER_SUBENTRY_KEY,
    )
    store = _subentry_store(entry)
    store[service.subentry_id] = service
    store[twin.subentry_id] = twin

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "migration")
    await _run_sync(flow, entry, context_map)
    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert twin.subentry_id not in manager.removed, (
        "a mis-keyed twin is not a leftover copy of a core group"
    )
    assert twin.subentry_id in entry.subentries


@pytest.mark.asyncio
async def test_a_hub_losing_the_service_slot_is_not_swept_up_either() -> None:
    """The twin above is mis-keyed; this one stores the very key it loses.

    A ``hub`` writes ``SERVICE_SUBENTRY_KEY`` by design
    (``HubSubentryFlowHandler._group_key``), so beside a real service subentry
    it is not mis-keyed at all -- and the literal-owner rank makes it the
    *deterministic* loser of the resolver, where taking whichever came first
    used to leave the outcome open. Classifying by stored key alone therefore
    hands it to ``async_remove_subentry``, which clears its device and entity
    registry bindings: the silent deletion this change exists to prevent, only
    with the hub as the victim instead of the service group.

    The rule the cleanup needs is not "does the type name a *different* core
    key" but "does the type *literally own* the key it stores". Only the literal
    owner can be a leftover copy of that core group; every other type sitting on
    the key is a group of its own.
    """

    entry = _entry_with_subentries()
    service = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Google Find Hub Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, SERVICE_SUBENTRY_KEY),
    )
    hub = _legacy_twin(
        entry.entry_id,
        subentry_type=SUBENTRY_TYPE_HUB,
        group_key=SERVICE_SUBENTRY_KEY,
        unique_id=f"{entry.entry_id}-hub-legacy",
    )
    store = _subentry_store(entry)
    store[service.subentry_id] = service
    store[hub.subentry_id] = hub

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "migration")
    await _run_sync(flow, entry, context_map)

    assert context_map.get(SERVICE_SUBENTRY_KEY) == service.subentry_id, (
        "the premise is that the literal owner wins the service slot"
    )

    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert hub.subentry_id not in manager.removed, (
        "a hub that lost the service slot is a group of its own, not a leftover"
    )
    assert hub.subentry_id in entry.subentries


@pytest.mark.parametrize("path", ["migration", "reconfigure"])
@pytest.mark.asyncio
async def test_a_tracker_parked_on_the_service_key_is_not_swept_up(
    path: str,
) -> None:
    """The third non-owner cell: a ``tracker`` sitting on ``SERVICE_SUBENTRY_KEY``.

    The two tests above pin ``hub`` on either core key. This one pins the
    remaining combination, and it needs a third subentry to be reachable at all:
    beside a real ``service`` *alone*, the tracker-typed subentry is adopted
    into the tracker slot and re-keyed to ``TRACKER_SUBENTRY_KEY``, so it never
    reaches the cleanup on the service key. Only once a canonical
    ``tracker``/``TRACKER_SUBENTRY_KEY`` sibling wins that slot does the parked
    one stay behind on a core key it does not own.

    What is asserted is the ownership guard, not an endorsement of the parked
    state: the runtime index in ``coordinator/subentry.py`` still folds by
    stored key, so such a subentry keeps being indexed under the service key
    (see ``agents/config_flow/AGENTS.md``). That read side is untouched by this
    change -- byte-for-byte identical to ``b75bea42`` apart from comments -- and
    belongs to the alias/type axis. What changes here is only *reachability*:
    ``b75bea42`` resolved the collision by deleting one of the three (which of
    them depended on iteration order), taking its device and entity registry
    bindings along; leaving it in place is recoverable, deleting it is not.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    service = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Google Find Hub Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, SERVICE_SUBENTRY_KEY),
    )
    canonical_tracker = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core tracking",
        unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY),
    )
    parked = _legacy_twin(
        entry.entry_id,
        subentry_type=SUBENTRY_TYPE_TRACKER,
        group_key=SERVICE_SUBENTRY_KEY,
        visible_device_ids=("dev-parked",),
        unique_id=f"{entry.entry_id}-tracker-parked",
    )
    for subentry in (service, canonical_tracker, parked):
        store[subentry.subentry_id] = subentry

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, path)
    await _run_sync(flow, entry, context_map)

    assert context_map.get(SERVICE_SUBENTRY_KEY) == service.subentry_id, (
        "the premise is that the literal owner wins the service slot"
    )
    assert context_map.get(TRACKER_SUBENTRY_KEY) == canonical_tracker.subentry_id, (
        "and that the canonical sibling wins the tracker slot, "
        "which is what leaves the parked one behind"
    )

    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert parked.subentry_id not in manager.removed, (
        "a tracker on the service key is a group of its own, not a leftover copy "
        "of the service group"
    )
    assert parked.subentry_id in entry.subentries
    assert _stored_visible(parked) == ("dev-parked",), (
        "and its device assignment survives with it"
    )


@pytest.mark.asyncio
async def test_an_untyped_leftover_on_a_core_key_is_still_removed() -> None:
    """The guard's exclusion of untyped subentries is a decision, so it is pinned.

    ``_canonical_core_key_of`` treats a missing ``subentry_type`` as "the stored
    key keeps deciding", which makes such a subentry a candidate copy of the
    core group it stores rather than a group of its own. Sweeping stale legacy
    copies is what this pass exists for, so the ownership guard deliberately
    stops short of the untyped case.

    How reachable that case is, is *not* asserted here, and the distinction
    matters for what this test proves: ``ConfigSubentry.subentry_type`` is a
    required keyword-only ``str`` in the installed core, so ``None`` is a shape
    this module's mutable stub allows and the core's dataclass does not. The
    branch is therefore defensive, and this test pins the guard's boundary, not
    a migration path.

    Unchanged from ``b75bea42``: one of two same-keyed siblings dies there too.
    What the type axis changes is only *which* one.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    tracker = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core tracking",
        unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY),
    )
    untyped = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=None,
        title="Legacy core tracking",
        unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}-legacy",
        subentry_id="sub-untyped-legacy",
    )
    store[tracker.subentry_id] = tracker
    store[untyped.subentry_id] = untyped

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "migration")
    await _run_sync(flow, entry, context_map)

    assert context_map.get(TRACKER_SUBENTRY_KEY) == tracker.subentry_id, (
        "the premise is that the typed sibling wins the tracker slot"
    )

    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert untyped.subentry_id in manager.removed, (
        "an untyped leftover on a core key is a stale copy, not a foreign group"
    )


@pytest.mark.asyncio
async def test_a_genuinely_orphaned_core_group_is_still_removed() -> None:
    """The negative case above needs its positive counterpart, or it proves nothing.

    A guard that only ever refuses is indistinguishable from a guard that
    disabled the cleanup outright. This pins the direction the type axis must
    *not* have broken: a leftover subentry that stores a core key, whose type
    agrees with that key and which no longer appears in ``context_map``, is
    still swept up.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    duplicates = [
        ConfigSubentry(
            data=MappingProxyType(
                {"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}
            ),
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title=f"Core tracking {suffix}",
            unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}-{suffix}",
            subentry_id=f"sub-core-{suffix}",
        )
        for suffix in ("a", "b")
    ]
    for duplicate in duplicates:
        store[duplicate.subentry_id] = duplicate

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "migration")
    await _run_sync(flow, entry, context_map)

    claimed = set(context_map.values())
    orphans = [item for item in duplicates if item.subentry_id not in claimed]
    assert len(orphans) == 1, (
        "the premise is that the sync claims one of the two and orphans the other"
    )

    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert orphans[0].subentry_id in manager.removed, (
        "a core-keyed leftover whose type agrees with its key is still stale"
    )


@pytest.mark.asyncio
async def test_a_legacy_tracker_group_on_its_own_key_is_left_alone() -> None:
    """Several tracker groups with distinct keys are a supported shape.

    ``coordinator/subentry.py`` states this and leaves tracker subentries on
    their stored key. A first version of the type axis folded *every* tracker
    onto ``TRACKER_SUBENTRY_KEY``, so this per-account group was resolved as the
    core tracker group and had its key, title and identity overwritten: the axis
    meant to prevent a data defect producing one, one field level up. Only a
    tracker storing the *service* key is a genuine mis-key.

    Pinned on the **migration** path only, and that limit is a measurement, not
    an oversight. On the reconfigure path the same group is adopted at
    ``b75bea42`` too, i.e. before this axis existed, because
    ``ConfigEntrySubEntryManager`` keys every tracker-typed subentry under
    ``TRACKER_SUBENTRY_KEY`` and the seeder receives it already renamed. That
    fold is a separate defect with its own blast radius (runtime index, manager
    adoption) and is carried as ``B13`` in the remainder register of
    ``agents/config_flow/AGENTS.md``, owned by
    ``PLAN_GFMY_SUBENTRY_TYPE_MIGRATION``. Parametrising
    this test over both paths would assert a fix this change does not make.
    """

    path = "migration"

    entry = _entry_with_subentries()
    legacy = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": "owner@example.com",
                "feature_flags": {},
                "visible_device_ids": ["dev-legacy"],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Buero Tracker",
        unique_id=f"{entry.entry_id}-owner@example.com",
        subentry_id=_stable_subentry_id(entry.entry_id, "legacy-account-group"),
    )
    _subentry_store(entry)[legacy.subentry_id] = legacy

    flow = _build_flow(entry)
    await _run_sync(flow, entry, _context_map_for(flow, entry, path))

    assert dict(legacy.data)["group_key"] == "owner@example.com", (
        "a tracker group on its own key is not the core tracking group"
    )
    assert legacy.title == "Buero Tracker", "its title is the user's, not the sync's"
    assert _stored_visible(legacy) == ("dev-legacy",), (
        "and it keeps the devices assigned to it"
    )


@pytest.mark.parametrize("order", [("hub", "svc"), ("svc", "hub")])
@pytest.mark.asyncio
async def test_the_canonical_service_group_survives_a_hub_claiming_its_key(
    order: tuple[str, str],
) -> None:
    """A literal ``service`` type beats a ``hub`` that merely folds onto the key.

    ``HubSubentryFlowHandler._group_key`` is ``SERVICE_SUBENTRY_KEY``, so this
    module itself produces a ``hub`` storing the service key. With a real
    ``service`` subentry beside it, both reach the exact-match exit. Taking
    whichever came first made the outcome depend on ``entry.subentries``
    iteration order, and in one of the two orders it turned the ``AbortFlow`` of
    the previous commit into a silent ``async_remove_subentry`` of the canonical
    service group, registry bindings included.

    Both orders are pinned, because a single order would pass on the very
    implementation that decides by insertion order.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    made = {
        "hub": ConfigSubentry(
            data=MappingProxyType(
                {"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}
            ),
            subentry_type=SUBENTRY_TYPE_HUB,
            title="My Hub",
            unique_id=f"{entry.entry_id}-hub-custom",
            subentry_id="sub-hub",
        ),
        "svc": ConfigSubentry(
            data=MappingProxyType(
                {"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}
            ),
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Google Find Hub Service",
            unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
            subentry_id="sub-service",
        ),
    }
    for name in order:
        store[made[name].subentry_id] = made[name]

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "reconfigure")
    await _run_sync(flow, entry, context_map)
    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert "sub-service" not in manager.removed, (
        "the canonical service group must never be the one removed"
    )
    assert made["svc"].unique_id == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}", (
        "and it keeps the canonical identity, whatever the insert order"
    )


@pytest.mark.parametrize("order", [("hub", "svc"), ("svc", "hub")])
@pytest.mark.asyncio
async def test_a_seed_naming_a_non_owner_still_loses_to_the_literal_owner(
    order: tuple[str, str],
) -> None:
    """The pool's rank outranks the seed, asserted on the one reachable route.

    This is the same shape as the test above, with one difference that is the
    whole point: the context map is written *directly* rather than resolved
    through ``ConfigEntrySubEntryManager``. Both matter, and they stopped being
    the same assertion once the manager gained a rank axis.

    Through the manager the shape is no longer reachable: for
    ``SERVICE_SUBENTRY_KEY`` both candidates store the key exactly, the literal
    owner wins ``_candidate_score``'s type field, and the seed the manager
    hands over is therefore the ``service`` subentry. The two tests above kept
    asserting their outcomes and stopped exercising the ordering that produces
    them -- measured by putting the seed in front of the pool, which failed
    both before the rank existed and neither after.

    A context map is not only written by that resolver, though. It survives
    between the steps of one flow (``_ensure_subentry_context``), so an entry
    whose shape changed mid-flow -- a hub created, then a service subentry
    repaired beside it -- carries a seed the manager would no longer choose.
    Guarding the ordering therefore means writing the seed the way that route
    does, and this test is the guard the manager's rank cannot make redundant.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    made = {
        "hub": ConfigSubentry(
            data=MappingProxyType(
                {"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}
            ),
            subentry_type=SUBENTRY_TYPE_HUB,
            title="My Hub",
            unique_id=f"{entry.entry_id}-hub-custom",
            subentry_id="sub-hub",
        ),
        "svc": ConfigSubentry(
            data=MappingProxyType(
                {"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}
            ),
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Google Find Hub Service",
            unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
            subentry_id="sub-service",
        ),
    }
    for name in order:
        store[made[name].subentry_id] = made[name]

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "reconfigure")
    # The seed the manager would never hand over any more. Asserted first so a
    # future change that makes the resolver overwrite it fails here rather than
    # turning this test into a duplicate of the one above.
    context_map[SERVICE_SUBENTRY_KEY] = "sub-hub"
    await _run_sync(flow, entry, context_map)
    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert "sub-service" not in manager.removed, (
        "the literal owner must survive a seed naming the hub"
    )
    assert made["svc"].unique_id == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}", (
        "and keep the canonical identity, whatever the insert order"
    )


@pytest.mark.parametrize("order", [("legacy", "canonical"), ("canonical", "legacy")])
@pytest.mark.asyncio
async def test_a_seed_that_does_not_carry_the_key_yields_to_the_stored_match(
    order: tuple[str, str],
) -> None:
    """A seeded subentry that answers for a key it does not carry must not win.

    The reconfigure seed is resolved by ``ConfigEntrySubEntryManager``, which
    folds *every* tracker-typed subentry onto ``TRACKER_SUBENTRY_KEY``, so it
    can name a legacy per-account group while the canonical core group sits
    right beside it. That legacy group is in neither pool here: it stores a
    foreign key and ``_canonical_core_key_of`` folds a tracker only off the
    *service* key. Letting the seed outrank a pool that does not contain it
    made the sync rewrite the legacy group onto the core key, displaced the
    canonical group's identity through ``_claim_unique_id`` and then swept it,
    device and entity registry bindings included -- where the previous commit
    raised ``AbortFlow`` and left both groups intact.

    **This fixture seeds through a live manager**, and that matters for what it
    still proves. ``_reset_reconfigure_subentry_context`` instantiates
    ``ConfigEntrySubEntryManager`` and reads ``managed_subentries``, so once
    that manager gained a rank axis (exact stored key, then literal owner) it
    stopped nominating the legacy group *in this shape*: both are
    ``tracker``-typed and fold onto the same key, so the canonical one wins on
    the exact-key field. The pool ordering asserted here is therefore no longer
    exercised through the seed by this fixture, which is stated rather than
    left to be rediscovered -- the assertions below still hold, but one of the
    two ways of reaching them closed.

    Measured rather than argued: putting the seed in front of the pool
    (``if seeded is not None: return seeded``) failed this test and
    ``::test_the_canonical_service_group_survives_a_hub_claiming_its_key``
    before the manager gained its rank, and fails neither afterwards. Both lost
    their access to the ordering by the same mechanism, so the guard was
    restored explicitly rather than left to be inferred:
    ``::test_a_seed_naming_a_non_owner_still_loses_to_the_literal_owner``
    injects the seed into the context map directly instead of resolving it
    through the manager, which is the one route the manager's rank cannot
    close.

    Both orders are pinned because the defect only showed in one of them, and a
    single order would pass on the implementation that decides by insertion
    order.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    made = {
        "legacy": ConfigSubentry(
            data=MappingProxyType(
                {
                    "group_key": "owner@example.com",
                    "feature_flags": {},
                    "visible_device_ids": ["dev-legacy"],
                }
            ),
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title="Buero Tracker",
            unique_id=f"{entry.entry_id}-owner@example.com",
            subentry_id="sub-aaa-legacy",
        ),
        "canonical": ConfigSubentry(
            data=MappingProxyType(
                {
                    "group_key": TRACKER_SUBENTRY_KEY,
                    "feature_flags": {},
                    "visible_device_ids": ["dev-1"],
                }
            ),
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title="Google Find My devices",
            unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
            subentry_id="sub-zzz-canonical",
        ),
    }
    for name in order:
        store[made[name].subentry_id] = made[name]
    store["sub-service"] = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Google Find Hub Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id="sub-service",
    )

    flow = _build_flow(entry)
    context_map = _context_map_for(flow, entry, "reconfigure")
    await _run_sync(flow, entry, context_map)
    await flow._async_cleanup_stale_subentries(entry, context_map)  # type: ignore[attr-defined]

    manager = flow.hass.config_entries  # type: ignore[attr-defined]
    assert "sub-zzz-canonical" not in manager.removed, (
        "the canonical core group must never be the one removed"
    )
    assert made["canonical"].unique_id == f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}", (
        "it keeps the canonical identity the five reconciling sites build"
    )
    assert _stored_visible(made["canonical"]) == ("dev-1",), (
        "and the devices assigned to it survive, whatever the insert order"
    )
    assert dict(made["legacy"].data)["group_key"] == "owner@example.com", (
        "while the legacy group stays on its own key, unadopted"
    )


@pytest.mark.asyncio
async def test_a_seed_still_decides_between_candidates_of_equal_rank() -> None:
    """Among equals the seeded subentry wins, and that is not decoration.

    The seed lost its precedence *in front of* the pool because it can name a
    subentry that does not carry the key at all. Inside the pool it still ranks,
    and it has to: a map written by an earlier step of the same flow names the
    group that step wrote, and dropping to the lowest ``subentry_id`` instead
    would re-home the group between two steps of one flow -- the stability the
    map exists for.

    Two same-typed twins on the core key are equal under both other ranks, so
    only the seed can decide, and the map deliberately names the *higher* id:
    without the tie-break the arbitrary ``subentry_id`` order would answer, and
    this assertion would pass on a rule that is not there.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    for subentry_id, unique_suffix in (
        ("sub-aaa-twin", f"{TRACKER_SUBENTRY_KEY}-alt"),
        ("sub-zzz-seeded", TRACKER_SUBENTRY_KEY),
    ):
        store[subentry_id] = ConfigSubentry(
            data=MappingProxyType(
                {"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}
            ),
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title="Google Find My devices",
            unique_id=f"{entry.entry_id}-{unique_suffix}",
            subentry_id=subentry_id,
        )

    flow = _build_flow(entry)
    context_map: dict[str, str | None] = {
        TRACKER_SUBENTRY_KEY: "sub-zzz-seeded",
        SERVICE_SUBENTRY_KEY: None,
    }
    await _run_sync(flow, entry, context_map)

    assert context_map[TRACKER_SUBENTRY_KEY] == "sub-zzz-seeded", (
        "the seeded twin keeps the slot, not the lexicographically first one"
    )


@pytest.mark.asyncio
async def test_an_exact_stored_key_match_beats_a_folded_literal_owner() -> None:
    """Exactness outranks the literal-owner rank, and that order is deliberate.

    The two rules can disagree: a ``hub`` storing the service key is an exact
    match but not the literal owner, while a ``service``-typed subentry on an
    email-style key is the literal owner but only folds onto it. Ranking the
    literal owner first would rewrite the key, title and identity of a stored
    subentry nobody asked to change, which is the very cost the exact-match
    preference exists to avoid. The pool is therefore ``exact or folded``, not
    ``exact + folded``, and this test is what keeps that from being decoration.
    """

    entry = _entry_with_subentries()
    store = _subentry_store(entry)
    exact_hub = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_HUB,
        title="My Hub",
        unique_id=f"{entry.entry_id}-hub-custom",
        subentry_id="sub-hub",
    )
    folded_service = ConfigSubentry(
        data=MappingProxyType({"group_key": "owner@example.com", "feature_flags": {}}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Buero Service",
        unique_id=f"{entry.entry_id}-owner@example.com",
        subentry_id="sub-aaa-folded",
    )
    store[exact_hub.subentry_id] = exact_hub
    store[folded_service.subentry_id] = folded_service

    flow = _build_flow(entry)
    await _run_sync(flow, entry, _context_map_for(flow, entry, "migration"))

    assert dict(exact_hub.data)["group_key"] == SERVICE_SUBENTRY_KEY, (
        "the exact match is the one written, despite not being the literal owner"
    )
    assert dict(folded_service.data)["group_key"] == "owner@example.com", (
        "and the folded literal owner keeps the stored identity it had"
    )
    assert folded_service.title == "Buero Service"


@pytest.mark.asyncio
async def test_two_folded_twins_resolve_deterministically() -> None:
    """With no exact match, the iteration order must not pick the winner.

    Two non-device twins both storing ``core_tracking`` fold onto the service
    key. Whichever the scan returns inherits the canonical identity while the
    other is left where it stands, so taking "the first" would make a
    user-visible outcome depend on dict ordering. No ``-legacy`` displacement
    happens here, deliberately stated because an earlier version of this
    docstring claimed one: ``_claim_unique_id`` only renames a subentry that
    holds the *desired* unique id, and two folded twins on their own ids hold
    neither. What the loser actually keeps is its key, its title and its id.
    The lowest ``subentry_id`` wins instead; the value is arbitrary, the
    stability is the point.
    """

    async def _winner_for(order: tuple[str, str]) -> str | None:
        entry = _entry_with_subentries()
        store = _subentry_store(entry)
        twins = {
            suffix: ConfigSubentry(
                data=MappingProxyType(
                    {"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}
                ),
                subentry_type=SUBENTRY_TYPE_HUB,
                title=f"Legacy hub {suffix}",
                unique_id=f"{entry.entry_id}-hub-{suffix}",
                subentry_id=f"sub-{suffix}",
            )
            for suffix in ("aaa", "zzz")
        }
        for suffix in order:
            store[twins[suffix].subentry_id] = twins[suffix]

        flow = _build_flow(entry)
        await _run_sync(flow, entry, _context_map_for(flow, entry, "migration"))

        canonical = f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"
        holders = [
            subentry_id
            for subentry_id, twin in twins.items()
            if getattr(twin, "unique_id", None) == canonical
        ]
        return holders[0] if len(holders) == 1 else None

    first = await _winner_for(("aaa", "zzz"))
    second = await _winner_for(("zzz", "aaa"))
    assert first == second == "aaa", (
        "the folded winner is the lowest subentry_id, whatever the insert order"
    )
