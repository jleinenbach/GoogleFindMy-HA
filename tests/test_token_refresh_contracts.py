# tests/test_token_refresh_contracts.py
"""Contract tests for ``Auth/token_refresh`` (Coverage W1, AP-W1.3).

These pin the terminal exits of the manual token-regeneration helpers and the
per-token-type cooldown key contract. The star is the H4 regression: the
documented no-op of ``clear_cooldown`` when called without a ``token_type``
(production only ever records typed ``entry_id:fcm`` / ``entry_id:adm`` keys),
which a naive refactor would "fix" into a real wipe and silently reset the
buttons' rate limits. One test per return/raise exit, assertions on the
returned value and the cooldown state, no golden master.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import token_refresh
from custom_components.googlefindmy.Auth.token_refresh import (
    _cooldown_key,
    _get_entry_id,
    _record_refresh,
    async_regenerate_adm_token,
    async_regenerate_fcm_token,
    clear_all_cooldowns,
    clear_cooldown,
    get_cooldown_remaining,
    is_refresh_on_cooldown,
)
from custom_components.googlefindmy.const import DOMAIN

_ENTRY = "entry-abc"


@pytest.fixture(autouse=True)
def _isolate_cooldowns() -> Any:
    """Prevent global cooldown-state leaks between tests (W1-3 reset)."""
    clear_all_cooldowns()
    yield
    clear_all_cooldowns()


# ---------------------------------------------------------------------------
# _cooldown_key: namespace isolation contract (:fcm != :adm)
# ---------------------------------------------------------------------------


def test_cooldown_key_none_type_is_legacy_entry_only() -> None:
    assert _cooldown_key(_ENTRY, None) == _ENTRY
    assert _cooldown_key(_ENTRY) == _ENTRY


def test_cooldown_key_normalizes_case_and_whitespace() -> None:
    # Case/whitespace fold together so "FCM" and "fcm " share one bucket.
    assert _cooldown_key(_ENTRY, "FCM") == f"{_ENTRY}:fcm"
    assert _cooldown_key(_ENTRY, "fcm ") == f"{_ENTRY}:fcm"
    assert _cooldown_key(_ENTRY, "FCM") == _cooldown_key(_ENTRY, "fcm ")


def test_cooldown_key_fcm_and_adm_are_isolated() -> None:
    # The core invariant: the two buttons live on independent rate limits.
    assert _cooldown_key(_ENTRY, "fcm") != _cooldown_key(_ENTRY, "adm")


def test_recording_fcm_does_not_place_adm_on_cooldown() -> None:
    _record_refresh(_ENTRY, "fcm")
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is True
    assert is_refresh_on_cooldown(_ENTRY, "adm")[0] is False


def test_get_cooldown_remaining_positive_after_record() -> None:
    _record_refresh(_ENTRY, "adm")
    remaining = get_cooldown_remaining(_ENTRY, "adm")
    assert 0.0 < remaining <= token_refresh.TOKEN_REFRESH_COOLDOWN_S


def test_is_refresh_on_cooldown_false_for_unknown_entry() -> None:
    on_cd, remaining = is_refresh_on_cooldown("never-recorded", "fcm")
    assert on_cd is False
    assert remaining == 0.0


# ---------------------------------------------------------------------------
# H4 regression: clear_cooldown without a token_type is a documented no-op
# ---------------------------------------------------------------------------


def test_clear_cooldown_without_type_is_noop_against_typed_keys() -> None:
    """H4: clearing the legacy entry-only key must NOT reset the typed key."""
    _record_refresh(_ENTRY, "fcm")
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is True

    clear_cooldown(_ENTRY)  # legacy key -> no-op against entry:fcm

    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is True


def test_clear_cooldown_with_matching_type_clears_it() -> None:
    _record_refresh(_ENTRY, "fcm")
    clear_cooldown(_ENTRY, "fcm")
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is False


def test_clear_all_cooldowns_wipes_every_key() -> None:
    _record_refresh(_ENTRY, "fcm")
    _record_refresh(_ENTRY, "adm")
    clear_all_cooldowns()
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is False
    assert is_refresh_on_cooldown(_ENTRY, "adm")[0] is False


# ---------------------------------------------------------------------------
# _get_entry_id: three terminal exits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_id_prefers_entry_id_attribute() -> None:
    cache = SimpleNamespace(entry_id="from-attr", _namespace="ns")
    assert await _get_entry_id(cache) == "from-attr"


@pytest.mark.asyncio
async def test_get_entry_id_falls_back_to_namespace() -> None:
    cache = SimpleNamespace(entry_id=None, _namespace="ns-fallback")
    assert await _get_entry_id(cache) == "ns-fallback"


@pytest.mark.asyncio
async def test_get_entry_id_defaults_when_nothing_present() -> None:
    cache = SimpleNamespace(entry_id=None, _namespace=None)
    assert await _get_entry_id(cache) == "default"


# ---------------------------------------------------------------------------
# async_regenerate_fcm_token: terminal exits
# ---------------------------------------------------------------------------


def _hass_with(bucket: Any) -> Any:
    return SimpleNamespace(data={DOMAIN: bucket})


class _Receiver:
    def __init__(self, result: bool | Exception) -> None:
        self._result = result
        self.called_with: str | None = None

    async def async_reregister_fcm(self, entry_id: str) -> bool:
        self.called_with = entry_id
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.asyncio
async def test_fcm_regen_blocked_on_cooldown() -> None:
    _record_refresh(_ENTRY, "fcm")
    hass = _hass_with({"fcm_receivers": {_ENTRY: _Receiver(True)}})
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is False


@pytest.mark.asyncio
async def test_fcm_regen_fails_without_integration_data() -> None:
    hass = SimpleNamespace(data={})  # bucket missing
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is False


@pytest.mark.asyncio
async def test_fcm_regen_fails_when_no_receiver_found() -> None:
    hass = _hass_with({"fcm_receivers": {}})  # no per-entry, no singleton
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is False


@pytest.mark.asyncio
async def test_fcm_regen_uses_singleton_receiver_fallback() -> None:
    receiver = _Receiver(True)
    hass = _hass_with({"fcm_receivers": {}, "fcm_receiver": receiver})
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is True
    assert receiver.called_with == _ENTRY
    # success records the typed cooldown
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is True


@pytest.mark.asyncio
async def test_fcm_regen_success_records_cooldown() -> None:
    receiver = _Receiver(True)
    hass = _hass_with({"fcm_receivers": {_ENTRY: receiver}})
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is True
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is True
    assert is_refresh_on_cooldown(_ENTRY, "adm")[0] is False


@pytest.mark.asyncio
async def test_fcm_regen_reregister_returns_false_no_cooldown() -> None:
    hass = _hass_with({"fcm_receivers": {_ENTRY: _Receiver(False)}})
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is False
    # a failed regen must not arm the cooldown
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is False


@pytest.mark.asyncio
async def test_fcm_regen_swallows_receiver_exception() -> None:
    hass = _hass_with({"fcm_receivers": {_ENTRY: _Receiver(RuntimeError("boom"))}})
    assert await async_regenerate_fcm_token(hass=hass, entry_id=_ENTRY) is False
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is False


# ---------------------------------------------------------------------------
# async_regenerate_adm_token: terminal exits
# ---------------------------------------------------------------------------


class _Cache:
    def __init__(self, entry_id: str = _ENTRY) -> None:
        self.entry_id = entry_id
        self.sets: list[tuple[str, Any]] = []

    async def set(self, key: str, value: Any) -> None:
        self.sets.append((key, value))


def _patch_adm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: Any,
    username: str | None = "user@example.com",
) -> None:
    from unittest.mock import AsyncMock

    import custom_components.googlefindmy.Auth.adm_token_retrieval as adm_mod
    import custom_components.googlefindmy.Auth.username_provider as user_mod

    monkeypatch.setattr(adm_mod, "async_get_adm_token", AsyncMock(return_value=token))
    monkeypatch.setattr(
        user_mod, "async_get_username", AsyncMock(return_value=username)
    )


@pytest.mark.asyncio
async def test_adm_regen_blocked_on_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_refresh(_ENTRY, "adm")
    _patch_adm(monkeypatch, token="new-token")
    assert await async_regenerate_adm_token(cache=_Cache()) is False


@pytest.mark.asyncio
async def test_adm_regen_fails_without_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_adm(monkeypatch, token="new-token", username=None)
    # username neither passed nor resolvable -> resolution branch + early exit
    assert await async_regenerate_adm_token(cache=_Cache()) is False


@pytest.mark.asyncio
async def test_adm_regen_success_records_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_adm(monkeypatch, token="fresh-adm")
    cache = _Cache()
    assert (
        await async_regenerate_adm_token(cache=cache, username="user@example.com")
        is True
    )
    # ADM invalidated before regeneration
    assert cache.sets and cache.sets[0][1] is None
    assert is_refresh_on_cooldown(_ENTRY, "adm")[0] is True
    # isolation: FCM stays free
    assert is_refresh_on_cooldown(_ENTRY, "fcm")[0] is False


@pytest.mark.asyncio
async def test_adm_regen_empty_token_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_adm(monkeypatch, token=None)  # regeneration yields empty token
    assert (
        await async_regenerate_adm_token(cache=_Cache(), username="user@example.com")
        is False
    )
    assert is_refresh_on_cooldown(_ENTRY, "adm")[0] is False


@pytest.mark.asyncio
async def test_adm_regen_swallows_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    import custom_components.googlefindmy.Auth.adm_token_retrieval as adm_mod
    import custom_components.googlefindmy.Auth.username_provider as user_mod

    monkeypatch.setattr(
        user_mod, "async_get_username", AsyncMock(return_value="user@example.com")
    )
    monkeypatch.setattr(
        adm_mod,
        "async_get_adm_token",
        AsyncMock(side_effect=RuntimeError("regen failed")),
    )
    assert (
        await async_regenerate_adm_token(cache=_Cache(), username="user@example.com")
        is False
    )
    assert is_refresh_on_cooldown(_ENTRY, "adm")[0] is False
