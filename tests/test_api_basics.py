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
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
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
        self._patch_request(monkeypatch, NovaAuthError(401, "auth"))
        api = _make_api_with_session()
        with pytest.raises(ConfigEntryAuthFailed):
            run_coro(api.async_get_basic_device_list())

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
        async def _submit(*_a: Any, **_k: Any) -> Any:
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(api_module, "async_submit_start_sound_request", _submit)

    def test_missing_token_short_circuits(self) -> None:
        api_module._FCM_ReceiverGetter = None
        api = GoogleFindMyAPI(cache=StubCache(entry_id="e"))
        assert run_coro(api.async_play_sound("d")) == (False, None)

    def test_empty_submission_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None)
        assert run_coro(api.async_play_sound("d")) == (False, None)

    def test_success_returns_uuid_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, ("AB", "uuid-1234"))
        assert run_coro(api.async_play_sound("d")) == (True, "uuid-1234")

    @pytest.mark.parametrize(
        "exc",
        [
            NovaAuthError(401, "auth"),
            NovaHTTPError(401, "http"),
            NovaHTTPError(503, "http"),
            NovaRateLimitError("rate"),
            ClientError("net"),
            Exception("boom"),
        ],
    )
    def test_documented_exceptions_return_false_none(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=exc)
        assert run_coro(api.async_play_sound("d")) == (False, None)


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
        assert run_coro(api.async_stop_sound("d")) is False

    def test_none_response_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None)
        assert run_coro(api.async_stop_sound("d", "uuid-1234")) is False

    def test_success_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, "CDEF")
        assert run_coro(api.async_stop_sound("d", "uuid-5678")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            NovaAuthError(401, "auth"),
            NovaHTTPError(403, "http"),
            NovaHTTPError(500, "http"),
            NovaRateLimitError("rate"),
            ClientError("net"),
            Exception("boom"),
        ],
    )
    def test_documented_exceptions_return_false(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        api = self._api_with_token(monkeypatch)
        self._patch_submit(monkeypatch, None, raises=exc)
        assert run_coro(api.async_stop_sound("d")) is False
