# tests/test_aas_token_retrieval.py
"""Unit tests for the gpsoauth exchange helper."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import aas_token_retrieval
from custom_components.googlefindmy.Auth.username_provider import username_string
from custom_components.googlefindmy.const import CONF_OAUTH_TOKEN, DATA_AAS_TOKEN


class _DummyCache:
    """Minimal async cache implementing the TokenCache interface used in tests."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, name: str) -> Any:
        return self._data.get(name)

    async def set(self, name: str, value: Any) -> None:
        if value is None:
            self._data.pop(name, None)
        else:
            self._data[name] = value

    async def all(self) -> dict[str, Any]:
        return dict(self._data)

    async def get_or_set(self, name: str, generator):  # type: ignore[override]
        if name in self._data:
            return self._data[name]
        result = generator()
        if asyncio.iscoroutine(result):
            result = await result
        await self.set(name, result)
        return result


def test_exchange_oauth_for_aas_logs_inputs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ensure the exchange logs a masked username and token diagnostics."""

    recorded_args: dict[str, Any] = {}

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        recorded_args["username"] = username
        recorded_args["oauth_token"] = oauth_token
        recorded_args["android_id"] = android_id
        return {"Token": "aas-token", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    caplog.set_level(logging.DEBUG, logger=aas_token_retrieval.__name__)

    result = asyncio.run(
        aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "oauth-secret-value", 0x1234
        )
    )

    assert result["Token"] == "aas-token"
    assert recorded_args == {
        "username": "user@example.com",
        "oauth_token": "oauth-secret-value",
        "android_id": 0x1234,
    }

    call_logs = [
        record
        for record in caplog.records
        if record.message == "Calling gpsoauth.exchange_token."
    ]
    assert call_logs, "Expected the gpsoauth exchange call to be logged"
    call_log = call_logs[0]
    assert getattr(call_log, "user") == "u***@example.com"
    assert getattr(call_log, "token_length") == 18
    assert getattr(call_log, "android_id_hex") == "0x1234"

    messages = "\n".join(record.message for record in caplog.records)
    assert "gpsoauth exchange response received" in messages


def test_exchange_oauth_for_aas_missing_token_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing Token key results in a warning and a RuntimeError."""

    def fake_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        return {"Error": "BadAuthentication"}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    caplog.set_level(logging.WARNING, logger=aas_token_retrieval.__name__)

    with pytest.raises(RuntimeError, match="Missing 'Token' in gpsoauth response"):
        asyncio.run(
            aas_token_retrieval._exchange_oauth_for_aas(
                "user@example.com", "oauth-secret-value", 0xDEADBEEF
            )
        )

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "gpsoauth response missing token" in record.message
    ]
    assert warnings, "Expected warning about missing Token key"
    warning = warnings[0]
    assert getattr(warning, "error_field_present") is True
    assert getattr(warning, "response_keys") == ["Error"]
    assert getattr(warning, "user") == "u***@example.com"


def test_async_get_aas_token_short_circuits_for_cached_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached AAS token must be reused without calling gpsoauth.exchange_token."""

    cache = _DummyCache()

    async def _prepare() -> None:
        await cache.set(username_string, "user@example.com")
        await cache.set(CONF_OAUTH_TOKEN, "aas_et/MASTER_TOKEN")

    asyncio.run(_prepare())

    called = False

    def _fail_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        raise AssertionError(
            "gpsoauth.exchange_token should not be invoked when an AAS token is cached"
        )

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", _fail_exchange)

    result = asyncio.run(aas_token_retrieval.async_get_aas_token(cache=cache))

    assert result == "aas_et/MASTER_TOKEN"
    assert not called
    assert asyncio.run(cache.get(DATA_AAS_TOKEN)) == "aas_et/MASTER_TOKEN"


def test_get_or_generate_android_id_ignores_boolean_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boolean android_id placeholders should be ignored and replaced."""

    cache = _DummyCache()

    async def _prepare() -> None:
        await cache.set("android_id_user@example.com", True)

    asyncio.run(_prepare())
    monkeypatch.setattr(aas_token_retrieval.random, "randint", lambda *_: 0xABCDEF12)

    android_id = asyncio.run(
        aas_token_retrieval._get_or_generate_android_id(
            "user@example.com", cache=cache
        )
    )

    assert android_id == 0xABCDEF12
    assert cache._data["android_id_user@example.com"] == 0xABCDEF12


def test_request_token_uses_supplied_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synchronous request_token helper must forward the provided cache."""

    from custom_components.googlefindmy.Auth import token_retrieval

    recorded: dict[str, object] = {}

    async def fake_async_get_aas_token(*, cache) -> str:  # type: ignore[no-untyped-def]
        recorded["cache"] = cache
        return "aas-token"

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        scope: str,
        play_services: bool,
        *,
        android_id: int,
    ) -> str:
        recorded["oauth_params"] = (
            username,
            aas_token,
            scope,
            play_services,
            android_id,
        )
        return "spot-token"

    monkeypatch.setattr(
        token_retrieval, "async_get_aas_token", fake_async_get_aas_token
    )
    monkeypatch.setattr(token_retrieval, "_perform_oauth_sync", fake_perform_oauth)
    monkeypatch.setattr(token_retrieval.random, "randint", lambda *_: 0xDEADBEEFCAFED00D)

    sentinel_cache = _DummyCache()
    token = token_retrieval.request_token(
        "user@example.com", "spot", cache=sentinel_cache
    )

    assert token == "spot-token"
    assert recorded["cache"] is sentinel_cache
    assert recorded["oauth_params"] == (
        "user@example.com",
        "aas-token",
        "spot",
        False,
        0xDEADBEEFCAFED00D,
    )
    assert sentinel_cache._data["android_id_user@example.com"] == 0xDEADBEEFCAFED00D
