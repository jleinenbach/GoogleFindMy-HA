# tests/test_coordinator_main_coverage.py
"""Behavioural coverage tests for ``coordinator/main.py``.

These tests target the previously uncovered defensive/edge branches of the
``GoogleFindMyCoordinator`` glue methods (stats/sound-uuid persistence,
device-list refresh dispatch, setup/shutdown/unload, snapshot fallbacks,
presence/purge accessors and push readiness).

Following the repo convention (see ``tests/test_coordinator_sound_uuid.py``)
the coordinator is constructed via ``__new__`` and only the attributes the
method under test needs are set explicitly. Async paths use ``async def`` with
``asyncio_mode=auto`` (no ``asyncio.run``).
"""

from __future__ import annotations

import time
from datetime import UTC
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import (
    OPT_IGNORED_DEVICES,
    OPT_SEMANTIC_LOCATIONS,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.config_entries_stub import make_config_entry


def _bare() -> Any:
    """Return a coordinator instance without running ``__init__``."""
    return GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)


class _Cache:
    """Minimal async cache double implementing the CacheProtocol surface."""

    def __init__(self, store: dict[str, Any] | None = None) -> None:
        self.store: dict[str, Any] = dict(store or {})
        self.saved: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self.store.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.saved[key] = value


# ---------------------------------------------------------------------------
# _async_load_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_stats_populates_known_keys() -> None:
    """Cached integration_stats values overwrite matching keys only."""
    c = _bare()
    c.stats = {"a": 0, "b": 0}
    c._cache = _Cache({"integration_stats": {"a": 5, "unknown": 9}})
    c._sound_request_uuids = {}
    c._sound_request_timestamps = {}

    await c._async_load_stats()

    assert c.stats == {"a": 5, "b": 0}


@pytest.mark.asyncio
async def test_load_stats_load_exception_is_swallowed() -> None:
    """A cache raising on get must not propagate out of load_stats."""
    c = _bare()
    c.stats = {"a": 0}

    class _Boom:
        async def async_get_cached_value(self, key: str) -> Any:
            raise RuntimeError("boom")

    c._cache = _Boom()
    c._sound_request_uuids = {}
    c._sound_request_timestamps = {}

    await c._async_load_stats()  # must not raise
    assert c.stats == {"a": 0}


@pytest.mark.asyncio
async def test_load_stats_no_cached_sound_uuids_keeps_current() -> None:
    """Non-dict cached sound uuids leave the runtime map untouched."""
    c = _bare()
    c.stats = {}
    c._cache = _Cache({"integration_stats": {}, "sound_request_uuids": "not-a-dict"})
    c._sound_request_uuids = {"dev": "keep"}
    c._sound_request_timestamps = {"dev": 1.0}

    await c._async_load_stats()

    assert c._sound_request_uuids == {"dev": "keep"}


@pytest.mark.asyncio
async def test_load_stats_new_format_valid_and_expired() -> None:
    """New-format uuids are loaded unless expired; bad shapes are skipped."""
    now = time.time()
    c = _bare()
    c.stats = {}
    c._cache = _Cache(
        {
            "integration_stats": {},
            "sound_request_uuids": {
                "fresh": {"uuid": "u-fresh", "ts": now},
                "old": {"uuid": "u-old", "ts": now - 10_000_000},
                "empty-uuid": {"uuid": "", "ts": now},
                123: {"uuid": "bad-key", "ts": now},  # non-str device_id
            },
        }
    )
    c._sound_request_uuids = {}
    c._sound_request_timestamps = {}

    await c._async_load_stats()

    assert c._sound_request_uuids == {"fresh": "u-fresh"}
    assert c._sound_request_timestamps["fresh"] == pytest.approx(now)


@pytest.mark.asyncio
async def test_load_stats_legacy_format_discarded() -> None:
    """Legacy str-only uuids (no timestamp) are discarded on load."""
    c = _bare()
    c.stats = {}
    c._cache = _Cache(
        {"integration_stats": {}, "sound_request_uuids": {"dev": "legacy-uuid"}}
    )
    c._sound_request_uuids = {}
    c._sound_request_timestamps = {}

    await c._async_load_stats()

    assert c._sound_request_uuids == {}


@pytest.mark.asyncio
async def test_load_stats_skips_cached_empty_when_runtime_populated() -> None:
    """A cached empty map does not clobber an already-populated runtime map."""
    now = time.time()
    c = _bare()
    c.stats = {}
    # cached map contains only an expired entry -> loaded map ends up empty
    c._cache = _Cache(
        {
            "integration_stats": {},
            "sound_request_uuids": {"old": {"uuid": "u", "ts": now - 10_000_000}},
        }
    )
    c._sound_request_uuids = {"live": "u-live"}
    c._sound_request_timestamps = {"live": now}

    await c._async_load_stats()

    assert c._sound_request_uuids == {"live": "u-live"}


@pytest.mark.asyncio
async def test_load_stats_merges_loaded_and_runtime() -> None:
    """Loaded uuids merge with runtime; runtime wins on key collision."""
    now = time.time()
    c = _bare()
    c.stats = {}
    c._cache = _Cache(
        {
            "integration_stats": {},
            "sound_request_uuids": {
                "a": {"uuid": "loaded-a", "ts": now},
                "b": {"uuid": "loaded-b", "ts": now},
            },
        }
    )
    c._sound_request_uuids = {"b": "runtime-b"}
    c._sound_request_timestamps = {"b": now}

    await c._async_load_stats()

    assert c._sound_request_uuids == {"a": "loaded-a", "b": "runtime-b"}


# ---------------------------------------------------------------------------
# _async_save_stats / _async_save_sound_uuids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_stats_persists_copy() -> None:
    c = _bare()
    c.stats = {"x": 1}
    cache = _Cache()
    c._cache = cache

    await c._async_save_stats()

    assert cache.saved["integration_stats"] == {"x": 1}


@pytest.mark.asyncio
async def test_save_stats_exception_swallowed() -> None:
    c = _bare()
    c.stats = {"x": 1}

    class _Boom:
        async def async_set_cached_value(self, key: str, value: Any) -> None:
            raise RuntimeError("boom")

    c._cache = _Boom()
    await c._async_save_stats()  # must not raise


@pytest.mark.asyncio
async def test_save_sound_uuids_uses_stored_timestamp() -> None:
    c = _bare()
    cache = _Cache()
    c._cache = cache
    c._sound_request_uuids = {"dev": "u1"}
    c._sound_request_timestamps = {"dev": 42.0}

    await c._async_save_sound_uuids()

    assert cache.saved["sound_request_uuids"] == {"dev": {"uuid": "u1", "ts": 42.0}}


@pytest.mark.asyncio
async def test_save_sound_uuids_exception_swallowed() -> None:
    c = _bare()

    class _Boom:
        async def async_set_cached_value(self, key: str, value: Any) -> None:
            raise RuntimeError("boom")

    c._cache = _Boom()
    c._sound_request_uuids = {"dev": "u1"}
    c._sound_request_timestamps = {}
    await c._async_save_sound_uuids()  # must not raise


# ---------------------------------------------------------------------------
# request_device_list_refresh
# ---------------------------------------------------------------------------


def test_request_device_list_refresh_off_loop_redispatches() -> None:
    """When not on the hass loop the call is re-dispatched onto it."""
    c = _bare()
    c._is_on_hass_loop = lambda: False
    calls: list[dict[str, Any]] = []
    c._run_on_hass_loop = lambda fn, **kw: calls.append(kw)

    c.request_device_list_refresh(reason="r", schedule_refresh=True)

    assert calls == [{"reason": "r", "schedule_refresh": True}]


def test_request_device_list_refresh_sets_flag_and_schedules() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: True
    c._force_device_list_refresh = False
    c._force_device_list_reason = None
    c._entry_id = lambda: "entry-1"
    dispatched: list[dict[str, Any]] = []
    c._dispatch_async_request_refresh = lambda **kw: dispatched.append(kw)

    c.request_device_list_refresh(reason="boot", schedule_refresh=True)

    assert c._force_device_list_refresh is True
    assert c._force_device_list_reason == "boot"
    assert len(dispatched) == 1


def test_request_device_list_refresh_no_schedule_when_disabled() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: True
    c._force_device_list_refresh = True  # already set -> skip flag branch
    c._force_device_list_reason = "prev"
    c._entry_id = lambda: None
    dispatched: list[Any] = []
    c._dispatch_async_request_refresh = lambda **kw: dispatched.append(kw)

    c.request_device_list_refresh(reason=None, schedule_refresh=False)

    assert dispatched == []
    assert c._force_device_list_reason == "prev"


# ---------------------------------------------------------------------------
# _api_push_ready
# ---------------------------------------------------------------------------


def test_api_push_ready_cooldown_active() -> None:
    c = _bare()
    c._push_cooldown_until = time.monotonic() + 1000
    c._push_ready_memo = True
    c.api = SimpleNamespace()

    assert c._api_push_ready() is False
    assert c._push_ready_memo is False


def test_api_push_ready_is_push_ready_callable() -> None:
    c = _bare()
    c._push_cooldown_until = 0.0
    c._push_ready_memo = None
    c.api = SimpleNamespace(is_push_ready=lambda: True)

    assert c._api_push_ready() is True
    assert c._push_ready_memo is True


def test_api_push_ready_bool_attribute_fallback() -> None:
    c = _bare()
    c._push_cooldown_until = 0.0
    c._push_ready_memo = None
    c.api = SimpleNamespace(push_ready=False)

    assert c._api_push_ready() is False


def test_api_push_ready_fcm_nested_attribute() -> None:
    c = _bare()
    c._push_cooldown_until = 0.0
    c._push_ready_memo = None
    c.api = SimpleNamespace(fcm=SimpleNamespace(is_ready=True))

    assert c._api_push_ready() is True


def test_api_push_ready_optimistic_default() -> None:
    c = _bare()
    c._push_cooldown_until = 0.0
    c._push_ready_memo = None
    c.api = SimpleNamespace()  # nothing resolvable

    assert c._api_push_ready() is True


def test_api_push_ready_exception_defaults_true() -> None:
    c = _bare()
    c._push_cooldown_until = 0.0
    c._push_ready_memo = None

    class _Api:
        @property
        def is_push_ready(self) -> Any:
            raise RuntimeError("boom")

    c.api = _Api()

    assert c._api_push_ready() is True


# ---------------------------------------------------------------------------
# Presence / device accessors
# ---------------------------------------------------------------------------


def test_get_device_last_seen_returns_none_when_missing() -> None:
    c = _bare()
    c._device_location_data = {}
    assert c.get_device_last_seen("dev") is None


def test_get_device_name_map_is_copy() -> None:
    c = _bare()
    src = {"dev": "Name"}
    c._ensure_device_name_cache = lambda: src
    result = c.get_device_name_map()
    assert result == src
    assert result is not src


def test_get_absent_device_ids_filters_present() -> None:
    c = _bare()
    now = time.monotonic()
    c._presence_ttl_s = 100.0
    c._ensure_device_name_cache = lambda: {"present": "P", "absent": "A"}
    c._device_location_data = {}
    c._present_last_seen = {"present": now}  # fresh -> not absent

    assert c.get_absent_device_ids() == ["absent"]


# ---------------------------------------------------------------------------
# async_setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_sets_cache_namespace_and_subscribes() -> None:
    c = _bare()
    cache = SimpleNamespace()  # no entry_id attr
    c._cache = cache
    c.config_entry = make_config_entry(entry_id="entry-42", data={}, options={})
    reindex: list[bool] = []
    ensure: list[bool] = []
    c._ensure_service_device_exists = lambda: ensure.append(True)
    c._reindex_poll_targets_from_device_registry = lambda: reindex.append(True)
    c._dr_unsub = None
    listens: list[str] = []
    c.hass = SimpleNamespace(
        bus=SimpleNamespace(
            async_listen=lambda ev, cb: (listens.append(ev), "unsub")[1]
        )
    )

    await c.async_setup()

    assert cache.entry_id == "entry-42"
    assert ensure and reindex
    assert c._dr_unsub == "unsub"
    assert listens  # subscribed to device-registry updates


@pytest.mark.asyncio
async def test_async_setup_namespace_failure_swallowed() -> None:
    c = _bare()

    class _Cache:
        # setattr raising simulates a read-only cache object
        entry_id = None

        def __setattr__(self, name: str, value: Any) -> None:
            raise RuntimeError("read-only")

    c._cache = _Cache()
    c.config_entry = make_config_entry(entry_id="e", data={}, options={})
    c._ensure_service_device_exists = lambda: None
    c._reindex_poll_targets_from_device_registry = lambda: None
    c._dr_unsub = "already"  # skip re-subscribe branch
    c.hass = SimpleNamespace(bus=SimpleNamespace(async_listen=lambda *a: "x"))

    await c.async_setup()  # must not raise
    assert c._dr_unsub == "already"


# ---------------------------------------------------------------------------
# _get_google_home_filter
# ---------------------------------------------------------------------------


def test_get_google_home_filter_reads_runtime_data() -> None:
    c = _bare()
    gh = object()
    c.config_entry = SimpleNamespace(
        runtime_data=SimpleNamespace(google_home_filter=gh)
    )
    assert c._get_google_home_filter() is gh


def test_get_google_home_filter_none_when_absent() -> None:
    c = _bare()
    c.config_entry = SimpleNamespace(runtime_data=None)
    assert c._get_google_home_filter() is None


# ---------------------------------------------------------------------------
# async_shutdown / _async_unload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_shutdown_cancels_handles_and_unloads() -> None:
    c = _bare()
    cancelled: list[str] = []
    c._cancel_pending_subentry_repair = lambda: None
    c._dr_unsub = lambda: cancelled.append("dr")
    c._short_retry_cancel = lambda: cancelled.append("retry")

    class _Task:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            cancelled.append("stats")

    c._stats_save_task = _Task()
    c._eid_refresh_debounce_handle = SimpleNamespace(
        cancel=lambda: cancelled.append("eid1")
    )
    c._eid_inline_refresh_debounce_handle = SimpleNamespace(
        cancel=lambda: cancelled.append("eid2")
    )
    unloaded: list[bool] = []

    async def _unload() -> None:
        unloaded.append(True)

    c._async_unload = _unload  # type: ignore[assignment]

    await c.async_shutdown()

    assert set(cancelled) == {"dr", "retry", "stats", "eid1", "eid2"}
    assert c._dr_unsub is None
    assert c._short_retry_cancel is None
    assert unloaded


@pytest.mark.asyncio
async def test_async_shutdown_swallows_unsub_errors() -> None:
    c = _bare()
    c._cancel_pending_subentry_repair = lambda: None

    def _boom() -> None:
        raise RuntimeError("boom")

    c._dr_unsub = _boom
    c._short_retry_cancel = _boom
    c._stats_save_task = None
    c._eid_refresh_debounce_handle = SimpleNamespace(cancel=_boom)
    c._eid_inline_refresh_debounce_handle = None

    async def _unload() -> None:
        return None

    c._async_unload = _unload  # type: ignore[assignment]

    await c.async_shutdown()  # must not raise
    assert c._dr_unsub is None


@pytest.mark.asyncio
async def test_async_unload_stops_resolver_and_closes_api() -> None:
    c = _bare()
    events: list[str] = []
    c._unsub_scheduler = lambda: events.append("sched")
    c.eid_resolver = SimpleNamespace(stop=lambda: events.append("stop"))

    async def _close() -> None:
        events.append("close")

    c.api = SimpleNamespace(close=_close)

    await c._async_unload()

    assert events == ["sched", "stop", "close"]
    assert c._unsub_scheduler is None


# ---------------------------------------------------------------------------
# _get_ignored_set / is_ignored
# ---------------------------------------------------------------------------


def test_get_ignored_set_from_options() -> None:
    c = _bare()
    c.config_entry = make_config_entry(
        entry_id="e", data={}, options={OPT_IGNORED_DEVICES: {"dev-a": True}}
    )
    assert c._get_ignored_set() == {"dev-a"}
    assert c.is_ignored("dev-a") is True


def test_get_ignored_set_attr_list_fallback() -> None:
    c = _bare()
    c.config_entry = None
    c.ignored_devices = ["x", "y", 5]  # non-str filtered
    assert c._get_ignored_set() == {"x", "y"}


def test_get_ignored_set_empty_default() -> None:
    c = _bare()
    c.config_entry = None
    c.ignored_devices = None
    assert c._get_ignored_set() == set()


# ---------------------------------------------------------------------------
# note_error / metric getters
# ---------------------------------------------------------------------------


def test_note_error_appends_triple() -> None:
    c = _bare()
    from collections import deque

    c.recent_errors = deque(maxlen=10)
    c.note_error(ValueError("bad"), where="poll", device="dev-1")
    assert len(c.recent_errors) == 1
    _ts, err_type, msg = c.recent_errors[0]
    assert err_type == "ValueError"
    assert "poll(dev-1)" in msg


def test_get_metric_and_durations() -> None:
    c = _bare()
    c.performance_metrics = {
        "setup_start_monotonic": 10.0,
        "setup_end_monotonic": 12.5,
        "bogus": "not-a-number",
    }
    assert c.get_metric("setup_start_monotonic") == 10.0
    assert c.get_metric("bogus") is None
    assert c.get_metric("missing") is None
    assert c.get_setup_duration_seconds() == pytest.approx(2.5)


def test_get_recent_errors_formats() -> None:
    c = _bare()
    from collections import deque

    c.recent_errors = deque([(1.0, "ValueError", "boom")])
    result = c.get_recent_errors()
    assert isinstance(result, list) and result


# ---------------------------------------------------------------------------
# _update_entry_from_cache / _build_snapshot_from_cache
# ---------------------------------------------------------------------------


def test_update_entry_from_cache_no_data() -> None:
    c = _bare()
    c._device_location_data = {}
    entry = {"device_id": "dev"}
    assert c._update_entry_from_cache(entry, wall_now=100.0) is False


def test_update_entry_from_cache_metadata_only() -> None:
    c = _bare()
    c._device_location_data = {"dev": {"metadata_only": True}}
    entry = {"device_id": "dev"}
    assert c._update_entry_from_cache(entry, wall_now=100.0) is True
    assert entry["status"] == "Anchor metadata cached"


def test_update_entry_from_cache_non_mapping() -> None:
    c = _bare()
    c.location_poll_interval = 300
    c._device_location_data = {"dev": ["not", "a", "mapping"]}
    entry = {"device_id": "dev"}
    assert c._update_entry_from_cache(entry, wall_now=100.0) is True
    # Non-mapping cache seeds the anchor status then recomputes from age=wall_now.
    assert "status" in entry


def test_update_entry_from_cache_with_location_status() -> None:
    c = _bare()
    c.location_poll_interval = 300
    c._device_location_data = {"dev": {"last_updated": 90.0, "latitude": 1.0}}
    entry = {"device_id": "dev"}
    assert c._update_entry_from_cache(entry, wall_now=100.0) is True
    assert "status" in entry


def test_build_snapshot_from_cache_uses_helpers() -> None:
    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    snap = c._build_snapshot_from_cache([{"id": "d1", "name": "One"}], wall_now=1.0)
    assert snap == [{"device_id": "d1", "name": "One"}]


# ---------------------------------------------------------------------------
# _apply_semantic_mapping
# ---------------------------------------------------------------------------


def test_apply_semantic_mapping_no_semantic_name() -> None:
    c = _bare()
    assert c._apply_semantic_mapping({}) is False


def test_apply_semantic_mapping_replayed_skipped() -> None:
    c = _bare()
    assert (
        c._apply_semantic_mapping({"semantic_name": "Home", "is_replayed": True})
        is False
    )


def test_apply_semantic_mapping_from_data_fallback() -> None:
    c = _bare()
    c.config_entry = SimpleNamespace(
        options={},
        data={
            OPT_SEMANTIC_LOCATIONS: {
                "Home": {"latitude": 52.0, "longitude": 13.0}  # no accuracy -> default
            }
        },
    )
    payload = {"semantic_name": "home"}
    assert c._apply_semantic_mapping(payload) is True
    assert payload["latitude"] == 52.0
    assert payload["location_type"] == "trusted"


def test_apply_semantic_mapping_no_match_returns_false() -> None:
    c = _bare()
    c.config_entry = SimpleNamespace(
        options={
            OPT_SEMANTIC_LOCATIONS: {"Office": {"latitude": 1.0, "longitude": 2.0}}
        },
        data={},
    )
    assert c._apply_semantic_mapping({"semantic_name": "Nowhere"}) is False


def test_apply_semantic_mapping_skips_invalid_mapping_entries() -> None:
    c = _bare()
    c.config_entry = SimpleNamespace(
        options={
            OPT_SEMANTIC_LOCATIONS: {
                42: {"latitude": 1.0, "longitude": 2.0},  # non-str name -> skip
                "Bad": "not-a-mapping",  # non-Mapping coords -> skip
                "Home": {"latitude": 52.0, "longitude": 13.0},
            }
        },
        data={},
    )
    payload = {"semantic_name": "home"}
    assert c._apply_semantic_mapping(payload) is True
    assert payload["latitude"] == 52.0


# ---------------------------------------------------------------------------
# increment_stat / _refresh_canonicless_drop_stats / _debounced_save_stats
# ---------------------------------------------------------------------------


def test_increment_stat_known_on_loop() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: True
    c.stats = {"polls": 0}
    scheduled: list[bool] = []
    c._schedule_stats_persist = lambda: scheduled.append(True)
    c.increment_stat("polls")
    assert c.stats["polls"] == 1
    assert scheduled


def test_increment_stat_unknown_warns() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: True
    c.stats = {"polls": 0}
    c._schedule_stats_persist = lambda: None
    c.increment_stat("does-not-exist")  # warning branch, no raise
    assert c.stats == {"polls": 0}


def test_increment_stat_off_loop_redispatch() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: False
    calls: list[Any] = []
    c._run_on_hass_loop = lambda fn, name: calls.append(name)
    c.increment_stat("polls")
    assert calls == ["polls"]


def test_refresh_canonicless_drop_stats(monkeypatch: Any) -> None:
    import custom_components.googlefindmy.coordinator.main as main_mod

    c = _bare()
    c.stats = {
        "canonicless_drop_total": 0,
        "canonicless_drop_benign": 0,
        "canonicless_drop_warn": 0,
    }
    c._schedule_stats_persist = lambda: None
    monkeypatch.setattr(
        main_mod.decoder,
        "get_canonicless_counts",
        lambda entry_id: {"total": 5, "benign": 3, "warn": 2},
    )
    c._refresh_canonicless_drop_stats("entry-1")
    assert c.stats["canonicless_drop_total"] == 5
    assert c.stats["canonicless_drop_warn"] == 2


def test_refresh_canonicless_drop_stats_no_entry_id() -> None:
    c = _bare()
    c._refresh_canonicless_drop_stats(None)  # no-op, no raise


@pytest.mark.asyncio
async def test_debounced_save_stats_exception_swallowed(monkeypatch: Any) -> None:
    c = _bare()
    c._stats_debounce_seconds = 0

    async def _boom() -> None:
        raise RuntimeError("save failed")

    c._async_save_stats = _boom  # type: ignore[assignment]
    await c._debounced_save_stats()  # must not raise


# ---------------------------------------------------------------------------
# push_updated
# ---------------------------------------------------------------------------


def _push_ready(c: Any) -> dict[str, list[Any]]:
    """Wire the collaborators push_updated needs; return a call sink."""
    sink: dict[str, list[Any]] = {"index": [], "store": [], "set": []}
    c._is_on_hass_loop = lambda: True
    c._set_fcm_status = lambda status: None
    c._last_poll_mono = 0.0
    c._device_location_data = {}
    c._get_ignored_set = set
    c._present_last_seen = {}
    c._build_snapshot_from_cache = lambda devices, wall_now: [
        {"device_id": d["id"], "name": d["name"]} for d in devices
    ]
    c._refresh_subentry_index = sink["index"].append
    c._store_subentry_snapshots = sink["store"].append
    c.async_set_updated_data = sink["set"].append
    return sink


def test_push_updated_off_loop_redispatch() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: False
    calls: list[Any] = []
    c._run_on_hass_loop = lambda fn, ids, reset_baseline: calls.append(
        (ids, reset_baseline)
    )
    c.push_updated(["dev"], reset_baseline=False)
    assert calls == [(["dev"], False)]


def test_push_updated_explicit_ids_and_merge() -> None:
    c = _bare()
    sink = _push_ready(c)
    c._ensure_device_name_cache = lambda: {"dev1": "My Phone"}
    c.data = [{"device_id": "old", "name": "Old"}]
    c.push_updated(["dev1"], reset_baseline=True)
    published = sink["set"][-1]
    ids = {row["device_id"] for row in published}
    assert ids == {"old", "dev1"}  # merge preserves non-pushed device
    assert c._present_last_seen["dev1"] > 0


def test_push_updated_all_ids_and_ignored_filter() -> None:
    c = _bare()
    sink = _push_ready(c)
    c._ensure_device_name_cache = lambda: {"keep": "Keep", "drop": "Drop"}
    c._device_location_data = {"keep": {}, "drop": {}}
    c._get_ignored_set = lambda: {"drop"}
    c.data = []
    c.push_updated(None, reset_baseline=False)
    published = sink["set"][-1]
    ids = {row["device_id"] for row in published}
    assert "drop" not in ids
    assert "keep" in ids


def test_push_updated_blank_name_uses_default() -> None:
    c = _bare()
    _push_ready(c)
    # name cache maps the id to a blank string -> default device name is used
    c._ensure_device_name_cache = lambda: {"dev1": "   "}
    stubs: list[dict[str, Any]] = []
    c._build_snapshot_from_cache = lambda devices, wall_now: (
        stubs.extend(devices)
        or [{"device_id": d["id"], "name": d["name"]} for d in devices]
    )
    c.data = []
    c.push_updated(["dev1"], reset_baseline=False)
    assert stubs[0]["name"] == "Google Find My Device"


# ---------------------------------------------------------------------------
# purge_device
# ---------------------------------------------------------------------------


def _purge_ready(c: Any) -> dict[str, list[Any]]:
    sink: dict[str, list[Any]] = {"set": [], "saved": []}
    c._is_on_hass_loop = lambda: True
    c._device_location_data = {"dev": {"x": 1}}
    c._device_last_good_location = {"dev": {}}
    c._round_trip_anchors = {"dev": {"lat": 1.0, "lon": 2.0, "ts": 3.0}}
    c._device_update_history = {"dev": []}
    c._device_interval_history = {"dev": []}
    c._ensure_device_name_cache = lambda: {"dev": "Name"}
    c._device_caps = {"dev": {}}
    c._locate_inflight = {"dev"}
    c._locate_cooldown_until = {"dev": 1.0}
    c._sound_request_uuids = {}
    c._sound_request_timestamps = {"dev": 1.0}
    c._device_poll_cooldown_until = {"dev": 1.0}
    c._present_device_ids = {"dev"}
    c._present_last_seen = {"dev": 1.0}
    c.data = [{"device_id": "dev", "name": "Name"}, {"device_id": "other"}]
    c._refresh_subentry_index = lambda stub: None
    c._store_subentry_snapshots = lambda snap: None
    c.async_set_updated_data = sink["set"].append
    return sink


def test_purge_device_off_loop_redispatch() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: False
    calls: list[Any] = []
    c._run_on_hass_loop = lambda fn, dev: calls.append(dev)
    c.purge_device("dev")
    assert calls == ["dev"]


def test_purge_device_removes_and_republishes() -> None:
    c = _bare()
    sink = _purge_ready(c)
    # include a non-dict row to exercise the data-hygiene skip branch
    c.data = [{"device_id": "dev", "name": "Name"}, "junk-row", {"device_id": "other"}]
    c.purge_device("dev")
    published = sink["set"][-1]
    ids = {row.get("device_id") for row in published}
    assert ids == {"other"}
    assert "dev" not in c._device_location_data


def test_purge_device_saves_sound_uuids_when_present() -> None:
    c = _bare()
    _purge_ready(c)
    c._sound_request_uuids = {"dev": "uuid-1"}  # triggers async save
    created: list[Any] = []

    async def _save() -> None:
        return None

    c._async_save_sound_uuids = _save  # type: ignore[assignment]
    c.hass = SimpleNamespace(
        async_create_task=lambda coro: (created.append(coro), coro.close())[0]
    )
    c.purge_device("dev")
    assert created  # save scheduled


# ---------------------------------------------------------------------------
# _async_build_device_snapshot_with_fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_fallback_cache_hit() -> None:
    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: True  # cache hit -> continue
    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["device_id"] == "d1"


@pytest.mark.asyncio
async def test_snapshot_fallback_registry_miss() -> None:
    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    c._find_tracker_entity_entry = lambda dev_id: (
        None
    )  # registry miss -> append+continue
    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["name"] == "One"


@pytest.mark.asyncio
async def test_snapshot_fallback_missing_entity_id() -> None:
    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    c._find_tracker_entity_entry = lambda dev_id: SimpleNamespace(
        entity_id="", unique_id="u"
    )
    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["device_id"] == "d1"


@pytest.mark.asyncio
async def test_snapshot_fallback_uses_current_state() -> None:
    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    c._find_tracker_entity_entry = lambda dev_id: SimpleNamespace(
        entity_id="device_tracker.d1", unique_id="u"
    )
    from datetime import datetime

    state = SimpleNamespace(
        attributes={"latitude": 1.0, "longitude": 2.0, "accuracy_m": 5.0},
        last_updated=datetime(2026, 1, 1, tzinfo=UTC),
    )
    c.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: state))
    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["latitude"] == 1.0
    assert snap[0]["status"] == "Using current state"


@pytest.mark.asyncio
async def test_snapshot_fallback_no_state_no_history() -> None:
    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    c._find_tracker_entity_entry = lambda dev_id: SimpleNamespace(
        entity_id="device_tracker.d1", unique_id="u"
    )
    c.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    c.allow_history_fallback = False
    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["device_id"] == "d1"


# ---------------------------------------------------------------------------
# _schedule_stats_persist
# ---------------------------------------------------------------------------


def test_schedule_stats_persist_on_loop_creates_task() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: True
    c._stats_save_task = None
    created: list[Any] = []

    def _create(coro: Any, name: str = "") -> Any:
        coro.close()
        created.append(name)
        return SimpleNamespace(done=lambda: True, cancel=lambda: None)

    c.hass = SimpleNamespace(
        async_create_background_task=_create,
        async_create_task=_create,  # eager getattr default needs this attribute
    )
    c._schedule_stats_persist()
    assert created  # a debounced task was scheduled


def test_schedule_stats_persist_cancels_pending_and_falls_back_to_create_task() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: True
    cancelled: list[bool] = []
    c._stats_save_task = SimpleNamespace(
        done=lambda: False, cancel=lambda: cancelled.append(True)
    )
    created: list[Any] = []

    def _create(coro: Any, name: str = "") -> Any:
        coro.close()
        created.append(name)
        return SimpleNamespace()

    # No async_create_background_task -> getattr falls back to async_create_task
    c.hass = SimpleNamespace(async_create_task=_create)
    c._schedule_stats_persist()
    assert cancelled and created


def test_schedule_stats_persist_off_loop_redispatch() -> None:
    c = _bare()
    c._is_on_hass_loop = lambda: False
    calls: list[Any] = []
    c._run_on_hass_loop = calls.append
    c._schedule_stats_persist()
    assert calls  # marshalled onto the loop


# ---------------------------------------------------------------------------
# _async_build_device_snapshot_with_fallbacks - history fallback path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_fallback_history_used(monkeypatch: Any) -> None:
    import custom_components.googlefindmy.coordinator.main as main_mod

    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    c._find_tracker_entity_entry = lambda dev_id: SimpleNamespace(
        entity_id="device_tracker.d1", unique_id="u"
    )
    c.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    c.allow_history_fallback = True
    incremented: list[str] = []
    c.increment_stat = incremented.append

    class _Recorder:
        async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
            return {"latitude": 9.0, "longitude": 8.0, "status": "History fallback"}

    monkeypatch.setattr(main_mod, "get_recorder", lambda hass: _Recorder())

    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["latitude"] == 9.0
    assert incremented == ["history_fallback_used"]


@pytest.mark.asyncio
async def test_snapshot_fallback_history_empty(monkeypatch: Any) -> None:
    import custom_components.googlefindmy.coordinator.main as main_mod

    c = _bare()
    c._build_base_snapshot_entry = lambda dev: {
        "device_id": dev["id"],
        "name": dev["name"],
    }
    c._update_entry_from_cache = lambda entry, wall_now: False
    c._find_tracker_entity_entry = lambda dev_id: SimpleNamespace(
        entity_id="device_tracker.d1", unique_id="u"
    )
    c.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None))
    c.allow_history_fallback = True
    c.increment_stat = lambda name: None

    class _Recorder:
        async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
            return None  # no history

    monkeypatch.setattr(main_mod, "get_recorder", lambda hass: _Recorder())

    snap = await c._async_build_device_snapshot_with_fallbacks(
        [{"id": "d1", "name": "One"}]
    )
    assert snap[0]["device_id"] == "d1"
