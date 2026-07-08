# tests/test_token_refresh.py
"""Unit tests for token regeneration functionality.

Tests cover:
- Cooldown mechanism (scoped per entry and per token type)
- AAS token regeneration
- ADM token regeneration
- Token dependency handling (ADM depends on AAS)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.googlefindmy.const import TOKEN_REFRESH_COOLDOWN_S


class _MockTokenCache:
    """Minimal TokenCache mock for token refresh tests."""

    def __init__(self, entry_id: str = "test-entry") -> None:
        self.entry_id = entry_id
        self._namespace = entry_id
        self._data: dict[str, Any] = {}
        self.set_calls: list[tuple[str, Any]] = []

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.set_calls.append((key, value))


class TestCooldownMechanism:
    """Test the cooldown mechanism for token refresh operations."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            clear_all_cooldowns,
        )

        clear_all_cooldowns()

    def test_cooldown_not_active_initially(self) -> None:
        """Token refresh should not be on cooldown initially."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            is_refresh_on_cooldown,
        )

        on_cooldown, remaining = is_refresh_on_cooldown("test-entry")
        assert not on_cooldown
        assert remaining == 0.0

    def test_cooldown_remaining_initially_zero(self) -> None:
        """Cooldown remaining should be zero initially."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            get_cooldown_remaining,
        )

        remaining = get_cooldown_remaining("test-entry")
        assert remaining == 0.0

    def test_cooldown_recorded_after_refresh(self) -> None:
        """Cooldown should be active after recording a refresh."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            is_refresh_on_cooldown,
        )

        _record_refresh("test-entry")

        on_cooldown, remaining = is_refresh_on_cooldown("test-entry")
        assert on_cooldown
        assert remaining > 0
        assert remaining <= TOKEN_REFRESH_COOLDOWN_S

    def test_cooldown_isolates_entries(self) -> None:
        """Cooldown should be isolated per config entry."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            is_refresh_on_cooldown,
        )

        _record_refresh("entry-1")

        on_cooldown_1, _ = is_refresh_on_cooldown("entry-1")
        on_cooldown_2, _ = is_refresh_on_cooldown("entry-2")

        assert on_cooldown_1
        assert not on_cooldown_2

    def test_clear_cooldown_works(self) -> None:
        """Clearing cooldown should allow immediate refresh."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            clear_cooldown,
            is_refresh_on_cooldown,
        )

        _record_refresh("test-entry")
        assert is_refresh_on_cooldown("test-entry")[0]

        clear_cooldown("test-entry")
        assert not is_refresh_on_cooldown("test-entry")[0]

    def test_clear_all_cooldowns(self) -> None:
        """Clearing all cooldowns should reset all entries."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            clear_all_cooldowns,
            is_refresh_on_cooldown,
        )

        _record_refresh("entry-1")
        _record_refresh("entry-2")

        clear_all_cooldowns()

        assert not is_refresh_on_cooldown("entry-1")[0]
        assert not is_refresh_on_cooldown("entry-2")[0]


class _MockHass:
    """Minimal Home Assistant mock for FCM token refresh tests."""

    def __init__(self, entry_id: str = "test-entry") -> None:
        self._entry_id = entry_id
        self._receiver = AsyncMock()
        self._receiver.async_reregister_fcm = AsyncMock(return_value=True)
        from custom_components.googlefindmy.const import DOMAIN

        self.data: dict[str, Any] = {
            DOMAIN: {
                "fcm_receivers": {entry_id: self._receiver},
            }
        }


class TestFcmTokenRegeneration:
    """Test FCM token regeneration."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            clear_all_cooldowns,
        )

        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_fcm_regeneration_calls_receiver(self) -> None:
        """FCM regeneration should call the FCM receiver to re-register."""
        hass = _MockHass("test-entry")

        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_fcm_token,
        )

        result = await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")

        assert result is True
        hass._receiver.async_reregister_fcm.assert_called_once_with("test-entry")

    @pytest.mark.asyncio
    async def test_fcm_regeneration_blocked_by_cooldown(self) -> None:
        """FCM regeneration should be blocked when on cooldown."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            async_regenerate_fcm_token,
        )

        hass = _MockHass("test-entry")
        _record_refresh("test-entry", "fcm")

        result = await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")

        assert result is False

    @pytest.mark.asyncio
    async def test_fcm_regeneration_records_cooldown(self) -> None:
        """Successful FCM regeneration should record cooldown."""
        hass = _MockHass("test-entry")

        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_fcm_token,
            is_refresh_on_cooldown,
        )

        await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")

        on_cooldown, _ = is_refresh_on_cooldown("test-entry", "fcm")
        assert on_cooldown

    @pytest.mark.asyncio
    async def test_fcm_regeneration_returns_false_when_receiver_fails(self) -> None:
        """FCM regeneration should return False if receiver returns False."""
        hass = _MockHass("test-entry")
        hass._receiver.async_reregister_fcm = AsyncMock(return_value=False)

        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_fcm_token,
        )

        result = await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")

        assert result is False

    @pytest.mark.asyncio
    async def test_fcm_regeneration_handles_exception(self) -> None:
        """FCM regeneration should return False on exception."""
        hass = _MockHass("test-entry")
        hass._receiver.async_reregister_fcm = AsyncMock(
            side_effect=Exception("FCM registration failed")
        )

        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_fcm_token,
        )

        result = await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")

        assert result is False


class TestAdmTokenRegeneration:
    """Test ADM token regeneration."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            clear_all_cooldowns,
        )

        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_adm_regeneration_invalidates_adm_only(self) -> None:
        """ADM regeneration should only invalidate ADM token, not AAS."""
        cache = _MockTokenCache("test-entry")
        cache._data["aas_token"] = "aas-token"
        cache._data["adm_token_test@example.com"] = "old-adm-token"

        with (
            patch(
                "custom_components.googlefindmy.Auth.adm_token_retrieval.async_get_adm_token",
                new_callable=AsyncMock,
                return_value="new-adm-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_adm_token,
            )

            result = await async_regenerate_adm_token(cache=cache)

        assert result is True
        # Check that only ADM was invalidated
        invalidated_keys = [k for k, v in cache.set_calls if v is None]
        assert "adm_token_test@example.com" in invalidated_keys
        assert "aas_token" not in invalidated_keys

    @pytest.mark.asyncio
    async def test_adm_regeneration_blocked_by_cooldown(self) -> None:
        """ADM regeneration should be blocked when on cooldown."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            async_regenerate_adm_token,
        )

        cache = _MockTokenCache("test-entry")
        _record_refresh("test-entry", "adm")

        # Should not call the actual token generation - the function returns early
        result = await async_regenerate_adm_token(cache=cache)

        assert result is False

    @pytest.mark.asyncio
    async def test_adm_regeneration_fails_without_username(self) -> None:
        """ADM regeneration should fail if no username is available."""
        cache = _MockTokenCache("test-entry")

        with patch(
            "custom_components.googlefindmy.Auth.username_provider.async_get_username",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_adm_token,
            )

            result = await async_regenerate_adm_token(cache=cache)

        assert result is False

    @pytest.mark.asyncio
    async def test_adm_regeneration_records_cooldown(self) -> None:
        """Successful ADM regeneration should record cooldown."""
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.adm_token_retrieval.async_get_adm_token",
                new_callable=AsyncMock,
                return_value="new-adm-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_adm_token,
                is_refresh_on_cooldown,
            )

            await async_regenerate_adm_token(cache=cache)

        on_cooldown, _ = is_refresh_on_cooldown("test-entry", "adm")
        assert on_cooldown


class TestScopedCooldown:
    """Cooldown is scoped per token type: FCM and ADM refresh independently."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            clear_all_cooldowns,
        )

        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_fcm_refresh_does_not_block_adm_refresh(self) -> None:
        """FCM refresh must NOT block a subsequent ADM refresh (per-type cooldown)."""
        hass = _MockHass("test-entry")

        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_fcm_token,
        )

        result = await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")
        assert result is True

        # ADM must remain available despite the FCM cooldown (decoupled).
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.adm_token_retrieval.async_get_adm_token",
                new_callable=AsyncMock,
                return_value="new-adm-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_adm_token,
            )

            result = await async_regenerate_adm_token(cache=cache)

        assert result is True

    @pytest.mark.asyncio
    async def test_adm_refresh_does_not_block_fcm_refresh(self) -> None:
        """ADM refresh must NOT block a subsequent FCM refresh (per-type cooldown)."""
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.adm_token_retrieval.async_get_adm_token",
                new_callable=AsyncMock,
                return_value="new-adm-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_adm_token,
            )

            result = await async_regenerate_adm_token(cache=cache)
            assert result is True

        # FCM must remain available despite the ADM cooldown (decoupled).
        hass = _MockHass("test-entry")

        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_fcm_token,
        )

        result = await async_regenerate_fcm_token(hass=hass, entry_id="test-entry")

        assert result is True


class TestEmailMasking:
    """Test email masking for privacy in logs."""

    def test_mask_email_normal(self) -> None:
        """Normal email should be masked."""
        from custom_components.googlefindmy.Auth.token_refresh import _mask_email

        assert _mask_email("user@example.com") == "u***@example.com"

    def test_mask_email_short_local(self) -> None:
        """Single character local part should be masked."""
        from custom_components.googlefindmy.Auth.token_refresh import _mask_email

        assert _mask_email("a@example.com") == "*@example.com"

    def test_mask_email_none(self) -> None:
        """None should return unknown."""
        from custom_components.googlefindmy.Auth.token_refresh import _mask_email

        assert _mask_email(None) == "<unknown>"

    def test_mask_email_no_at(self) -> None:
        """Email without @ should return unknown."""
        from custom_components.googlefindmy.Auth.token_refresh import _mask_email

        assert _mask_email("invalid") == "<unknown>"

    def test_mask_email_empty_local(self) -> None:
        """Empty local part should be handled."""
        from custom_components.googlefindmy.Auth.token_refresh import _mask_email

        assert _mask_email("@example.com") == "*@example.com"
