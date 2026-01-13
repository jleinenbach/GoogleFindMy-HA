# tests/test_token_refresh.py
"""Unit tests for token regeneration functionality.

Tests cover:
- Cooldown mechanism (shared across all token refresh operations)
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


class TestAasTokenRegeneration:
    """Test AAS token regeneration."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            clear_all_cooldowns,
        )

        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_aas_regeneration_invalidates_tokens(self) -> None:
        """AAS regeneration should invalidate both AAS and ADM tokens."""
        cache = _MockTokenCache("test-entry")
        cache._data["aas_token"] = "old-aas-token"
        cache._data["adm_token_test@example.com"] = "old-adm-token"

        with (
            patch(
                "custom_components.googlefindmy.Auth.aas_token_retrieval.async_get_aas_token",
                new_callable=AsyncMock,
                return_value="new-aas-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_aas_token,
            )

            result = await async_regenerate_aas_token(cache=cache)

        assert result is True
        # Check that both tokens were invalidated
        invalidated_keys = [k for k, v in cache.set_calls if v is None]
        assert "aas_token" in invalidated_keys
        assert "adm_token_test@example.com" in invalidated_keys

    @pytest.mark.asyncio
    async def test_aas_regeneration_blocked_by_cooldown(self) -> None:
        """AAS regeneration should be blocked when on cooldown."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            _record_refresh,
            async_regenerate_aas_token,
        )

        cache = _MockTokenCache("test-entry")
        _record_refresh("test-entry")

        # Should not call the actual token generation - the function returns early
        result = await async_regenerate_aas_token(cache=cache)

        assert result is False

    @pytest.mark.asyncio
    async def test_aas_regeneration_records_cooldown(self) -> None:
        """Successful AAS regeneration should record cooldown."""
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.aas_token_retrieval.async_get_aas_token",
                new_callable=AsyncMock,
                return_value="new-aas-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_aas_token,
                is_refresh_on_cooldown,
            )

            await async_regenerate_aas_token(cache=cache)

        on_cooldown, _ = is_refresh_on_cooldown("test-entry")
        assert on_cooldown

    @pytest.mark.asyncio
    async def test_aas_regeneration_returns_false_on_empty_token(self) -> None:
        """AAS regeneration should return False if token generation returns empty."""
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.aas_token_retrieval.async_get_aas_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_aas_token,
            )

            result = await async_regenerate_aas_token(cache=cache)

        assert result is False

    @pytest.mark.asyncio
    async def test_aas_regeneration_handles_exception(self) -> None:
        """AAS regeneration should return False on exception."""
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.aas_token_retrieval.async_get_aas_token",
                new_callable=AsyncMock,
                side_effect=Exception("Token generation failed"),
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_aas_token,
            )

            result = await async_regenerate_aas_token(cache=cache)

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
        _record_refresh("test-entry")

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

        on_cooldown, _ = is_refresh_on_cooldown("test-entry")
        assert on_cooldown


class TestSharedCooldown:
    """Test that cooldown is shared between AAS and ADM token refresh."""

    def setup_method(self) -> None:
        """Clear cooldowns before each test."""
        from custom_components.googlefindmy.Auth.token_refresh import (
            clear_all_cooldowns,
        )

        clear_all_cooldowns()

    @pytest.mark.asyncio
    async def test_aas_refresh_blocks_adm_refresh(self) -> None:
        """AAS refresh should block subsequent ADM refresh."""
        cache = _MockTokenCache("test-entry")

        with (
            patch(
                "custom_components.googlefindmy.Auth.aas_token_retrieval.async_get_aas_token",
                new_callable=AsyncMock,
                return_value="new-aas-token",
            ),
            patch(
                "custom_components.googlefindmy.Auth.username_provider.async_get_username",
                new_callable=AsyncMock,
                return_value="test@example.com",
            ),
        ):
            from custom_components.googlefindmy.Auth.token_refresh import (
                async_regenerate_aas_token,
            )

            result = await async_regenerate_aas_token(cache=cache)
            assert result is True

        # Now ADM should be blocked
        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_adm_token,
        )

        result = await async_regenerate_adm_token(cache=cache)

        assert result is False

    @pytest.mark.asyncio
    async def test_adm_refresh_blocks_aas_refresh(self) -> None:
        """ADM refresh should block subsequent AAS refresh."""
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

        # Now AAS should be blocked
        from custom_components.googlefindmy.Auth.token_refresh import (
            async_regenerate_aas_token,
        )

        result = await async_regenerate_aas_token(cache=cache)

        assert result is False


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
