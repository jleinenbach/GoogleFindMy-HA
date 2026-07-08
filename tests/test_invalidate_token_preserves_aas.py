# tests/test_invalidate_token_preserves_aas.py
"""Regression guard for AAS-token preservation on scoped-token invalidation.

Root cause (v1.7.10): ``_invalidate_token_async`` unconditionally nulled the
quasi-permanent AAS master token in the volatile entry-scoped cache on *every*
scoped (spot/adm) auth failure. Because the runtime AAS regeneration path only
reads the one-time (consumed) Chrome cookie and never falls back to the
persistent ``entry.data`` SSOT, this turned a routinely transient scoped-token
failure into a user-visible reauth that only a full integration reload could
heal. These tests pin the fixed contract: a scoped invalidation must leave the
AAS untouched, while a genuinely non-scoped kind still clears it.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module

DATA_AAS_TOKEN = spot_request_module.DATA_AAS_TOKEN


class _DummyCache(TokenCache):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value


@pytest.mark.asyncio
async def test_scoped_spot_invalidation_preserves_aas() -> None:
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")
    await cache.set("spot_token_user@example.com", "ya29.scoped")

    await spot_request_module._invalidate_token_async(
        "spot", "user@example.com", cache=cache
    )

    # Scoped token cleared, quasi-permanent AAS preserved.
    assert cache._data.get("spot_token_user@example.com") is None
    assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/master"


@pytest.mark.asyncio
async def test_scoped_adm_invalidation_preserves_aas() -> None:
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")
    await cache.set("adm_token_user@example.com", "ya29.scoped")

    await spot_request_module._invalidate_token_async(
        "adm", "user@example.com", cache=cache
    )

    assert cache._data.get("adm_token_user@example.com") is None
    assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/master"


@pytest.mark.asyncio
async def test_non_scoped_kind_still_clears_aas() -> None:
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")

    await spot_request_module._invalidate_token_async(
        "aas", "user@example.com", cache=cache
    )

    # The explicit "aas" kind clears the AAS (intentional, documented path).
    assert cache._data.get(DATA_AAS_TOKEN) is None


@pytest.mark.asyncio
async def test_unknown_kind_preserves_aas_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive tail: an unrecognized kind must NOT clear the AAS master.

    Only "spot"/"adm" (scoped) and the explicit "aas" kind are handled; any
    future/unknown kind must fall into the whitelist tail, which logs a warning
    and clears nothing (in particular it must never null the parent AAS
    credential -- the destructive default this hardening removes).
    """
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")
    await cache.set("spot_token_user@example.com", "ya29.scoped")

    with caplog.at_level("WARNING"):
        await spot_request_module._invalidate_token_async(
            "nova", "user@example.com", cache=cache
        )

    # Nothing cleared: AAS master and the unrelated scoped token both survive.
    assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/master"
    assert cache._data.get("spot_token_user@example.com") == "ya29.scoped"
    assert any(
        "Unexpected token kind" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_revoked_aas_still_cleared_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-contract: a genuinely revoked AAS is still cleared and escalates.

    The fix narrows *only* the scoped (spot/adm) invalidation path; it must not
    weaken the security escalation for a truly invalid AAS. Here the scoped-token
    derivation raises ``InvalidAasTokenError`` (revoked parent credential), so the
    real ``async_spot_request`` retry loop must clear the AAS via
    ``_clear_aas_token_async`` and, once the one clear attempt is spent, surface
    ``SpotAuthPermanentError`` -- exactly the reauth escalation the fix preserves.
    This drives the caller so the ``_pick_auth_token_async`` re-raise and the
    loop's clear-and-escalate wiring are both exercised, not just the leaf helper.
    """
    cache = _DummyCache()
    await cache.set("username", "user@example.com")
    await cache.set(DATA_AAS_TOKEN, "aas_et/revoked")

    async def _raise_invalid_aas(*_args: Any, **_kwargs: Any) -> str:
        raise spot_request_module.InvalidAasTokenError("AAS revoked")

    # Scoped-token derivation fails because the parent AAS is genuinely revoked.
    monkeypatch.setattr(spot_request_module, "async_get_spot_token", _raise_invalid_aas)

    with pytest.raises(spot_request_module.SpotAuthPermanentError):
        await spot_request_module.async_spot_request(
            "GetEidInfoForE2eeDevices", b"", cache=cache
        )

    # The revoked AAS was cleared by the escalation path (contract preserved).
    assert cache._data.get(DATA_AAS_TOKEN) is None
