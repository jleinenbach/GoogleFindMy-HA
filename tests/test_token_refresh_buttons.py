# tests/test_token_refresh_buttons.py
"""Unit tests for token regeneration button entities.

Tests cover:
- Button entity creation and unique IDs
- Button availability based on cooldown
- Button press behavior
- Entity descriptions and attributes
- Disabled by default behavior
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant

from custom_components.googlefindmy import button
from custom_components.googlefindmy.Auth.token_refresh import clear_all_cooldowns
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    TOKEN_REFRESH_COOLDOWN_S,
)


class _ConfigEntryStub:
    """Minimal config entry stub for button setup tests."""

    def __init__(self) -> None:
        self.entry_id = "entry-token-btn"
        self.data: dict[str, Any] = {
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        }
        self.options: dict[str, Any] = {}
        self.runtime_data: Any | None = None
        self.subentries: dict[str, Any] = {}
        self._unload_callbacks: list[Any] = []

    def async_on_unload(self, callback: Any) -> None:
        self._unload_callbacks.append(callback)


class _MockTokenCache:
    """Mock TokenCache for button tests."""

    def __init__(self, entry_id: str = "entry-token-btn") -> None:
        self.entry_id = entry_id
        self._namespace = entry_id
        self._data: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value


def _make_hass(loop: asyncio.AbstractEventLoop) -> HomeAssistant:
    hass = HomeAssistant()
    hass.loop = loop
    hass.data = {"core.uuid": "test-instance"}
    hass.bus = SimpleNamespace(
        async_listen=lambda *_args, **_kwargs: (lambda: None),
        async_listen_once=lambda *_args, **_kwargs: (lambda: None),
    )
    hass.async_create_task = lambda coro, *, name=None: loop.create_task(
        coro, name=name
    )
    hass.async_run_hass_job = lambda job, *args: getattr(
        job, "target", lambda *_: None
    )(*args)
    hass.verify_event_loop_thread = lambda *_args, **_kwargs: None
    return hass


def _make_add_entities(hass: HomeAssistant, loop: asyncio.AbstractEventLoop):
    added: list[tuple[Any, str | None]] = []
    pending: list[asyncio.Task[Any]] = []

    def _async_add_entities(entities: list[Any], **kwargs: Any) -> None:
        config_subentry_id = kwargs.get("config_subentry_id")
        for entity in entities:
            entity.hass = hass
            added.append((entity, config_subentry_id))
            if hasattr(entity, "async_added_to_hass"):
                pending.append(loop.create_task(entity.async_added_to_hass()))

    return _async_add_entities, added, pending


class TestTokenRefreshButtonSetup:
    """Test token refresh button entity setup."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_token_buttons_created_for_service_subentry(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Token refresh buttons should be created under service subentry."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find token refresh buttons
        token_buttons = [
            (entity, config)
            for entity, config in added
            if "regenerate" in (getattr(entity, "unique_id", "") or "")
        ]

        # Should have 2 token buttons (AAS and ADM)
        assert len(token_buttons) == 2

        unique_ids = {entity.unique_id for entity, _ in token_buttons}
        assert any("regenerate_fcm_token" in uid for uid in unique_ids)
        assert any("regenerate_adm_token" in uid for uid in unique_ids)

    @pytest.mark.asyncio
    async def test_token_buttons_disabled_by_default(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Token refresh buttons should be disabled by default."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find token refresh buttons
        token_buttons = [
            entity
            for entity, _ in added
            if "regenerate" in (getattr(entity, "unique_id", "") or "")
        ]

        for btn in token_buttons:
            assert btn._attr_entity_registry_enabled_default is False


class TestTokenRefreshButtonAttributes:
    """Test token refresh button attributes and state."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_button_available_when_no_cooldown(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Button should be available when not on cooldown."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find FCM token button
        fcm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_fcm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert fcm_button is not None
        assert fcm_button.available is True

    @pytest.mark.asyncio
    async def test_button_unavailable_when_on_cooldown(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Button should be unavailable when on cooldown."""
        from custom_components.googlefindmy.Auth.token_refresh import _record_refresh

        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Record cooldown for the FCM token type (per-type scoping)
        _record_refresh(entry.entry_id, "fcm")

        # Find FCM token button
        fcm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_fcm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert fcm_button is not None
        assert fcm_button.available is False

    @pytest.mark.asyncio
    async def test_fcm_cooldown_does_not_disable_adm_button(
        self, stub_coordinator_factory: Any
    ) -> None:
        """A recorded FCM cooldown must not disable the ADM button.

        Regression guard for the per-token-type cooldown scoping: an FCM
        refresh previously flipped the sibling ADM button to ``unavailable``,
        which surfaced in the logbook as an unintended ADM press.
        """
        from custom_components.googlefindmy.Auth.token_refresh import _record_refresh

        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Record a cooldown for the FCM token type only.
        _record_refresh(entry.entry_id, "fcm")

        fcm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_fcm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )
        adm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_adm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert fcm_button is not None
        assert adm_button is not None
        # Same-type button is rate-limited...
        assert fcm_button.available is False
        # ...but the sibling ADM button stays available (decoupled cooldown).
        assert adm_button.available is True

    @pytest.mark.asyncio
    async def test_button_extra_state_attributes(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Button should expose cooldown information in extra state attributes."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find FCM token button
        fcm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_fcm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert fcm_button is not None
        attrs = fcm_button.extra_state_attributes

        assert "cooldown_seconds" in attrs
        assert attrs["cooldown_seconds"] == TOKEN_REFRESH_COOLDOWN_S


class TestTokenRefreshButtonPress:
    """Test token refresh button press behavior."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_fcm_button_press_calls_regenerate(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Pressing FCM button should call the regeneration function."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find FCM token button
        fcm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_fcm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert fcm_button is not None

        # Mock the regeneration function - patch where it's used inside the method
        with patch(
            "custom_components.googlefindmy.Auth.token_refresh.async_regenerate_fcm_token",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_regenerate:
            await fcm_button.async_press()

        mock_regenerate.assert_called_once()

    @pytest.mark.asyncio
    async def test_adm_button_press_calls_regenerate(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Pressing ADM button should call the regeneration function."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find ADM token button
        adm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_adm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert adm_button is not None

        # Mock the regeneration function - patch where it's used inside the method
        with patch(
            "custom_components.googlefindmy.Auth.token_refresh.async_regenerate_adm_token",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_regenerate:
            await adm_button.async_press()

        mock_regenerate.assert_called_once()

    @pytest.mark.asyncio
    async def test_button_press_blocked_when_on_cooldown(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Button press should not call regeneration when on cooldown."""
        from custom_components.googlefindmy.Auth.token_refresh import _record_refresh

        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Record cooldown before pressing (per-type scoping: match FCM button)
        _record_refresh(entry.entry_id, "fcm")

        # Find FCM token button
        fcm_button = next(
            (
                entity
                for entity, _ in added
                if "regenerate_fcm_token" in (getattr(entity, "unique_id", "") or "")
            ),
            None,
        )

        assert fcm_button is not None
        assert fcm_button.available is False

        # Mock the regeneration function - should NOT be called
        with patch(
            "custom_components.googlefindmy.Auth.token_refresh.async_regenerate_fcm_token",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_regenerate:
            await fcm_button.async_press()

        mock_regenerate.assert_not_called()


class TestTokenRefreshButtonUniqueIds:
    """Test token refresh button unique ID generation."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_unique_ids_include_entry_and_subentry(
        self, stub_coordinator_factory: Any
    ) -> None:
        """Unique IDs should include entry_id and subentry identifier."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find token buttons
        token_buttons = [
            entity
            for entity, _ in added
            if "regenerate" in (getattr(entity, "unique_id", "") or "")
        ]

        for btn in token_buttons:
            uid = btn.unique_id
            assert entry.entry_id in uid
            assert DOMAIN in uid

    @pytest.mark.asyncio
    async def test_unique_ids_are_different_for_each_button(
        self, stub_coordinator_factory: Any
    ) -> None:
        """AAS and ADM buttons should have different unique IDs."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        entry = _ConfigEntryStub()
        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("button",)},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            subentry_id="service-subentry",
        )
        entry.subentries = {service_subentry.subentry_id: service_subentry}

        coordinator_cls = stub_coordinator_factory(
            subentry_key="tracker",
            service_subentry_key=SERVICE_SUBENTRY_KEY,
        )
        coordinator = coordinator_cls(
            hass, cache=SimpleNamespace(entry_id=entry.entry_id)
        )
        coordinator.config_entry = entry

        token_cache = _MockTokenCache(entry.entry_id)
        entry.runtime_data = SimpleNamespace(
            coordinator=coordinator,
            token_cache=token_cache,
        )

        add_entities, added, pending = _make_add_entities(hass, loop)

        await button.async_setup_entry(hass, entry, add_entities)
        await asyncio.gather(*pending)

        # Find token buttons
        token_buttons = [
            entity
            for entity, _ in added
            if "regenerate" in (getattr(entity, "unique_id", "") or "")
        ]

        unique_ids = [btn.unique_id for btn in token_buttons]
        # All unique IDs should be different
        assert len(unique_ids) == len(set(unique_ids))
