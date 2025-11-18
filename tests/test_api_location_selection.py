# tests/test_api_location_selection.py
"""Unit tests for location record selection heuristics."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import ClientSession

import custom_components.googlefindmy.api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from tests.helpers import drain_loop


class _StubCache:
    """Minimal cache satisfying the API protocol for unit tests."""

    entry_id = "test-entry"

    async def async_get_cached_value(
        self, key: str
    ) -> Any:  # pragma: no cover - not used
        return None

    async def async_set_cached_value(
        self, key: str, value: Any
    ) -> None:  # pragma: no cover - not used
        return None


def _make_api() -> GoogleFindMyAPI:
    """Helper to build the API with the lightweight stub cache."""

    return GoogleFindMyAPI(cache=_StubCache())


class _SyncHarness(GoogleFindMyAPI):
    """Test harness overriding async calls for sync-wrapper tests."""

    def __init__(self) -> None:
        super().__init__(cache=_StubCache())
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def async_get_basic_device_list(self) -> list[dict[str, Any]]:
        self.calls.append(("basic", ()))
        return [{"id": "stub"}]

    async def async_get_device_location(
        self, device_id: str, device_name: str
    ) -> dict[str, Any]:
        self.calls.append(("loc", (device_id, device_name)))
        return {"id": device_id, "name": device_name}

    async def async_play_sound(self, device_id: str) -> bool:
        self.calls.append(("play", (device_id,)))
        return True

    async def async_stop_sound(self, device_id: str) -> bool:
        self.calls.append(("stop", (device_id,)))
        return True


class _LoopCaptureHarness(GoogleFindMyAPI):
    """Harness capturing the loop used by sync wrappers when a session is provided."""

    def __init__(self, session: ClientSession) -> None:
        super().__init__(cache=_StubCache(), session=session)
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def async_get_basic_device_list(self) -> list[dict[str, Any]]:
        self.loops.append(asyncio.get_running_loop())
        return []


def test_select_best_location_prefers_owner_report() -> None:
    """Records with identical timestamps favor the owner's report."""

    api = _make_api()
    records = [
        {
            "last_seen": "1700000000",
            "is_own_report": False,
            "accuracy": 5.0,
            "tag": "network",
        },
        {
            "last_seen": "1700000000",
            "is_own_report": True,
            "accuracy": 15.0,
            "tag": "owner",
        },
        {
            "last_seen": "1699999999",
            "is_own_report": False,
            "accuracy": 1.0,
            "tag": "older",
        },
    ]

    best = api._select_best_location(records)

    assert best["tag"] == "owner"
    assert best["is_own_report"] is True


def test_select_best_location_prefers_precision_without_owner() -> None:
    """Accuracy breaks ties when ownership data is absent or false."""

    api = _make_api()
    records = [
        {
            "last_seen": 1800000000,
            "is_own_report": False,
            "accuracy": 25.0,
            "tag": "coarse",
        },
        {
            "last_seen": 1800000000,
            "is_own_report": False,
            "accuracy": 5.0,
            "tag": "precise",
        },
        {
            "last_seen": 1799999999,
            "is_own_report": False,
            "accuracy": 1.0,
            "tag": "older",
        },
    ]

    best = api._select_best_location(records)

    assert best["tag"] == "precise"
    assert best["accuracy"] == 5.0


def test_sync_wrappers_execute_without_running_loop() -> None:
    """Sync helpers execute successfully when no loop is active."""

    api = _SyncHarness()

    basic = api.get_basic_device_list()
    assert basic == [{"id": "stub"}]

    location = api.get_device_location("dev-1", "Device 1")
    assert location == {"id": "dev-1", "name": "Device 1"}

    assert api.play_sound("dev-2") is True
    assert api.stop_sound("dev-3") is True

    assert api.calls == [
        ("basic", ()),
        ("loc", ("dev-1", "Device 1")),
        ("play", ("dev-2",)),
        ("stop", ("dev-3",)),
    ]


def test_sync_wrappers_guard_when_loop_running() -> None:
    """Sync helpers refuse to run when an event loop is already active."""

    api = _SyncHarness()

    async def _runner() -> None:
        assert api.get_basic_device_list() == []
        assert api.get_device_location("dev-1", "Device 1") == {}
        assert api.play_sound("dev-2") is False
        assert api.stop_sound("dev-3") is False

    asyncio.run(_runner())
    assert api.calls == []


def test_sync_wrappers_use_provided_session_loop() -> None:
    """Sync helpers reuse the loop tied to an injected session."""

    loop = asyncio.new_event_loop()

    async def _setup() -> tuple[ClientSession, _LoopCaptureHarness]:
        session = ClientSession()
        harness = _LoopCaptureHarness(session)
        return session, harness

    session, harness = loop.run_until_complete(_setup())
    try:
        assert harness.get_basic_device_list() == []
        assert harness.loops == [loop]
    finally:
        loop.run_until_complete(session.close())
        drain_loop(loop)


def test_process_device_list_response_deduplicates_canonic_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device list processing emits a single entry per canonical ID."""

    api = _make_api()

    parsed = object()
    monkeypatch.setattr(
        api_module, "parse_device_list_protobuf", lambda hex_str: parsed
    )
    monkeypatch.setattr(
        api_module,
        "_build_can_ring_index",
        lambda msg, *, cache=None: {"device-1": True},
    )
    monkeypatch.setattr(
        api_module,
        "get_canonic_ids",
        lambda msg: [
            ("Primary", "device-1"),
            ("Alias", "device-1"),
            ("Secondary", "device-2"),
        ],
    )

    devices = api._process_device_list_response("feedface")

    assert devices == [
        {
            "name": "Primary",
            "id": "device-1",
            "device_id": "device-1",
            "can_ring": True,
        },
        {
            "name": "Secondary",
            "id": "device-2",
            "device_id": "device-2",
        },
    ]


def test_api_forwards_contributor_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contributor mode settings are passed to the location request helper."""

    captured: dict[str, Any] = {}

    async def fake_get_location_data_for_device(
        device_id: str,
        device_name: str,
        *,
        session: ClientSession | None = None,
        namespace: str | None = None,
        cache: Any,
        contributor_mode: str | None = None,
        last_mode_switch: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        captured["mode"] = contributor_mode
        captured["switch"] = last_mode_switch
        return []

    monkeypatch.setattr(
        "custom_components.googlefindmy.api.get_location_data_for_device",
        fake_get_location_data_for_device,
    )

    async def _run() -> None:
        api = GoogleFindMyAPI(
            cache=_StubCache(),
            contributor_mode="high_traffic",
            contributor_mode_switch_epoch=1_700_000_000,
        )

        await api.async_get_device_location("dev-1", "Tracker")

    asyncio.run(_run())

    assert captured["mode"] == "high_traffic"
    assert captured["switch"] == 1_700_000_000
