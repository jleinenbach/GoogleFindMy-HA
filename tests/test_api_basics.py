# tests/test_api_basics.py
"""Basics coverage for :mod:`custom_components.googlefindmy.api` (Phase 4 AP-I).

Risk-priority follows Aniche RV-G3: methods whose failure mode silently corrupts
user-facing data (token retrieval, error mapping, sync-wrappers) are covered
first. End-to-end Auth round-trips and HTTP transport remain Phase-5 scope.

Class naming uses the ``Basics`` suffix (CA-F6: disjoint from existing
``_StubCache`` / ``_SyncHarness`` classes in :mod:`tests.test_api_location_selection`).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy._reauth_reason import ReauthReasonCode
from custom_components.googlefindmy.api import (
    GoogleFindMyAPI,
    _build_can_ring_index,
    _EphemeralCache,
    _infer_can_ring_slot,
    _is_multi_entry_guard_message,
    _maybe_log_guard_once,
    _short_err,
    register_fcm_receiver_provider,
    unregister_fcm_receiver_provider,
)
from custom_components.googlefindmy.const import (
    CONTRIBUTOR_MODE_HIGH_TRAFFIC,
    CONTRIBUTOR_MODE_IN_ALL_AREAS,
    DEFAULT_CONTRIBUTOR_MODE,
    SoundDispatchOutcome,
)
from custom_components.googlefindmy.exceptions import MissingTokenCacheError
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    SharedKeyMismatchError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    NovaError,
    NovaHTTPError,
    NovaLogicError,
    NovaProtobufDecodeError,
    NovaRateLimitError,
)
from tests.helpers.api_stub import (
    FakeReceiver,
    RaisingCache,
    StubCache,
    install_receiver_provider,
    make_pc,
    run_coro,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fcm_provider() -> Any:
    """Snapshot/restore the module-level FCM receiver getter."""

    saved = api_module._FCM_ReceiverGetter
    yield
    api_module._FCM_ReceiverGetter = saved


@pytest.fixture(autouse=True)
def _reset_guard_log_flag() -> Any:
    """Reset the module-level multi-entry guard log flag between tests."""

    api_module._GUARD_LOGGED_ONCE = False
    yield
    api_module._GUARD_LOGGED_ONCE = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestShortErrBasics:
    """:func:`_short_err` truncation and pass-through."""

    def test_short_message_is_returned_verbatim(self) -> None:
        assert _short_err("kurz") == "kurz"

    def test_long_message_is_truncated_with_ellipsis(self) -> None:
        long = "x" * (api_module._MAX_ERR_CHARS + 50)
        result = _short_err(long)
        assert len(result) == api_module._MAX_ERR_CHARS
        assert result.endswith("...")

    def test_exception_input_is_stringified(self) -> None:
        assert _short_err(RuntimeError("boom")) == "boom"


class TestMultiEntryGuardDetection:
    """Both markers of the multi-entry guard message."""

    @pytest.mark.parametrize(
        "msg",
        [
            "Multiple config entries active for googlefindmy",
            "entry.runtime_data missing for this entry",
        ],
    )
    def test_detects_known_markers(self, msg: str) -> None:
        assert _is_multi_entry_guard_message(msg) is True

    @pytest.mark.parametrize("msg", ["", "unrelated error"])
    def test_rejects_unrelated_messages(self, msg: str) -> None:
        assert _is_multi_entry_guard_message(msg) is False


class TestMaybeLogGuardOnce:
    """Initial INFO log, subsequent DEBUG suppression, optional context."""

    def test_first_call_logs_info_then_suppressed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=api_module._LOGGER.name)
        _maybe_log_guard_once("device_list")
        _maybe_log_guard_once("device_list")
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(info_records) == 1
        assert len(debug_records) == 1
        assert "device_list" in debug_records[0].getMessage()

    def test_extras_are_appended(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger=api_module._LOGGER.name)
        _maybe_log_guard_once(
            "device_list", email="user@example.com", entry_id="entry-1"
        )
        info = [r for r in caplog.records if r.levelno == logging.INFO]
        message = info[0].getMessage()
        assert "email=user@example.com" in message
        assert "entry_id=entry-1" in message


class TestFcmProviderRegistration:
    """Register/unregister keeps the module-global provider in sync."""

    def test_register_and_unregister_round_trip(self) -> None:
        def _getter(_entry_id: str | None = None) -> FakeReceiver:
            return FakeReceiver()

        register_fcm_receiver_provider(_getter)
        assert api_module._FCM_ReceiverGetter is _getter
        unregister_fcm_receiver_provider()
        assert api_module._FCM_ReceiverGetter is None


# ---------------------------------------------------------------------------
# Capability helpers
# ---------------------------------------------------------------------------


class TestInferCanRingSlotBasics:
    """Slot inference across the documented input shapes."""

    @pytest.mark.parametrize(
        "device, expected",
        [
            ({"can_ring": True}, True),
            ({"can_ring": False}, False),
            ({"canRing": True}, True),
            ({"capabilities": ["ring", "vibrate"]}, True),
            ({"capabilities": {"PLAY_SOUND": True}}, True),
            ({"capabilities": {"ring": False, "play_sound": False}}, False),
            ({"capabilities": "scalar-not-iterable"}, None),
            ({}, None),
        ],
    )
    def test_returns_expected_verdict(
        self, device: dict[str, Any], expected: bool | None
    ) -> None:
        assert _infer_can_ring_slot(device) is expected

    def test_returns_none_on_internal_error(self) -> None:
        class _Boom(dict):  # type: ignore[type-arg]
            def __contains__(self, key: object) -> bool:
                raise RuntimeError("boom")

        assert _infer_can_ring_slot(_Boom()) is None


class TestBuildCanRingIndexBasics:
    """Index respects identifier fallbacks and ignores undecidable rows."""

    def test_uses_canonical_id_and_skips_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empirie: api.py:Z290-Z315 (_infer_can_ring_slot) — returns None ONLY when
        # neither `can_ring`/`canRing` nor a `capabilities` key is present. An empty
        # list/dict for `capabilities` still triggers the membership check, yielding
        # False (a valid bool), so such a row IS recorded in the index. To test the
        # skip-unknown path we must omit the `capabilities` key entirely.
        rows = [
            {"canonicalId": "A", "can_ring": True},
            {"id": "B", "can_ring": False},
            {"device_id": "C"},  # no can_ring / canRing / capabilities → verdict None
            {"id": "", "can_ring": True},
        ]
        monkeypatch.setattr(
            api_module, "get_devices_with_location", lambda *_a, **_k: rows
        )
        index = _build_can_ring_index(object(), cache=None)
        assert index == {"A": True, "B": False}

    def test_decoder_error_yields_empty_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("decoder boom")

        monkeypatch.setattr(api_module, "get_devices_with_location", _raise)
        assert _build_can_ring_index(object(), cache=None) == {}


# ---------------------------------------------------------------------------
# Constructor / EphemeralCache / contributor mode / namespace
# ---------------------------------------------------------------------------


class TestApiInitBasics:
    """``__init__`` paths: cache, ephemeral, missing credentials, epoch."""

    def test_requires_cache_or_credentials(self) -> None:
        with pytest.raises(TypeError, match="GoogleFindMyAPI requires"):
            GoogleFindMyAPI()

    def test_ephemeral_cache_built_from_token_or_email(self) -> None:
        api = GoogleFindMyAPI(oauth_token="tok", google_email="x@example.com")
        assert isinstance(api._cache, _EphemeralCache)

    def test_invalid_switch_epoch_falls_back_to_now(self) -> None:
        api = GoogleFindMyAPI(
            cache=StubCache(),
            contributor_mode_switch_epoch=0,
        )
        assert api._contributor_mode_switch_epoch > 0

    def test_contributor_mode_normalized_at_init(self) -> None:
        api = GoogleFindMyAPI(
            cache=StubCache(),
            contributor_mode="  HIGH_TRAFFIC  ",
        )
        assert api._contributor_mode == CONTRIBUTOR_MODE_HIGH_TRAFFIC

    def test_unknown_contributor_mode_uses_default(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache(), contributor_mode="bogus")
        assert api._contributor_mode == DEFAULT_CONTRIBUTOR_MODE


class TestEphemeralCacheBasics:
    """Public TokenCache-protocol surface of :class:`_EphemeralCache`."""

    def test_get_set_delete_round_trip(self) -> None:
        cache = _EphemeralCache(oauth_token="tok", email="x@example.com")
        run_coro(cache.set("k", "v"))
        assert run_coro(cache.get("k")) == "v"
        run_coro(cache.set("k", None))
        assert run_coro(cache.get("k")) is None

    def test_all_returns_snapshot_copy(self) -> None:
        cache = _EphemeralCache(oauth_token="tok", email="x@example.com")
        snap = run_coro(cache.all())
        snap["mutated"] = True
        assert "mutated" not in run_coro(cache.all())

    def test_get_or_set_with_sync_generator(self) -> None:
        cache = _EphemeralCache(oauth_token="tok", email="x@example.com")
        value = run_coro(cache.get_or_set("new", lambda: "computed"))
        assert value == "computed"
        assert run_coro(cache.get("new")) == "computed"

    def test_get_or_set_with_async_generator(self) -> None:
        cache = _EphemeralCache(oauth_token="tok", email="x@example.com")

        async def _gen() -> str:
            return "async-value"

        assert run_coro(cache.get_or_set("k", _gen)) == "async-value"

    def test_get_or_set_returns_existing(self) -> None:
        cache = _EphemeralCache(oauth_token="tok", email="x@example.com")
        run_coro(cache.set("k", "pre"))
        assert run_coro(cache.get_or_set("k", lambda: "new")) == "pre"

    def test_protocol_aliases_forward_to_base(self) -> None:
        cache = _EphemeralCache(oauth_token="tok", email="x@example.com")
        run_coro(cache.async_set_cached_value("k", "v"))
        assert run_coro(cache.async_get_cached_value("k")) == "v"

    def test_secrets_bundle_injects_fcm_credentials(self) -> None:
        cache = _EphemeralCache(
            oauth_token=None,
            email=None,
            secrets_bundle={"fcm_credentials": {"android_id": "42"}},
        )
        assert run_coro(cache.get("fcm_credentials")) == {"android_id": "42"}

    def test_secrets_bundle_without_fcm_creds_is_logged(self) -> None:
        cache = _EphemeralCache(
            oauth_token=None,
            email=None,
            secrets_bundle={"other": "value"},
        )
        assert run_coro(cache.get("fcm_credentials")) is None


class TestApiCloseAndContributorMode:
    """``close`` unrefs the shared session; ``set_contributor_mode`` validates."""

    def test_close_clears_session_reference(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache(), session=MagicMock())
        run_coro(api.close())
        assert api._session is None

    def test_normalize_contributor_mode_known_value(self) -> None:
        assert (
            GoogleFindMyAPI._normalize_contributor_mode("  in_all_areas  ")
            == CONTRIBUTOR_MODE_IN_ALL_AREAS
        )

    def test_normalize_contributor_mode_non_string(self) -> None:
        assert (
            GoogleFindMyAPI._normalize_contributor_mode(None)
            == DEFAULT_CONTRIBUTOR_MODE
        )
        assert (
            GoogleFindMyAPI._normalize_contributor_mode(123)  # type: ignore[arg-type]
            == DEFAULT_CONTRIBUTOR_MODE
        )

    def test_set_contributor_mode_invalid_epoch_uses_now(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        api._contributor_mode_switch_epoch = 0
        api.set_contributor_mode("high_traffic", switch_epoch=-5)
        assert api._contributor_mode == CONTRIBUTOR_MODE_HIGH_TRAFFIC
        assert api._contributor_mode_switch_epoch > 0


class TestNamespaceBasics:
    """``_namespace`` reads ``entry_id`` then ``namespace`` then returns None."""

    def test_prefers_entry_id(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache(entry_id="  entry-x  "))
        assert api._namespace() == "entry-x"

    def test_falls_back_to_namespace(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache(entry_id=None, namespace="ns-1"))
        assert api._namespace() == "ns-1"

    def test_returns_none_when_absent(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache(entry_id=None, namespace=None))
        assert api._namespace() is None

    def test_returns_none_on_exception(self) -> None:
        api = GoogleFindMyAPI(cache=RaisingCache())
        assert api._namespace() is None

    def test_decoder_token_cache_returns_none_for_stub(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._decoder_token_cache() is None


# ---------------------------------------------------------------------------
# Sync-call guard / loop resolution / helper
# ---------------------------------------------------------------------------


class TestSyncCallGuardBasics:
    """Detect a running loop and log the guard message."""

    def test_returns_false_without_running_loop(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._sync_call_guard("should not log") is False

    def test_returns_true_when_loop_running(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        caplog.set_level(logging.ERROR, logger=api_module._LOGGER.name)

        async def _probe() -> bool:
            return api._sync_call_guard("guard:active loop")

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(_probe()) is True
        finally:
            loop.close()
        assert any("guard:active loop" in r.getMessage() for r in caplog.records)


class TestResolveSyncLoopBasics:
    """Session-bound vs. fresh-loop resolution paths."""

    def test_session_without_loop_raises(self) -> None:
        api = GoogleFindMyAPI(
            cache=StubCache(),
            session=SimpleNamespace(loop=None),
        )
        with pytest.raises(RuntimeError, match="determine the event loop"):
            api._resolve_sync_loop()

    def test_session_with_closed_loop_raises(self) -> None:
        closed = asyncio.new_event_loop()
        closed.close()
        api = GoogleFindMyAPI(
            cache=StubCache(),
            session=SimpleNamespace(loop=closed),
        )
        with pytest.raises(RuntimeError, match="closed"):
            api._resolve_sync_loop()

    def test_session_with_open_loop_returns_it(self) -> None:
        open_loop = asyncio.new_event_loop()
        try:
            api = GoogleFindMyAPI(
                cache=StubCache(),
                session=SimpleNamespace(loop=open_loop),
            )
            assert api._resolve_sync_loop() is open_loop
        finally:
            open_loop.close()

    def test_no_session_creates_and_caches_loop(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        try:
            loop_a = api._resolve_sync_loop()
            loop_b = api._resolve_sync_loop()
            assert loop_a is loop_b
        finally:
            if api._sync_loop is not None:
                api._sync_loop.close()

    def test_closed_cached_loop_is_replaced(self) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        old = asyncio.new_event_loop()
        old.close()
        api._sync_loop = old
        try:
            new_loop = api._resolve_sync_loop()
            assert new_loop is not old
            assert not new_loop.is_closed()
        finally:
            if api._sync_loop is not None:
                api._sync_loop.close()


class TestRunSyncHelperBasics:
    """Guard short-circuit / resolve failure / running loop / coroutine errors."""

    def test_returns_default_when_guard_active(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        caplog.set_level(logging.ERROR, logger=api_module._LOGGER.name)

        async def _probe() -> Any:
            async def _factory() -> str:
                return "ok"

            return api._run_sync_helper(
                _factory,
                guard_message="guard:short",
                context="ctx",
                default="default",
            )

        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(_probe()) == "default"
        finally:
            loop.close()

    def test_returns_default_when_resolve_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = GoogleFindMyAPI(
            cache=StubCache(),
            session=SimpleNamespace(loop=None),
        )
        caplog.set_level(logging.ERROR, logger=api_module._LOGGER.name)

        async def _factory() -> str:
            return "unused"

        assert (
            api._run_sync_helper(
                _factory,
                guard_message="g",
                context="ctx",
                default="def",
            )
            == "def"
        )
        assert any("sync setup" in r.getMessage() for r in caplog.records)

    def test_returns_default_when_target_loop_already_running(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        running = MagicMock(spec=asyncio.AbstractEventLoop)
        running.is_running.return_value = True
        running.is_closed.return_value = False
        api = GoogleFindMyAPI(cache=StubCache())
        monkeypatch.setattr(api, "_resolve_sync_loop", lambda: running)
        caplog.set_level(logging.ERROR, logger=api_module._LOGGER.name)

        async def _factory() -> str:
            return "unused"

        assert (
            api._run_sync_helper(
                _factory, guard_message="g", context="ctx", default="def"
            )
            == "def"
        )

    def test_coroutine_exception_returns_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = GoogleFindMyAPI(cache=StubCache())
        caplog.set_level(logging.ERROR, logger=api_module._LOGGER.name)

        async def _factory() -> str:
            raise RuntimeError("inner boom")

        try:
            result = api._run_sync_helper(
                _factory, guard_message="g", context="ctx", default="def"
            )
        finally:
            if api._sync_loop is not None:
                api._sync_loop.close()
        assert result == "def"
        assert any("inner boom" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# FCM token retrieval (action / quiet)
# ---------------------------------------------------------------------------


class TestFcmTokenForActionBasics:
    """All branches of :meth:`_get_fcm_token_for_action`."""

    def test_no_provider_returns_none(self) -> None:
        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._get_fcm_token_for_action() is None

    def test_entry_scoped_token_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = FakeReceiver(token="entry-token-1234")
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="entry-x"))
        assert api._get_fcm_token_for_action() == "entry-token-1234"

    def test_provider_typeerror_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(token="legacy-token-12345")
        install_receiver_provider(monkeypatch, receiver, accepts_entry=False)
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._get_fcm_token_for_action() == "legacy-token-12345"

    def test_provider_legacy_failure_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        install_receiver_provider(
            monkeypatch,
            None,
            accepts_entry=False,
            raise_on_legacy_call=RuntimeError("legacy boom"),
        )
        api = GoogleFindMyAPI(cache=StubCache())
        caplog.set_level(logging.ERROR, logger=api_module._LOGGER.name)
        assert api._get_fcm_token_for_action() is None
        assert any("legacy path" in r.getMessage() for r in caplog.records)

    def test_provider_general_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(
            monkeypatch,
            None,
            raise_on_call=RuntimeError("provider boom"),
        )
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._get_fcm_token_for_action() is None

    def test_receiver_none_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_receiver_provider(monkeypatch, None)
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._get_fcm_token_for_action() is None

    def test_receiver_typeerror_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(
            token="receiver-legacy-1234",
            accepts_entry=False,
        )
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._get_fcm_token_for_action() == "receiver-legacy-1234"

    def test_receiver_legacy_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(
            accepts_entry=False,
            raise_on_get_legacy=RuntimeError("legacy receiver boom"),
        )
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._get_fcm_token_for_action() is None

    def test_receiver_general_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(raise_on_get=RuntimeError("receiver boom"))
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._get_fcm_token_for_action() is None

    def test_short_token_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = FakeReceiver(token="abc")
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._get_fcm_token_for_action() is None

    def test_cache_entry_id_exception_falls_back_to_legacy_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empirie: api.py:Z624-Z638 (_namespace) wraps BOTH getattr calls in a single
        # try/except Exception. When entry_id raises, the whole function returns None
        # — the namespace attribute is NEVER reached. _get_fcm_token_for_action then
        # passes None to the provider, which falls through to the legacy receiver
        # call (no args). Author's mental model (entry_id raises → namespace used) is
        # wrong; the defensive wrapper short-circuits BOTH attribute reads.
        receiver = FakeReceiver(token="legacy-token-1234567")
        calls = install_receiver_provider(monkeypatch, receiver)
        cache = RaisingCache()
        cache.namespace = "never-reached-because-entry_id-raises"  # type: ignore[attr-defined]
        api = GoogleFindMyAPI(cache=cache)
        assert api._get_fcm_token_for_action() == "legacy-token-1234567"
        assert calls and calls[0] == (None,)

    def test_namespace_fallback_used_when_entry_id_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empirie: api.py:Z624-Z638 (_namespace) — when getattr(cache, "entry_id")
        # returns None WITHOUT raising, the `or getattr(cache, "namespace", None)`
        # branch is reached and resolves to "ns-via-fallback". The provider is then
        # called entry-scoped with that resolved namespace.
        receiver = FakeReceiver(token="ns-token-1234567")
        calls = install_receiver_provider(monkeypatch, receiver)
        cache = StubCache(entry_id=None, namespace="ns-via-fallback")
        api = GoogleFindMyAPI(cache=cache)
        assert api._get_fcm_token_for_action() == "ns-token-1234567"
        assert calls and calls[0] == ("ns-via-fallback",)


class TestPeekFcmTokenQuietlyBasics:
    """Quiet probe mirrors action-token paths but logs at DEBUG only."""

    def test_no_provider_returns_none(self) -> None:
        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._peek_fcm_token_quietly() is None

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = FakeReceiver(token="peek-token-12345")
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._peek_fcm_token_quietly() == "peek-token-12345"

    def test_provider_typeerror_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(token="peek-legacy-12345")
        install_receiver_provider(monkeypatch, receiver, accepts_entry=False)
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._peek_fcm_token_quietly() == "peek-legacy-12345"

    def test_provider_legacy_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(
            monkeypatch,
            None,
            accepts_entry=False,
            raise_on_legacy_call=RuntimeError("peek legacy boom"),
        )
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._peek_fcm_token_quietly() is None

    def test_provider_general_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(
            monkeypatch,
            None,
            raise_on_call=RuntimeError("peek provider boom"),
        )
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._peek_fcm_token_quietly() is None

    def test_receiver_none_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_receiver_provider(monkeypatch, None)
        api = GoogleFindMyAPI(cache=StubCache())
        assert api._peek_fcm_token_quietly() is None

    def test_receiver_typeerror_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(
            token="receiver-peek-12345",
            accepts_entry=False,
        )
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._peek_fcm_token_quietly() == "receiver-peek-12345"

    def test_receiver_legacy_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(
            accepts_entry=False,
            raise_on_get_legacy=RuntimeError("peek receiver legacy"),
        )
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._peek_fcm_token_quietly() is None

    def test_receiver_general_exception_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(raise_on_get=RuntimeError("peek receiver boom"))
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._peek_fcm_token_quietly() is None

    def test_short_token_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = FakeReceiver(token="ab")
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api._peek_fcm_token_quietly() is None


# ---------------------------------------------------------------------------
# Push readiness / can_play_sound
# ---------------------------------------------------------------------------


class TestIsPushReadyBasics:
    """All decision branches of :meth:`is_push_ready`."""

    def test_returns_false_without_provider(self) -> None:
        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache())
        assert api.is_push_ready() is False

    def test_returns_false_when_receiver_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(monkeypatch, None)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.is_push_ready() is False

    def test_returns_value_of_is_ready_boolean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=True)
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.is_push_ready() is True

    def test_returns_true_when_push_client_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=None, pc=make_pc(do_listen=True))
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.is_push_ready() is True

    def test_returns_false_when_push_client_not_listening(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=None, pc=make_pc(do_listen=False))
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.is_push_ready() is False

    def test_returns_false_when_no_signals_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=None, pc=None)
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.is_push_ready() is False

    def test_provider_typeerror_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=True)
        install_receiver_provider(monkeypatch, receiver, accepts_entry=False)
        api = GoogleFindMyAPI(cache=StubCache())
        assert api.is_push_ready() is True

    def test_provider_general_exception_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(
            monkeypatch, None, raise_on_call=RuntimeError("push provider boom")
        )
        api = GoogleFindMyAPI(cache=StubCache())
        assert api.is_push_ready() is False

    def test_provider_legacy_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(
            monkeypatch,
            None,
            accepts_entry=False,
            raise_on_legacy_call=RuntimeError("push legacy boom"),
        )
        api = GoogleFindMyAPI(cache=StubCache())
        assert api.is_push_ready() is False

    def test_push_ready_property_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = FakeReceiver(is_ready=True)
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.push_ready is True


class TestCanPlaySoundBasics:
    """Capability cache + push-ready gate decide the verdict."""

    def test_returns_false_when_push_not_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_receiver_provider(monkeypatch, None)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        api._device_capabilities["dev"] = True
        assert api.can_play_sound("dev") is False

    def test_returns_cached_value_when_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=True)
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        api._device_capabilities["dev"] = False
        assert api.can_play_sound("dev") is False

    def test_returns_none_when_capability_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = FakeReceiver(is_ready=True)
        install_receiver_provider(monkeypatch, receiver)
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert api.can_play_sound("unknown") is None


# ---------------------------------------------------------------------------
# Async error mapping: device list / location / play / stop
# ---------------------------------------------------------------------------


def _make_api_with_session() -> GoogleFindMyAPI:
    return GoogleFindMyAPI(cache=StubCache(entry_id="entry-1"), session=None)


class TestAsyncBasicDeviceListErrorMapping:
    """Each documented exception class is mapped to UpdateFailed/AuthFailed."""

    def _patch_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        exc: BaseException,
    ) -> None:
        async def _raise(*_a: Any, **_k: Any) -> str:
            raise exc

        monkeypatch.setattr(api_module, "async_request_device_list", _raise)

    def test_rate_limit_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaRateLimitError("rate"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    @pytest.mark.parametrize("status", [401, 403])
    def test_http_401_403_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        self._patch_request(monkeypatch, NovaHTTPError(status, "http"))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_basic_device_list())

    def test_http_500_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaHTTPError(500, "server"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    def test_nova_auth_error_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Transient NovaAuthError (is_permanent=False) records the generic code.
        self._patch_request(monkeypatch, NovaAuthError(401, "auth"))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed) as excinfo:
            run_coro(api.async_get_basic_device_list())
        assert (
            getattr(excinfo.value, "reauth_code", None)
            is ReauthReasonCode.NOVA_AUTH_FAILED
        )

    @pytest.mark.parametrize(
        "err",
        [
            NovaAuthError(401, "perm-flag", is_permanent=True),
            api_module.NovaAuthPermanentError(401, "perm-subclass"),
        ],
    )
    def test_permanent_nova_auth_error_records_permanent_code(
        self, monkeypatch: pytest.MonkeyPatch, err: NovaAuthError
    ) -> None:
        # Cross-site consistency (RV-G2e): the device-list path must record the
        # same permanent code the location path uses for a permanent failure,
        # not the generic NOVA_AUTH_FAILED. Mirrors api.py location handler.
        self._patch_request(monkeypatch, err)
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed) as excinfo:
            run_coro(api.async_get_basic_device_list())
        assert (
            getattr(excinfo.value, "reauth_code", None)
            is ReauthReasonCode.NOVA_AUTH_PERMANENT
        )

    def test_protobuf_decode_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaProtobufDecodeError("decode"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    def test_logic_error_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaLogicError(message="logic", code=1))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    @pytest.mark.parametrize(
        "marker",
        ["BadAuthentication", "Missing 'Token' in gpsoauth", "Bad Authentication"],
    )
    def test_bad_auth_runtime_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch, marker: str
    ) -> None:
        self._patch_request(monkeypatch, RuntimeError(marker))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_basic_device_list())

    def test_token_cache_closed_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, RuntimeError("TokenCache is closed"))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_basic_device_list())

    def test_multi_entry_guard_message_maps_to_update_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._patch_request(
            monkeypatch, RuntimeError("Multiple config entries active here")
        )
        api = _make_api_with_session()
        caplog.set_level(logging.INFO, logger=api_module._LOGGER.name)
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())
        assert any(
            "multiple config entries" in r.getMessage().lower() for r in caplog.records
        )

    def test_generic_runtime_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, RuntimeError("other transient"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    def test_client_error_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, ClientError("net"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    def test_unexpected_exception_maps_to_update_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, Exception("boom"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())

    def test_a_non_credential_4xx_on_the_device_list_is_not_a_reauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected REQUEST must not read as a rejected sign-in.

        The transport raises NovaAuthError for every non-retryable 4xx, so a
        device removed from the account arrived here as "your sign-in expired"
        and produced an immediate re-auth prompt with no threshold in front of
        it. It takes the same exit a 5xx takes one branch up.
        """

        self._patch_request(monkeypatch, NovaAuthError(404, "gone"))
        api = _make_api_with_session()
        with pytest.raises(UpdateFailed) as excinfo:
            run_coro(api.async_get_basic_device_list())
        assert not hasattr(excinfo.value, "reauth_code")

    def test_a_non_credential_4xx_on_the_device_list_does_not_log_an_authentication_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._patch_request(monkeypatch, NovaAuthError(404, "gone"))
        api = _make_api_with_session()
        with caplog.at_level(logging.DEBUG), pytest.raises(UpdateFailed):
            run_coro(api.async_get_basic_device_list())
        assert "Device list rejected by the server (HTTP 404)" in caplog.text
        assert "Authentication failed" not in caplog.text
        # The level is part of the user-visible effect: HA's log panel and its
        # error counter treat ERROR differently from WARNING, so a silent
        # promotion would put a deleted device back among the alarms.
        levels = {
            r.levelno
            for r in caplog.records
            if "Device list rejected by the server (HTTP 404)" in r.message
        }
        assert levels == {logging.WARNING}

    def test_a_credential_rejection_on_the_device_list_still_starts_reauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counterpart: 403 must keep reaching the re-auth flow.

        Without this row the previous two are satisfied by a branch that never
        raises ConfigEntryAuthFailed at all, which would bury a real expired
        sign-in instead of the reverse.
        """

        self._patch_request(monkeypatch, NovaAuthError(403, "denied"))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed) as excinfo:
            run_coro(api.async_get_basic_device_list())
        assert (
            getattr(excinfo.value, "reauth_code", None)
            is ReauthReasonCode.NOVA_AUTH_FAILED
        )

    def test_the_sound_classifier_follows_the_shared_predicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_classify_nova_auth_error must READ the predicate, not re-derive it.

        A second copy of the status test is exactly how the sound handlers and
        the four other handlers drifted apart in the first place. Patching the
        predicate to lie proves the classifier asks it.
        """

        monkeypatch.setattr(api_module, "is_credential_rejection", lambda _e: False)
        assert (
            api_module._classify_nova_auth_error(NovaAuthError(401, "x"))
            is SoundDispatchOutcome.REJECTED_SERVER
        )


class TestAsyncDeviceLocationErrorMapping:
    """Each documented exception class for the location request branch."""

    def _patch_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        exc: BaseException,
    ) -> None:
        async def _raise(*_a: Any, **_k: Any) -> list[Any]:
            raise exc

        monkeypatch.setattr(api_module, "get_location_data_for_device", _raise)

    def test_nova_auth_permanent_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(
            monkeypatch, api_module.NovaAuthPermanentError(401, "perm-auth")
        )
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_device_location("d", "name"))

    def test_nova_auth_transient_reraised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaAuthError(401, "transient"))
        api = _make_api_with_session()
        with pytest.raises(NovaAuthError):
            run_coro(api.async_get_device_location("d", "name"))

    def test_nova_auth_permanent_via_flag_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(
            monkeypatch,
            NovaAuthError(401, "perm-flag", is_permanent=True),
        )
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_device_location("d", "name"))

    @pytest.mark.parametrize("status", [401, 403])
    def test_http_401_403_maps_to_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        self._patch_request(monkeypatch, NovaHTTPError(status, "http"))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_device_location("d", "name"))

    def test_http_500_returns_empty_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_request(monkeypatch, NovaHTTPError(500, "server"))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    def test_rate_limit_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaRateLimitError("rate"))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    def test_protobuf_decode_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaProtobufDecodeError("decode"))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    def test_logic_error_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, NovaLogicError(message="logic", code=2))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    def test_client_error_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, ClientError("net"))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    def test_fcm_startup_runtime_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(
            monkeypatch,
            RuntimeError("FCM receiver provider has not been registered yet"),
        )
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    def test_generic_runtime_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, RuntimeError("other"))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}

    @pytest.mark.parametrize(
        "exc",
        [
            SharedKeyMismatchError("stale shared key"),
            StaleOwnerKeyError("tracker key outdated"),
            DecryptionError("generic decrypt failure"),
        ],
    )
    def test_decryption_error_is_reraised_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, exc: DecryptionError
    ) -> None:
        """Audit finding A1: ``DecryptionError`` is a ``RuntimeError`` subclass, so the
        broad ``except RuntimeError`` / ``except Exception`` handlers would otherwise
        swallow an auth-fatal stale-shared-key failure into an empty dict. It must
        propagate instead so the coordinator can escalate to a reauth flow."""
        self._patch_request(monkeypatch, exc)
        api = _make_api_with_session()
        with pytest.raises(DecryptionError):
            run_coro(api.async_get_device_location("d", "name"))

    def test_unexpected_exception_returns_empty_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_request(monkeypatch, ValueError("boom"))
        api = _make_api_with_session()
        assert run_coro(api.async_get_device_location("d", "name")) == {}


class TestAsyncPlaySoundErrorMapping:
    """Each documented exception class for the play-sound branch."""

    def _api_with_token(self, monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyAPI:
        receiver = FakeReceiver(token="play-token-1234567")
        install_receiver_provider(monkeypatch, receiver)
        return GoogleFindMyAPI(cache=StubCache(entry_id="e"))

    def _patch_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        result: Any,
        *,
        raises: BaseException | None = None,
    ) -> None:
        """Install a fake submitter that models the acceptance contract.

        The real submitter returns a result tuple *only* when the server accepted
        the command (HTTP 200) and re-raises on every other outcome. The fake
        mirrors that single contract: it returns ``result`` to model an accepted
        command, or raises ``raises`` to model any non-acceptance — a pre-dispatch
        failure (nothing sent), a connection-setup error, or a server rejection
        (401/403/5xx, reached the wire but refused). There is no out-of-band
        dispatch signal to model anymore.
        """

        async def _submit(*_a: Any, **_k: Any) -> Any:
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(api_module, "async_submit_start_sound_request", _submit)

    def _patch_generate_uuid(
        self, monkeypatch: pytest.MonkeyPatch, value: str = "uuid-injected"
    ) -> None:
        """Pin the client-generated cancel key so acceptance paths are testable."""
        monkeypatch.setattr(api_module, "generate_random_uuid", lambda: value)

    def test_missing_token_short_circuits(self) -> None:
        # PRE-dispatch guard: nothing was sent, so there is no cancel key to keep.
        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        result = run_coro(api.async_play_sound("d"))
        assert (result.accepted, result.cancel_key) == (False, None)

    def test_empty_submission_drops_cancel_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The submitter returns a tuple on every accepted (200) command and
        # re-raises otherwise, so a None result is not an expected outcome. Treat
        # it conservatively: drop the key rather than risk overwriting a previous
        # play's still-valid cancel key. See IRR-CA-CANCEL-KEY-ON-SUCCESS-ONLY.
        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        self._patch_submit(monkeypatch, None)
        result = run_coro(api.async_play_sound("d"))
        assert (result.accepted, result.cancel_key) == (False, None)

    def test_success_returns_injected_uuid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Success returns the client-generated (injected) cancel key. The real
        # submitter echoes the injected UUID back through the builder; the mock
        # mirrors that contract.
        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)

        async def _echo_submit(*_a: Any, **_k: Any) -> Any:
            return ("AB", _k.get("request_uuid"))

        monkeypatch.setattr(
            api_module, "async_submit_start_sound_request", _echo_submit
        )
        result = run_coro(api.async_play_sound("d"))
        assert (result.accepted, result.cancel_key) == (True, "uuid-injected")

    @pytest.mark.parametrize(
        "exc",
        [
            # Server rejections (reached the wire, then refused) — Codex PR #1100
            # iter-4: a 401/403/5xx is NOT an accepted command, so it must drop the
            # cancel key, never keep it.
            NovaAuthError(401, "auth"),
            NovaHTTPError(401, "http"),
            NovaHTTPError(503, "http"),
            NovaRateLimitError("rate"),
            ClientError("net"),
            Exception("boom"),
            # Pre-dispatch failures (nothing sent) — Codex PR #1100 earlier iters,
            # incl. NovaAuthPermanentError from token refresh (a NovaAuthError
            # subclass) raised before the wire.
            MissingTokenCacheError(),
            api_module.NovaAuthPermanentError(401, "token refresh failed"),
            NovaAuthError(401, "auth resolution"),
            ValueError("Username is not available for async_nova_request."),
            Exception("boom before post"),
        ],
    )
    def test_any_submitter_exception_drops_cancel_key(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        # Architectural invariant (IRR-CA-CANCEL-KEY-ON-SUCCESS-ONLY): the submitter
        # returns a tuple only for an accepted command (HTTP 200) and re-raises for
        # every non-acceptance — whether a pre-dispatch failure (nothing sent) or a
        # server rejection (401/403/5xx, reached the wire but refused). In ALL these
        # cases the play never started a ring, so async_play_sound must return
        # (False, None); returning a non-null UUID would let the coordinator
        # overwrite a still-valid cancel key of an earlier, possibly still-ringing
        # play, breaking a later Stop. This collapses the former
        # post-dispatch-keeps / pre-dispatch-drops split into one contract.
        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=exc)
        result = run_coro(api.async_play_sound("d"))
        assert (result.accepted, result.cancel_key) == (False, None)

    def test_post_dispatch_network_failure_keeps_cancel_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex PR #1100 iter-5: a network failure at/after the request reached
        # the wire (server disconnect, read timeout) may have started a ring even
        # without a 200. async_nova_request flags the wrapped NovaError as
        # dispatched=True; async_play_sound then KEEPS the client-generated cancel
        # key (success stays False) so a later Stop can target the possibly-active
        # ring. This is the one failure path that preserves the key.
        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        err = NovaError("network failed after retries")
        err.dispatched = True
        self._patch_submit(monkeypatch, None, raises=err)
        result = run_coro(api.async_play_sound("d"))
        assert (result.accepted, result.cancel_key) == (False, "uuid-injected")

    def test_pre_dispatch_network_failure_drops_cancel_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A NovaError flagged dispatched=False (a pre-connect failure: DNS,
        # refused, connect timeout) provably never rang, so the key must be
        # dropped, exactly like every other non-acceptance.
        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        err = NovaError("connect failed before the wire")
        err.dispatched = False
        self._patch_submit(monkeypatch, None, raises=err)
        result = run_coro(api.async_play_sound("d"))
        assert (result.accepted, result.cancel_key) == (False, None)

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (NovaAuthError(401, "expired"), SoundDispatchOutcome.REJECTED_AUTH),
            (NovaAuthError(403, "forbidden"), SoundDispatchOutcome.REJECTED_AUTH),
            # NovaAuthError is raised for EVERY non-retryable 4xx, not only for
            # credential rejections: nova_request.py names "403 Forbidden, 404
            # Not Found" in the very comment above the raise, and
            # HTTP_RETRY_ELIGIBLE holds no 4xx besides 408 and 429. A missing
            # device or a malformed request must therefore not be reported as
            # REJECTED_AUTH, whose contract is "refused on credentials".
            (
                NovaAuthError(404, "no such device"),
                SoundDispatchOutcome.REJECTED_SERVER,
            ),
            (NovaAuthError(400, "bad request"), SoundDispatchOutcome.REJECTED_SERVER),
            # Permanence outranks the status code: the subclass exists to say
            # "re-authentication is definitively required", so it stays AUTH
            # even if it ever carries a status outside 401/403.
            (
                api_module.NovaAuthPermanentError(404, "aas rejected"),
                SoundDispatchOutcome.REJECTED_AUTH,
            ),
            (NovaHTTPError(403, "forbidden"), SoundDispatchOutcome.REJECTED_AUTH),
            (NovaHTTPError(503, "unavailable"), SoundDispatchOutcome.REJECTED_SERVER),
            (NovaRateLimitError("slow down"), SoundDispatchOutcome.REJECTED_RATE_LIMIT),
            (NovaLogicError(3, "logic"), SoundDispatchOutcome.REJECTED_SERVER),
            (
                NovaProtobufDecodeError("garbage"),
                SoundDispatchOutcome.INTERNAL_ERROR,
            ),
            (NovaError("socket died"), SoundDispatchOutcome.TRANSPORT_FAILED),
            (ClientError("pre-dispatch"), SoundDispatchOutcome.TRANSPORT_FAILED),
            (ValueError("our own bug"), SoundDispatchOutcome.INTERNAL_ERROR),
        ],
    )
    def test_play_sound_classifies_every_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raised: Exception,
        expected: SoundDispatchOutcome,
    ) -> None:
        """Every exit of async_play_sound must name its own cause.

        A server saying no, a network that never answered and a bug on our side
        used to be the same ``(False, None)``. The coordinator read that as a
        push transport problem and armed a 90-second cooldown for all three.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=raised)
        assert run_coro(api.async_play_sound("d")).outcome is expected

    def test_a_non_auth_4xx_does_not_log_an_authentication_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The log line IS the user-visible effect of this classification.

        Both outcomes reach the same ``StopSoundOutcome.FAILED`` and the same
        ``stop_sound_rejected`` message downstream, so nothing but the log tells
        the user whether to check their sign-in or their device list. Pinning
        only the returned enum would leave the whole point of the split
        unguarded: the branch could collapse back to a single
        ``_LOGGER.error("Authentication failed ...")`` with every enum
        assertion still green. Mirrors the "assert both the warning log and the
        exception" rule in ``tests/AGENTS.md``.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=NovaAuthError(404, "gone"))
        with caplog.at_level(logging.DEBUG):
            assert (
                run_coro(api.async_play_sound("d")).outcome
                is SoundDispatchOutcome.REJECTED_SERVER
            )
        assert "Client error (HTTP 404)" in caplog.text
        assert "Authentication failed" not in caplog.text
        # The level is part of the user-visible effect: HA's log panel and its
        # error counter treat ERROR differently from WARNING, so a silent
        # promotion would put a deleted device back among the alarms.
        levels = {
            r.levelno for r in caplog.records if "Client error (HTTP 404)" in r.message
        }
        assert levels == {logging.WARNING}

    def test_a_credential_rejection_still_logs_an_authentication_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The counterpart: 403 must keep naming credentials.

        Without this row the previous test is satisfied by a branch that never
        says "Authentication failed" at all, which would bury a real expired
        sign-in instead of the reverse.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=NovaAuthError(403, "denied"))
        with caplog.at_level(logging.DEBUG):
            assert (
                run_coro(api.async_play_sound("d")).outcome
                is SoundDispatchOutcome.REJECTED_AUTH
            )
        assert "Authentication failed" in caplog.text
        assert "Client error (HTTP 403)" not in caplog.text
        levels = {
            r.levelno for r in caplog.records if "Authentication failed" in r.message
        }
        assert levels == {logging.ERROR}

    def test_auth_error_without_a_readable_status_stays_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NovaAuthError whose status cannot be read keeps the old verdict.

        Every real instance carries one (the constructor takes it positionally),
        so this is the test-double and future-subclass case. The conservative
        reading of a type named "auth" is auth, and pinning it here keeps the
        fallback from being a claim in a comment.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        err = NovaAuthError(404, "double without a status")
        # delattr, not `= None`: the annotation says int, and removing the
        # instance attribute is exactly what makes getattr fall back.
        delattr(err, "status")
        self._patch_submit(monkeypatch, None, raises=err)
        assert (
            run_coro(api.async_play_sound("d")).outcome
            is SoundDispatchOutcome.REJECTED_AUTH
        )

    def test_missing_action_token_is_not_sent(self) -> None:
        """No FCM token means no transport was used, so nobody may be blamed."""

        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        result = run_coro(api.async_play_sound("d"))
        assert result.outcome is SoundDispatchOutcome.NOT_SENT
        assert result.cancel_key is None

    def test_empty_submitter_reply_is_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The submitter returns a tuple on 200 and re-raises otherwise.

        A None therefore breaks its own contract: our bug, not an outage.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        self._patch_submit(monkeypatch, None)
        result = run_coro(api.async_play_sound("d"))
        assert result.outcome is SoundDispatchOutcome.INTERNAL_ERROR
        assert result.cancel_key is None

    def test_http_200_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Acceptance carries the cancel key, and only acceptance sets accepted."""

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)

        async def _echo_submit(*_a: Any, **_k: Any) -> Any:
            return ("AB", _k.get("request_uuid"))

        monkeypatch.setattr(
            api_module, "async_submit_start_sound_request", _echo_submit
        )
        result = run_coro(api.async_play_sound("d"))
        assert result.outcome is SoundDispatchOutcome.ACCEPTED
        assert result.accepted is True
        assert result.cancel_key == "uuid-injected"

    def test_dispatched_transport_failure_keeps_key_and_names_the_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outcome and cancel key are two independent facts, not one.

        The key answers "may the device be ringing", the outcome answers "who
        refused". Reading the cause off the key is the out-of-band channel that
        PlaySoundResult replaces.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_generate_uuid(monkeypatch)
        err = NovaError("network failed after retries")
        err.dispatched = True
        self._patch_submit(monkeypatch, None, raises=err)
        result = run_coro(api.async_play_sound("d"))
        assert result.outcome is SoundDispatchOutcome.TRANSPORT_FAILED
        assert result.cancel_key == "uuid-injected"
        assert result.accepted is False


class TestAsyncStopSoundErrorMapping:
    """Each documented exception class for the stop-sound branch."""

    def _api_with_token(self, monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyAPI:
        receiver = FakeReceiver(token="stop-token-1234567")
        install_receiver_provider(monkeypatch, receiver)
        return GoogleFindMyAPI(cache=StubCache(entry_id="e"))

    def _patch_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        result: Any,
        *,
        raises: BaseException | None = None,
    ) -> None:
        async def _submit(*_a: Any, **_k: Any) -> Any:
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(api_module, "async_submit_stop_sound_request", _submit)

    def test_missing_token_short_circuits(self) -> None:
        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        # No transport was used, so neither the server nor the network is at fault.
        assert run_coro(api.async_stop_sound("d")) is SoundDispatchOutcome.NOT_SENT

    def test_none_response_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None)
        # The submitter re-raises on every non-acceptance, so an empty reply is a
        # broken contract on our side, not an outage.
        assert (
            run_coro(api.async_stop_sound("d", "uuid-1234"))
            is SoundDispatchOutcome.INTERNAL_ERROR
        )

    def test_success_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, "CDEF")
        assert (
            run_coro(api.async_stop_sound("d", "uuid-5678"))
            is SoundDispatchOutcome.ACCEPTED
        )

    def test_stop_without_uuid_does_not_claim_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An accepted POST without a cancel key is not a success message.

        A non-empty Nova reply proves that the submission was accepted, never
        that the device stopped. Without a cancel key the server cannot even
        correlate the stop with a running ring, so an INFO reading
        "submitted successfully" is misinformation (BSkando#195).
        """

        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, "CDEF")

        with caplog.at_level(logging.DEBUG):
            assert run_coro(api.async_stop_sound("d")) is SoundDispatchOutcome.ACCEPTED

        assert "successfully" not in caplog.text
        warnings = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and "without a cancel key" in record.getMessage()
        ]
        assert warnings, "the uncorrelated submission must be logged as a warning"

    def test_stop_with_uuid_logs_the_cancel_key_branch(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Positive control: with a key the warning must NOT appear."""

        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, "CDEF")

        with caplog.at_level(logging.DEBUG):
            assert (
                run_coro(api.async_stop_sound("d", "uuid-5678"))
                is SoundDispatchOutcome.ACCEPTED
            )

        assert "cancel key present" in caplog.text
        assert "without a cancel key" not in caplog.text

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_uuid_logs_as_uncorrelated_not_as_a_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        blank: str,
    ) -> None:
        """A blank key is dropped from the proto3 payload, so it is no key.

        This entry point is public and documented for non-HA contexts, so it can
        be reached without the coordinator funnel that normalises blanks. Without
        its own guard the log would announce a cancel key that never reaches the
        wire -- the same unbacked claim, only in the log instead of the service
        result.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, "CDEF")

        with caplog.at_level(logging.DEBUG):
            assert (
                run_coro(api.async_stop_sound("d", blank))
                is SoundDispatchOutcome.ACCEPTED
            )

        assert "cancel key present" not in caplog.text
        assert "without a cancel key" in caplog.text

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (NovaAuthError(401, "auth"), SoundDispatchOutcome.REJECTED_AUTH),
            (NovaAuthError(403, "forbidden"), SoundDispatchOutcome.REJECTED_AUTH),
            # Same rule as on the play path: the exception type is wider than
            # its name, so the status decides.
            (
                NovaAuthError(404, "no such device"),
                SoundDispatchOutcome.REJECTED_SERVER,
            ),
            (NovaAuthError(400, "bad request"), SoundDispatchOutcome.REJECTED_SERVER),
            (
                api_module.NovaAuthPermanentError(404, "aas rejected"),
                SoundDispatchOutcome.REJECTED_AUTH,
            ),
            (NovaHTTPError(403, "http"), SoundDispatchOutcome.REJECTED_AUTH),
            (NovaHTTPError(500, "http"), SoundDispatchOutcome.REJECTED_SERVER),
            (NovaRateLimitError("rate"), SoundDispatchOutcome.REJECTED_RATE_LIMIT),
            (NovaLogicError(3, "logic"), SoundDispatchOutcome.REJECTED_SERVER),
            (
                NovaProtobufDecodeError("garbage"),
                SoundDispatchOutcome.INTERNAL_ERROR,
            ),
            (NovaError("socket died"), SoundDispatchOutcome.TRANSPORT_FAILED),
            (ClientError("net"), SoundDispatchOutcome.TRANSPORT_FAILED),
            (Exception("boom"), SoundDispatchOutcome.INTERNAL_ERROR),
        ],
    )
    def test_documented_exceptions_are_classified(
        self,
        monkeypatch: pytest.MonkeyPatch,
        exc: BaseException,
        expected: SoundDispatchOutcome,
    ) -> None:
        """Stop carries the same classification contract as Play.

        None of these is an acceptance, but only the two transport rows may make
        a caller arm a push cooldown. Before the contract existed all nine
        collapsed into a single ``False``.
        """

        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=exc)
        assert run_coro(api.async_stop_sound("d")) is expected

    def test_a_non_auth_4xx_does_not_log_an_authentication_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same log guard as on the play path, and for the same reason."""

        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=NovaAuthError(404, "gone"))
        with caplog.at_level(logging.DEBUG):
            assert (
                run_coro(api.async_stop_sound("d"))
                is SoundDispatchOutcome.REJECTED_SERVER
            )
        assert "Client error (HTTP 404)" in caplog.text
        assert "Authentication failed" not in caplog.text
        # The level is part of the user-visible effect: HA's log panel and its
        # error counter treat ERROR differently from WARNING, so a silent
        # promotion would put a deleted device back among the alarms.
        levels = {
            r.levelno for r in caplog.records if "Client error (HTTP 404)" in r.message
        }
        assert levels == {logging.WARNING}

    def test_a_credential_rejection_still_logs_an_authentication_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The counterpart on the stop path."""

        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=NovaAuthError(403, "denied"))
        with caplog.at_level(logging.DEBUG):
            assert (
                run_coro(api.async_stop_sound("d"))
                is SoundDispatchOutcome.REJECTED_AUTH
            )
        assert "Authentication failed" in caplog.text
        assert "Client error (HTTP 403)" not in caplog.text
        levels = {
            r.levelno for r in caplog.records if "Authentication failed" in r.message
        }
        assert levels == {logging.ERROR}

    def test_auth_error_without_a_readable_status_stays_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same fallback as on the play path, pinned on both sides."""

        api = self._api_with_token(monkeypatch)
        err = NovaAuthError(404, "double without a status")
        delattr(err, "status")
        self._patch_submit(monkeypatch, None, raises=err)
        assert run_coro(api.async_stop_sound("d")) is SoundDispatchOutcome.REJECTED_AUTH
