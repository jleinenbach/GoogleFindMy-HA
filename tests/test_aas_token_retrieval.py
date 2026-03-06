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

# asyncio_mode = "auto" in pyproject.toml handles async detection automatically


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


async def test_exchange_oauth_for_aas_logs_inputs(
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

    result = await aas_token_retrieval._exchange_oauth_for_aas(
        "user@example.com", "oauth-secret-value", 0x1234
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


async def test_exchange_oauth_for_aas_missing_token_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing Token key results in a warning and a RuntimeError."""

    def fake_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        return {"Error": "BadAuthentication"}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    caplog.set_level(logging.WARNING, logger=aas_token_retrieval.__name__)

    with pytest.raises(RuntimeError, match="Missing 'Token' in gpsoauth response"):
        await aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "oauth-secret-value", 0xDEADBEEF
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


async def test_async_get_aas_token_short_circuits_for_cached_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached AAS token must be reused without calling gpsoauth.exchange_token."""

    cache = _DummyCache()

    async def _prepare() -> None:
        await cache.set(username_string, "user@example.com")
        await cache.set(CONF_OAUTH_TOKEN, "aas_et/MASTER_TOKEN")

    await _prepare()

    called = False

    def _fail_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        raise AssertionError(
            "gpsoauth.exchange_token should not be invoked when an AAS token is cached"
        )

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", _fail_exchange)

    result = await aas_token_retrieval.async_get_aas_token(cache=cache)

    assert result == "aas_et/MASTER_TOKEN"
    assert not called
    assert await cache.get(DATA_AAS_TOKEN) == "aas_et/MASTER_TOKEN"


async def test_get_or_generate_android_id_ignores_boolean_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boolean android_id placeholders should be ignored and replaced."""

    cache = _DummyCache()

    async def _prepare() -> None:
        await cache.set("android_id_user@example.com", True)

    await _prepare()
    # secrets.randbelow(0xF000000000000000) + 0x1000000000000000 = final value
    # Mock randbelow to return 0 so final value = 0x1000000000000000
    monkeypatch.setattr(aas_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF12)
    expected_id = 0xABCDEF12 + 0x1000000000000000

    android_id = await aas_token_retrieval._get_or_generate_android_id(
        "user@example.com", cache=cache
    )

    assert android_id == expected_id
    assert cache._data["android_id_user@example.com"] == expected_id


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
    fixed_id = 0xDEADBEEFCAFED00D
    monkeypatch.setattr(
        token_retrieval.secrets, "randbelow", lambda _: fixed_id - 0x1000000000000000
    )

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
        fixed_id,
    )
    assert sentinel_cache._data["android_id_user@example.com"] == fixed_id


# ---------------------------------------------------------------------------
# Additional tests for 100% coverage of aas_token_retrieval.py
# ---------------------------------------------------------------------------


def test_clip_truncates_long_strings() -> None:
    """Strings longer than the limit should be truncated with ellipsis."""
    long_string = "x" * 300
    result = aas_token_retrieval._clip(long_string, limit=200)
    assert len(result) == 200
    assert result.endswith("…")
    assert result == "x" * 199 + "…"


def test_clip_preserves_short_strings() -> None:
    """Strings shorter than the limit should be unchanged."""
    short_string = "hello"
    result = aas_token_retrieval._clip(short_string, limit=200)
    assert result == "hello"


def test_summarize_response_with_mapping() -> None:
    """Mapping objects should be summarized with sorted keys."""
    resp = {"B": 1, "A": 2, "C": 3}
    result = aas_token_retrieval._summarize_response(resp)
    assert result == "dict(keys=[A, B, C])"


def test_summarize_response_with_non_mapping() -> None:
    """Non-Mapping objects should show their type name."""
    result = aas_token_retrieval._summarize_response([1, 2, 3])
    assert result == "list"

    result2 = aas_token_retrieval._summarize_response("string")
    assert result2 == "str"


def test_mask_email_no_at_sign() -> None:
    """Emails without @ should return <unknown>."""
    assert aas_token_retrieval._mask_email_for_logs("noemail") == "<unknown>"
    assert aas_token_retrieval._mask_email_for_logs("") == "<unknown>"
    assert aas_token_retrieval._mask_email_for_logs(None) == "<unknown>"


def test_mask_email_empty_local_part() -> None:
    """Emails with empty local part should mask properly."""
    assert aas_token_retrieval._mask_email_for_logs("@example.com") == "*@example.com"


def test_mask_email_single_char_local() -> None:
    """Emails with single-char local part should return single asterisk."""
    assert aas_token_retrieval._mask_email_for_logs("a@example.com") == "*@example.com"


def test_looks_like_jwt_positive() -> None:
    """JWT-like strings should be detected."""
    jwt_like = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
    )
    assert aas_token_retrieval._looks_like_jwt(jwt_like) is True


def test_looks_like_jwt_negative() -> None:
    """Non-JWT strings should not match."""
    assert aas_token_retrieval._looks_like_jwt("aas_et/TOKEN") is False
    assert aas_token_retrieval._looks_like_jwt("no-dots") is False
    assert aas_token_retrieval._looks_like_jwt("one.dot") is False
    assert (
        aas_token_retrieval._looks_like_jwt("xy.z.abc") is False
    )  # doesn't start with eyJ


def test_disqualifies_oauth_for_exchange_jwt() -> None:
    """JWT-like tokens should be disqualified."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
    reason = aas_token_retrieval._disqualifies_oauth_for_exchange(jwt)
    assert reason is not None
    assert "JWT" in reason


def test_disqualifies_oauth_for_exchange_valid() -> None:
    """Valid OAuth tokens should not be disqualified."""
    assert (
        aas_token_retrieval._disqualifies_oauth_for_exchange("ya29.oauth_token") is None
    )
    assert aas_token_retrieval._disqualifies_oauth_for_exchange("aas_et/TOKEN") is None


def test_is_non_retryable_auth_badauthentication() -> None:
    """BadAuthentication errors should not be retryable."""
    err = RuntimeError("BadAuthentication")
    assert aas_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_invalid_grant() -> None:
    """invalid_grant errors should not be retryable."""
    err = RuntimeError("Error: invalid_grant")
    assert aas_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_unauthorized_forbidden() -> None:
    """401/403-style errors should not be retryable."""
    assert (
        aas_token_retrieval._is_non_retryable_auth(RuntimeError("unauthorized")) is True
    )
    assert aas_token_retrieval._is_non_retryable_auth(RuntimeError("forbidden")) is True


def test_is_non_retryable_auth_missing_token() -> None:
    """Missing token in gpsoauth response should not be retryable."""
    err = RuntimeError("missing 'token' in gpsoauth response")
    assert aas_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_transient() -> None:
    """Transient errors should be retryable."""
    err = RuntimeError("temporary network failure")
    assert aas_token_retrieval._is_non_retryable_auth(err) is False


def test_coerce_android_id_int() -> None:
    """Integer android_id should be returned unchanged."""
    assert aas_token_retrieval._coerce_android_id(12345, "test") == 12345


def test_coerce_android_id_string_decimal() -> None:
    """Decimal string android_id should be parsed."""
    assert aas_token_retrieval._coerce_android_id("12345", "test") == 12345


def test_coerce_android_id_string_hex() -> None:
    """Hex string android_id should be parsed."""
    assert aas_token_retrieval._coerce_android_id("0x1234", "test") == 0x1234


def test_coerce_android_id_string_invalid() -> None:
    """Invalid string android_id should return None."""
    assert aas_token_retrieval._coerce_android_id("not_a_number", "test") is None


def test_coerce_android_id_unsupported_type() -> None:
    """Unsupported types should return None."""
    assert aas_token_retrieval._coerce_android_id([1, 2, 3], "test") is None
    assert aas_token_retrieval._coerce_android_id({"key": "val"}, "test") is None


def test_coerce_android_id_none() -> None:
    """None should return None without logging."""
    assert aas_token_retrieval._coerce_android_id(None, "test") is None


async def test_get_or_generate_android_id_requires_cache() -> None:
    """_get_or_generate_android_id must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        await aas_token_retrieval._get_or_generate_android_id(
            "user@example.com", cache=None
        )


async def test_get_or_generate_android_id_uses_fcm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FCM credentials android_id should be used and cached."""
    cache = _DummyCache()
    await cache.set("fcm_credentials", {"gcm": {"android_id": "0xFCF00D"}})

    result = await aas_token_retrieval._get_or_generate_android_id(
        "user@example.com", cache=cache
    )

    assert result == 0xFCF00D
    assert cache._data["android_id_user@example.com"] == 0xFCF00D


async def test_get_or_generate_android_id_prefers_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached android_id should be used when FCM has none."""
    cache = _DummyCache()
    await cache.set("android_id_user@example.com", 0x12345678)
    await cache.set("fcm_credentials", {"gcm": {}})  # No android_id in FCM

    result = await aas_token_retrieval._get_or_generate_android_id(
        "user@example.com", cache=cache
    )

    assert result == 0x12345678


async def test_get_or_generate_android_id_handles_fcm_cache_write_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Cache write failures for FCM-derived android_id should be handled gracefully."""

    class FailingWriteCache(_DummyCache):
        async def set(self, name: str, value: Any) -> None:
            if "android_id" in name:
                raise RuntimeError("Cache write failed")
            await super().set(name, value)

    cache = FailingWriteCache()
    cache._data["fcm_credentials"] = {"gcm": {"android_id": "0xFCF00D"}}

    caplog.set_level(logging.DEBUG)
    result = await aas_token_retrieval._get_or_generate_android_id(
        "user@example.com", cache=cache
    )

    # Should still return the FCM value despite cache failure
    assert result == 0xFCF00D


async def test_get_or_generate_android_id_generates_new_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When FCM and cache are empty, a new ID should be generated."""

    class FailingWriteCache(_DummyCache):
        async def set(self, name: str, value: Any) -> None:
            if "android_id" in name:
                raise RuntimeError("Cache write failed")
            await super().set(name, value)

    cache = FailingWriteCache()
    monkeypatch.setattr(aas_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF)

    caplog.set_level(logging.WARNING)
    result = await aas_token_retrieval._get_or_generate_android_id(
        "user@example.com", cache=cache
    )

    expected_id = 0xABCDEF + 0x1000000000000000
    assert result == expected_id
    assert any("Generated new android_id" in r.message for r in caplog.records)


async def test_exchange_oauth_for_aas_auth_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """gpsoauth.AuthError should be wrapped in RuntimeError."""
    # Create a mock AuthError
    import types

    mock_exceptions = types.SimpleNamespace()

    class MockAuthError(Exception):
        pass

    mock_exceptions.AuthError = MockAuthError
    monkeypatch.setattr(aas_token_retrieval, "gpsoauth_exceptions", mock_exceptions)

    def fake_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        raise MockAuthError("Auth failed")

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.WARNING)

    with pytest.raises(RuntimeError, match="gpsoauth authentication failed"):
        await aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "token", 0x1234
        )


async def test_exchange_oauth_for_aas_generic_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Generic exceptions should be wrapped in RuntimeError."""
    monkeypatch.setattr(aas_token_retrieval, "gpsoauth_exceptions", None)

    def fake_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        raise ValueError("Unexpected error")

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.ERROR)

    with pytest.raises(RuntimeError, match="gpsoauth exchange failed"):
        await aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "token", 0x1234
        )


async def test_exchange_oauth_for_aas_invalid_response_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict response should raise RuntimeError."""

    def fake_exchange(*_: Any, **__: Any) -> list[Any]:
        return []

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    with pytest.raises(RuntimeError, match="Invalid response from gpsoauth"):
        await aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "token", 0x1234
        )


async def test_exchange_oauth_for_aas_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty dict response should raise RuntimeError."""

    def fake_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    with pytest.raises(RuntimeError, match="Invalid response from gpsoauth"):
        await aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "token", 0x1234
        )


async def test_generate_aas_token_requires_cache() -> None:
    """_generate_aas_token must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        await aas_token_retrieval._generate_aas_token(cache=None)


async def test_generate_aas_token_jwt_disqualified(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """JWT-like OAuth tokens should be disqualified and fallback used."""
    cache = _DummyCache()
    jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
    await cache.set(CONF_OAUTH_TOKEN, jwt_token)
    await cache.set(username_string, "user@example.com")
    await cache.set("adm_token_user@example.com", "valid_oauth_token")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        assert oauth_token == "valid_oauth_token"  # Should use ADM fallback
        return {"Token": "aas_et/NEW", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.WARNING)

    result = await aas_token_retrieval._generate_aas_token(cache=cache)
    assert result == "aas_et/NEW"
    assert any("JWT" in r.message for r in caplog.records)


async def test_generate_aas_token_missing_username_with_aas_shortcut() -> None:
    """AAS shortcut without username should raise ValueError."""
    cache = _DummyCache()
    await cache.set(CONF_OAUTH_TOKEN, "aas_et/ALREADY_AAS")

    with pytest.raises(ValueError, match="No username available"):
        await aas_token_retrieval._generate_aas_token(cache=cache)


async def test_generate_aas_token_missing_oauth() -> None:
    """Missing OAuth token should raise ValueError."""
    cache = _DummyCache()
    await cache.set(username_string, "user@example.com")

    with pytest.raises(ValueError, match="No OAuth token available"):
        await aas_token_retrieval._generate_aas_token(cache=cache)


async def test_generate_aas_token_missing_username_for_exchange() -> None:
    """Missing username for gpsoauth exchange should raise ValueError."""
    cache = _DummyCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")

    with pytest.raises(ValueError, match="No username available"):
        await aas_token_retrieval._generate_aas_token(cache=cache)


async def test_generate_aas_token_persists_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email from gpsoauth response should be persisted to cache."""
    cache = _DummyCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")
    await cache.set(username_string, "old@example.com")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        return {"Token": "aas_token", "Email": "new@example.com"}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    result = await aas_token_retrieval._generate_aas_token(cache=cache)
    assert result == "aas_token"
    assert cache._data[username_string] == "new@example.com"


async def test_generate_aas_token_no_oauth_uses_entry_adm_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without OAuth token, only the entry-scoped ADM cache should be used (no global fallback)."""
    cache = _DummyCache()
    await cache.set(username_string, "user@example.com")
    await cache.set("adm_token_user@example.com", "local_oauth")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        assert oauth_token == "local_oauth"
        return {"Token": "aas_token", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    result = await aas_token_retrieval._generate_aas_token(cache=cache)
    assert result == "aas_token"


async def test_async_get_aas_token_requires_cache() -> None:
    """async_get_aas_token must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        await aas_token_retrieval.async_get_aas_token(cache=None)


async def test_async_get_aas_token_retries_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient errors should be retried with backoff."""
    cache = _DummyCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")
    await cache.set(username_string, "user@example.com")

    attempts: list[int] = []
    sleep_durations: list[float] = []

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Temporary failure")
        return {"Token": "aas_token", "Email": username}

    async def fake_sleep(duration: float) -> None:
        sleep_durations.append(duration)

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    monkeypatch.setattr(aas_token_retrieval.asyncio, "sleep", fake_sleep)

    result = await aas_token_retrieval.async_get_aas_token(
        cache=cache, retries=2, backoff=1.0
    )

    assert result == "aas_token"
    assert len(attempts) == 3
    assert sleep_durations == [1.0, 2.0]


async def test_async_get_aas_token_no_retry_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-retryable auth errors should not be retried."""
    cache = _DummyCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")
    await cache.set(username_string, "user@example.com")

    attempts: list[int] = []

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        attempts.append(1)
        raise RuntimeError("BadAuthentication")

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    with pytest.raises(RuntimeError, match="BadAuthentication"):
        await aas_token_retrieval.async_get_aas_token(cache=cache, retries=3)

    assert len(attempts) == 1  # No retries


async def test_async_get_aas_token_records_ttl_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh token generation should record TTL timestamp."""
    cache = _DummyCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")
    await cache.set(username_string, "user@example.com")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        return {"Token": "aas_token", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    result = await aas_token_retrieval.async_get_aas_token(cache=cache)

    assert result == "aas_token"
    assert "aas_token_issued_at_user@example.com" in cache._data


async def test_async_get_aas_token_ttl_write_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TTL timestamp write failure should be handled gracefully."""

    class PartialFailCache(_DummyCache):
        async def set(self, name: str, value: Any) -> None:
            if "issued_at" in name:
                raise RuntimeError("Cache write failed")
            await super().set(name, value)

    cache = PartialFailCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")
    await cache.set(username_string, "user@example.com")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        return {"Token": "aas_token", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.DEBUG)

    result = await aas_token_retrieval.async_get_aas_token(cache=cache)
    assert result == "aas_token"


async def test_get_aas_token_sync_raises() -> None:
    """Sync API should raise RuntimeError when called from a running event loop."""
    cache = _DummyCache()
    with pytest.raises(RuntimeError, match="async_get_aas_token"):
        aas_token_retrieval.get_aas_token(cache=cache)


async def test_get_or_generate_android_id_fcm_without_gcm_block() -> None:
    """FCM credentials without gcm block should fall back to cached or generate."""
    cache = _DummyCache()
    await cache.set("fcm_credentials", {"not_gcm": {}})
    await cache.set("android_id_user@example.com", 0x12345678)

    result = await aas_token_retrieval._get_or_generate_android_id(
        "user@example.com", cache=cache
    )
    assert result == 0x12345678


async def test_generate_aas_token_shortcut_cache_write_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Cache write failure during AAS shortcut should be handled."""

    class PartialFailCache(_DummyCache):
        async def set(self, name: str, value: Any) -> None:
            if name == DATA_AAS_TOKEN:
                raise RuntimeError("Cache write failed")
            await super().set(name, value)

    cache = PartialFailCache()
    await cache.set(username_string, "user@example.com")
    await cache.set(CONF_OAUTH_TOKEN, "aas_et/ALREADY_AAS")

    caplog.set_level(logging.DEBUG)
    result = await aas_token_retrieval._generate_aas_token(cache=cache)
    assert result == "aas_et/ALREADY_AAS"


async def test_generate_aas_token_email_persist_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Email persistence failure should be handled gracefully."""

    class PartialFailCache(_DummyCache):
        async def set(self, name: str, value: Any) -> None:
            if name == username_string and value == "new@example.com":
                raise RuntimeError("Email write failed")
            await super().set(name, value)

    cache = PartialFailCache()
    await cache.set(CONF_OAUTH_TOKEN, "oauth_token")
    await cache.set(username_string, "old@example.com")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        return {"Token": "aas_token", "Email": "new@example.com"}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.DEBUG)

    result = await aas_token_retrieval._generate_aas_token(cache=cache)
    assert result == "aas_token"


async def test_generate_aas_token_adm_fallback_no_username(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ADM fallback with username from key should work."""
    cache = _DummyCache()
    # JWT-like token to force disqualification, then ADM fallback
    await cache.set(CONF_OAUTH_TOKEN, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig")
    # ADM token with username embedded in key
    await cache.set("adm_token_extracted@example.com", "oauth_from_adm")

    def fake_exchange(
        username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        assert oauth_token == "oauth_from_adm"
        return {"Token": "aas_from_adm", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.WARNING)

    result = await aas_token_retrieval._generate_aas_token(cache=cache)
    assert result == "aas_from_adm"


async def test_exchange_oauth_missing_token_no_error_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing Token with no Error field should still log properly."""

    def fake_exchange(*_: Any, **__: Any) -> dict[str, Any]:
        return {"SomeOther": "value"}  # No Token, no Error

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)
    caplog.set_level(logging.WARNING)

    with pytest.raises(RuntimeError, match="Missing 'Token'"):
        await aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "token", 0x1234
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings
    # Check that error_field_present is False
    assert getattr(warnings[0], "error_field_present") is False
