# tests/test_reauth_reason.py
"""FIX 3 unit tests: structured reauth-reason model, recording choke point,
raise-site classification and the redaction-safe diagnostics mirror.

These pin the behaviour introduced by FIX 3 (make *why* a reauth fired visible
in diagnostics without a ``--debug`` capture):

- :class:`ReauthReasonCode` values are stable literal identifiers and
  :class:`ReauthReason` is a slotted, literal-only record.
- ``GoogleFindMyCoordinator.record_reauth_reason`` stores the reason, snapshots
  the transient-auth counters and emits a once-per ``(code, origin)`` WARNING.
- Every ``ConfigEntryAuthFailed`` raise site tags the exception with the correct
  ``reauth_code`` so the coordinator catch sites can record it.
- ``diagnostics._reauth_reason_block`` mirrors the reason and, by the
  literal-only-input invariant, never leaks free-form/PII content.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

import custom_components.googlefindmy.api as api_module
from custom_components.googlefindmy._reauth_reason import (
    ReauthReason,
    ReauthReasonCode,
)
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.diagnostics import _reauth_reason_block
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    NovaAuthPermanentError,
    NovaHTTPError,
)
from tests.helpers.main_coordinator_stub import MainCoordinatorStub


class TestReauthReasonModel:
    """The enum and dataclass are stable, literal-only value objects."""

    def test_codes_serialize_to_stable_literals(self) -> None:
        # StrEnum → str(value) is the literal identifier, safe to expose verbatim.
        assert str(ReauthReasonCode.HTTP_401_AFTER_REFRESH) == "http_401_after_refresh"
        assert str(ReauthReasonCode.BADAUTH_GPSOAUTH) == "badauth_gpsoauth"
        assert str(ReauthReasonCode.UNKNOWN) == "unknown"

    def test_dataclass_is_slotted_and_defaults_empty(self) -> None:
        reason = ReauthReason(
            code=ReauthReasonCode.DECRYPT_STALE_KEY,
            origin="locate.py:async_locate_device:decrypt_stale_key",
        )
        assert reason.code is ReauthReasonCode.DECRYPT_STALE_KEY
        assert reason.origin == "locate.py:async_locate_device:decrypt_stale_key"
        assert reason.counters == {}
        assert reason.recorded_at == 0.0
        # slots=True ⇒ no __dict__, and unknown attributes are rejected.
        assert not hasattr(reason, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            reason.leaked = "secret"  # type: ignore[attr-defined]


class TestRecordReauthReason:
    """``record_reauth_reason`` is the single recording choke point."""

    def _coordinator(self) -> MainCoordinatorStub:
        coord = MainCoordinatorStub()
        coord._consecutive_transient_auth_failures = 2
        return coord

    def test_stores_reason_with_code_origin_and_counter_snapshot(self) -> None:
        coord = self._coordinator()
        coord.record_reauth_reason(
            ReauthReasonCode.HTTP_401_AFTER_REFRESH, "polling.py:_async_update_data"
        )
        reason = coord._reauth_reason
        assert reason is not None
        # Mutation-sharp: wrong code or origin fails here.
        assert reason.code is ReauthReasonCode.HTTP_401_AFTER_REFRESH
        assert reason.origin == "polling.py:_async_update_data"
        # Default snapshot captures the live transient-auth counter + threshold.
        assert reason.counters["consecutive_transient_auth_failures"] == 2
        assert reason.counters["max_transient_auth_failures"] >= 1
        assert reason.recorded_at > 0.0

    def test_explicit_counters_override_default_snapshot(self) -> None:
        coord = self._coordinator()
        coord.record_reauth_reason(
            ReauthReasonCode.DECRYPT_STALE_KEY,
            "locate.py:async_locate_device:decrypt_stale_key",
            counters={"custom": 7},
        )
        reason = coord._reauth_reason
        assert reason is not None
        assert reason.counters == {"custom": 7}
        assert "consecutive_transient_auth_failures" not in reason.counters

    def test_warns_once_per_code_origin(self, caplog: pytest.LogCaptureFixture) -> None:
        coord = self._coordinator()
        with caplog.at_level(logging.WARNING):
            coord.record_reauth_reason(
                ReauthReasonCode.DECRYPT_STALE_KEY,
                "locate.py:async_locate_device:decrypt_stale_key",
            )
            coord.record_reauth_reason(
                ReauthReasonCode.DECRYPT_STALE_KEY,
                "locate.py:async_locate_device:decrypt_stale_key",
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # De-dup: identical (code, origin) warns exactly once.
        assert len(warnings) == 1
        assert warnings[0].reauth_code == "decrypt_stale_key"
        assert (
            warnings[0].reauth_origin
            == "locate.py:async_locate_device:decrypt_stale_key"
        )

    def test_distinct_origin_warns_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord = self._coordinator()
        with caplog.at_level(logging.WARNING):
            # Same code, two real emitting origins (locate direct + poll cycle):
            # a distinct origin is a distinct dedup key, so it warns again.
            coord.record_reauth_reason(
                ReauthReasonCode.DECRYPT_STALE_KEY, "polling.py:_request_poll_reauth"
            )
            coord.record_reauth_reason(
                ReauthReasonCode.DECRYPT_STALE_KEY,
                "locate.py:async_locate_device:decrypt_stale_key",
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # A different origin is a distinct dedup key ⇒ warns again.
        assert len(warnings) == 2


class TestDiagnosticsReauthReasonBlock:
    """The diagnostics mirror is redaction-safe by the literal-only invariant."""

    def test_returns_none_when_no_reason_recorded(self) -> None:
        assert _reauth_reason_block(MagicMock(_reauth_reason=None)) is None

    def test_serializes_recorded_reason(self) -> None:
        reason = ReauthReason(
            code=ReauthReasonCode.HTTP_401_AFTER_REFRESH,
            origin="fixture:diagnostics_serialization",
            counters={"consecutive_transient_auth_failures": 3},
            recorded_at=1_700_000_000.0,
        )
        block = _reauth_reason_block(MagicMock(_reauth_reason=reason))
        assert block is not None
        assert block["code"] == "http_401_after_refresh"
        assert block["origin"] == "fixture:diagnostics_serialization"
        assert block["counters"] == {"consecutive_transient_auth_failures": 3}
        # recorded_at is rendered as an ISO-8601 string, not a raw float.
        assert isinstance(block["recorded_at"], str)
        assert block["recorded_at"].startswith("20")

    def test_filters_non_int_and_bool_counters(self) -> None:
        # Literal-only invariant: only genuine ints survive; bool/str dropped.
        reason = ReauthReason(
            code=ReauthReasonCode.UNKNOWN,
            origin="fixture:counter_filter",
            counters={"good": 4, "flag": True, "text": "leak"},  # type: ignore[dict-item]
            recorded_at=1_700_000_000.0,
        )
        block = _reauth_reason_block(MagicMock(_reauth_reason=reason))
        assert block is not None
        assert block["counters"] == {"good": 4}

    def test_defensive_return_none_when_attribute_access_raises(self) -> None:
        # A malformed reason whose attribute access raises must degrade to None
        # rather than propagate into the diagnostics download.
        class _Exploding:
            @property
            def code(self) -> object:
                raise RuntimeError("boom")

        assert _reauth_reason_block(MagicMock(_reauth_reason=_Exploding())) is None


def _make_api() -> GoogleFindMyAPI:
    """Build an API instance without running its normal ``__init__`` wiring."""
    api = GoogleFindMyAPI.__new__(GoogleFindMyAPI)
    api._session = None
    api._cache = MagicMock()
    api._cache.entry_id = "entry-reauth"
    api._contributor_mode = None
    api._contributor_mode_switch_epoch = 0
    api._namespace = lambda: "entry-reauth"
    return api


class TestDeviceListRaiseSiteCodes:
    """Every device-list ``ConfigEntryAuthFailed`` carries its ``reauth_code``."""

    async def _drive_device_list(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> ConfigEntryAuthFailed:
        async def _raise(*_a: object, **_k: object) -> list[dict[str, Any]]:
            raise error

        monkeypatch.setattr(api_module, "async_request_device_list", _raise)
        api = _make_api()
        with pytest.raises(ConfigEntryAuthFailed) as excinfo:
            await api.async_get_basic_device_list(username="user@example.com")
        return excinfo.value

    @pytest.mark.asyncio
    async def test_http_401_maps_to_after_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_device_list(monkeypatch, NovaHTTPError(401))
        assert exc.reauth_code is ReauthReasonCode.HTTP_401_AFTER_REFRESH

    @pytest.mark.asyncio
    async def test_nova_auth_error_maps_to_nova_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_device_list(monkeypatch, NovaAuthError(401))
        assert exc.reauth_code is ReauthReasonCode.NOVA_AUTH_FAILED

    @pytest.mark.asyncio
    async def test_gpsoauth_badauth_maps_to_badauth_gpsoauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_device_list(
            monkeypatch, RuntimeError("BadAuthentication from gpsoauth")
        )
        assert exc.reauth_code is ReauthReasonCode.BADAUTH_GPSOAUTH

    @pytest.mark.asyncio
    async def test_tokencache_closed_maps_to_tokencache_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_device_list(
            monkeypatch, RuntimeError("TokenCache is closed")
        )
        assert exc.reauth_code is ReauthReasonCode.TOKENCACHE_CLOSED


class TestLocationRaiseSiteCodes:
    """Every location ``ConfigEntryAuthFailed`` carries its ``reauth_code``."""

    async def _drive_location(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> ConfigEntryAuthFailed:
        async def _raise(*_a: object, **_k: object) -> list[dict[str, Any]]:
            raise error

        monkeypatch.setattr(api_module, "get_location_data_for_device", _raise)
        api = _make_api()
        with pytest.raises(ConfigEntryAuthFailed) as excinfo:
            await api.async_get_device_location("device-xyz", "Tracker")
        return excinfo.value

    @pytest.mark.asyncio
    async def test_permanent_nova_auth_maps_to_nova_auth_permanent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_location(monkeypatch, NovaAuthPermanentError(403))
        assert exc.reauth_code is ReauthReasonCode.NOVA_AUTH_PERMANENT

    @pytest.mark.asyncio
    async def test_permanent_flag_nova_auth_maps_to_nova_auth_permanent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_location(
            monkeypatch, NovaAuthError(401, is_permanent=True)
        )
        assert exc.reauth_code is ReauthReasonCode.NOVA_AUTH_PERMANENT

    @pytest.mark.asyncio
    async def test_http_401_maps_to_after_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exc = await self._drive_location(monkeypatch, NovaHTTPError(401))
        assert exc.reauth_code is ReauthReasonCode.HTTP_401_AFTER_REFRESH
