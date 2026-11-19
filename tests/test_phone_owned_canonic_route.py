# tests/test_phone_owned_canonic_route.py
"""A phone listed with a canonical ID is a locate target when it offers action 4.

``owned_phone_target`` names such a phone: type ``IDENTIFIER_ANDROID``, a usable
first canonical ID in ``phoneInformation.canonicIds`` (the entry the decoder row
uses), a set Android ID and the phone locate action in ``supportedActions``.

Every Android ID here is synthetic: drawn at run time, never a measured value.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.NovaApi import nova_request
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder

_ALL_ACTIONS = (1, 2, 4, 5, 6, 7, 13, 14)

# Invented, distinct Android IDs of the observed shape (19 digits). A counter instead
# of ``secrets``: another test module may leave ``secrets.randbelow`` patched.
_ANDROID_IDS = itertools.count(2_345_678_901_234_567_891, 6_007)


@pytest.fixture(autouse=True)
def _reset_decoder_state() -> None:
    decoder._reset_canonicless_warning_state()


def _android_id() -> int:
    return next(_ANDROID_IDS)


def _action_bytes(number: int) -> bytes:
    action = DeviceUpdate_pb2.DeviceMetadata.SupportedActions()
    getattr(action.type, f"phoneActionField{number}").SetInParent()
    return action.SerializeToString()


def _phone(
    android_id: int,
    *,
    phone_canonic: tuple[str, ...] = (),
    direct_canonic: str | None = None,
    id_type: int = DeviceUpdate_pb2.IDENTIFIER_ANDROID,
    actions: tuple[int, ...] = _ALL_ACTIONS,
) -> DeviceUpdate_pb2.DeviceMetadata:
    device = DeviceUpdate_pb2.DeviceMetadata()
    device.userDefinedDeviceName = "Phone"
    ident = device.identifierInformation
    ident.type = id_type
    ident.phoneInformation.phoneInformationField1 = android_id
    for value in phone_canonic:
        ident.phoneInformation.canonicIds.canonicId.add().id = value
    if direct_canonic:
        ident.canonicIds.canonicId.add().id = direct_canonic
    for number in actions:
        device.supportedActions.append(_action_bytes(number))
    return device


def _tracker(canonic: str) -> DeviceUpdate_pb2.DeviceMetadata:
    device = DeviceUpdate_pb2.DeviceMetadata()
    device.userDefinedDeviceName = "Tag"
    device.identifierInformation.type = DeviceUpdate_pb2.IDENTIFIER_SPOT
    device.identifierInformation.canonicIds.canonicId.add().id = canonic
    return device


def _rows(device: DeviceUpdate_pb2.DeviceMetadata) -> list[dict[str, Any]]:
    listing = DeviceUpdate_pb2.DevicesList()
    listing.deviceMetadata.add().CopyFrom(device)
    return decoder.get_devices_with_location(listing)


# --- The predicate, as a table ----------------------------------------------------


_POSITIVE: dict[str, Any] = {
    "one canonic id": lambda aid: _phone(aid, phone_canonic=("cid-one",)),
    "two canonic ids": lambda aid: _phone(aid, phone_canonic=("cid-first", "cid-2nd")),
    "action 4 only": lambda aid: _phone(aid, phone_canonic=("cid-four",), actions=(4,)),
}

_NEGATIVE: dict[str, Any] = {
    "android id 0": lambda aid: _phone(0, phone_canonic=("cid-zero",)),
    "no action 4": lambda aid: _phone(
        aid, phone_canonic=("cid-no4",), actions=(1, 2, 5)
    ),
    "no actions": lambda aid: _phone(aid, phone_canonic=("cid-none",), actions=()),
    "canonic id only direct": lambda aid: _phone(aid, direct_canonic="cid-direct"),
    "type spot": lambda aid: _phone(
        aid, phone_canonic=("cid-spot",), id_type=DeviceUpdate_pb2.IDENTIFIER_SPOT
    ),
    "tracker": lambda aid: _tracker("cid-tag"),
    "phone without canonic id": _phone,
    "empty first id": lambda aid: _phone(aid, phone_canonic=("",)),
    "empty first, valid second": lambda aid: _phone(
        aid, phone_canonic=("", "cid-second")
    ),
}


@pytest.mark.parametrize("case", sorted(_POSITIVE))
def test_positive_cases_name_the_first_canonic_id_and_the_android_id(
    case: str,
) -> None:
    aid = _android_id()
    device = _POSITIVE[case](aid)
    first = device.identifierInformation.phoneInformation.canonicIds.canonicId[0].id
    assert decoder.owned_phone_target(device) == (first, aid)


@pytest.mark.parametrize("case", sorted(_NEGATIVE))
def test_negative_cases_are_no_target(case: str) -> None:
    assert decoder.owned_phone_target(_NEGATIVE[case](_android_id())) is None


def test_the_canonicless_phone_stays_with_its_own_predicate() -> None:
    aid = _android_id()
    device = _phone(aid)
    assert decoder.owned_phone_target(device) is None
    assert decoder.canonicless_android_id(device) == aid


# --- Consistency with the decoder row ---------------------------------------------


@pytest.mark.parametrize("case", sorted(_POSITIVE))
def test_target_id_is_the_row_id(case: str) -> None:
    device = _POSITIVE[case](_android_id())
    target = decoder.owned_phone_target(device)
    rows = _rows(device)
    assert target is not None
    assert [row["id"] for row in rows] == [target[0]]


@pytest.mark.parametrize("case", ["empty first id", "empty first, valid second"])
def test_no_row_means_no_target(case: str) -> None:
    device = _NEGATIVE[case](_android_id())
    assert _rows(device) == []
    assert decoder.owned_phone_target(device) is None


# --- Foreign shapes and the log ---------------------------------------------------


def _shaped(android_id: Any, canonic: Any = "cid-shaped") -> Any:
    ids = SimpleNamespace(canonicId=[SimpleNamespace(id=canonic)])
    phone_info = SimpleNamespace(canonicIds=ids, phoneInformationField1=android_id)
    ident = SimpleNamespace(
        type=DeviceUpdate_pb2.IDENTIFIER_ANDROID, phoneInformation=phone_info
    )
    return SimpleNamespace(
        identifierInformation=ident, supportedActions=[_action_bytes(4)]
    )


def test_foreign_shapes_are_no_target() -> None:
    assert decoder.owned_phone_target(_shaped(True)) is None
    assert decoder.owned_phone_target(_shaped("12345")) is None
    assert decoder.owned_phone_target(_shaped(7, canonic=b"cid")) is None
    assert decoder.owned_phone_target(_shaped(7)) == ("cid-shaped", 7)


def test_an_unreadable_entry_logs_only_the_error_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "field-value-that-must-not-leak"

    class _Raising:
        @property
        def identifierInformation(self) -> Any:
            raise ValueError(secret)

    with caplog.at_level(logging.DEBUG, logger=decoder.__name__):
        assert decoder.owned_phone_target(_Raising()) is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("ValueError" in message for message in messages)
    assert not any(secret in message for message in messages)


def test_an_entry_whose_actions_cannot_be_read_is_no_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "action-value-that-must-not-leak"

    class _RaisingActions:
        identifierInformation = _shaped(7).identifierInformation

        @property
        def supportedActions(self) -> Any:
            raise RuntimeError(secret)

    with caplog.at_level(logging.DEBUG, logger=decoder.__name__):
        assert decoder.owned_phone_target(_RaisingActions()) is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("RuntimeError" in message for message in messages)
    assert not any(secret in message for message in messages)


def test_one_entry_whose_actions_raise_does_not_break_the_list(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "action-value-that-must-not-leak"

    def _raise(_device: Any) -> frozenset[int]:
        raise RuntimeError(secret)

    monkeypatch.setattr(decoder, "supported_action_numbers", _raise)
    api = _owned_api(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        rows = api._process_device_list_response(
            _listing(
                _phone(_android_id(), phone_canonic=("cid-own",)), _tracker("cid-tag")
            )
        )
    assert {row["id"] for row in rows} == {"cid-own", "cid-tag"}
    assert api._phone_targets == {}
    assert not any(secret in record.getMessage() for record in caplog.records)


def test_the_android_id_never_reaches_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    aid = _android_id()
    with caplog.at_level(logging.DEBUG):
        for build in (*_POSITIVE.values(), *_NEGATIVE.values()):
            decoder.owned_phone_target(build(aid))
    assert all(str(aid) not in record.getMessage() for record in caplog.records)


# --- The target table and the locate routing in the API ---------------------------


class _TokenCache:
    """A cache the API treats as a real token cache (patched in below)."""

    entry_id = "owned-entry"

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self.data.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.data[key] = value


def _listing(*devices: DeviceUpdate_pb2.DeviceMetadata) -> str:
    listing = DeviceUpdate_pb2.DevicesList()
    for device in devices:
        listing.deviceMetadata.add().CopyFrom(device)
    return listing.SerializeToString().hex()


def _owned_api(
    monkeypatch: pytest.MonkeyPatch, *, token_cache: bool = True
) -> GoogleFindMyAPI:
    cache = _TokenCache()
    api = GoogleFindMyAPI(cache=cast(Any, cache))
    monkeypatch.setattr(
        api, "_decoder_token_cache", lambda: cache if token_cache else None
    )
    return api


@contextlib.contextmanager
def _labelled(kind: str | None) -> Iterator[None]:
    """Run the block under the send-site label ``kind`` (``None``: no label)."""
    if kind is None:
        yield
        return
    with nova_request.observe_request_dispatch(lambda: None, kind=kind):
        yield


class _Routes:
    """Stand-ins for both locate routes; records which one a request took.

    The tracker stand-in calls the dispatch observer where the real request
    POSTs, so the coordinator stamps the request as it would on the wire, and it
    records the send-site label the API saw.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.phone: list[dict[str, Any]] = []
        self.tracker: list[str] = []
        self.tracker_kinds: list[str | None] = []
        monkeypatch.setattr(api_module, "locate_phone", self._phone)
        monkeypatch.setattr(api_module, "get_location_data_for_device", self._tracker)

    async def _phone(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.phone.append(kwargs)
        return []

    async def _tracker(self, device_id: str, *_a: Any, **_k: Any) -> list[Any]:
        self.tracker.append(device_id)
        self.tracker_kinds.append(nova_request.current_dispatch_kind())
        nova_request._notify_request_dispatch()
        return []


def _ready(api: GoogleFindMyAPI, monkeypatch: pytest.MonkeyPatch) -> object:
    receiver = object()
    monkeypatch.setattr(api, "_phone_fcm_receiver", lambda _entry: receiver)

    async def _channel_ready(*_a: Any) -> bool:
        return True

    monkeypatch.setattr(api, "_async_phone_channel_ready", _channel_ready)
    return receiver


def test_api_lists_an_owned_phone_as_a_target_without_the_route_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = _android_id()
    api = _owned_api(monkeypatch)
    rows = api._process_device_list_response(
        _listing(
            _phone(aid, phone_canonic=("cid-own",)),
            _phone(_android_id(), phone_canonic=("cid-no4",), actions=(1, 2, 5)),
            _tracker("cid-tag"),
        )
    )
    by_id = {row["id"]: row for row in rows}
    assert api._phone_targets == {"cid-own": aid}
    # Its automatic request is the tracker request, so the row carries no marker.
    assert "locate_route" not in by_id["cid-own"]
    assert "locate_route" not in by_id["cid-no4"]
    assert "locate_route" not in by_id["cid-tag"]
    assert str(aid) not in repr(rows)


def test_both_phone_classes_share_the_table_with_disjoint_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy.const import PHONE_ID_PREFIX

    owned_aid, canonicless_aid = _android_id(), _android_id()
    api = _owned_api(monkeypatch)
    api._phone_id_key = bytes(range(32))
    rows = api._process_device_list_response(
        _listing(
            _phone(owned_aid, phone_canonic=("cid-own",)),
            _phone(canonicless_aid),
        )
    )
    canonicless = [r["id"] for r in rows if r["id"].startswith(PHONE_ID_PREFIX)]
    assert len(canonicless) == 1
    assert api._phone_targets == {
        "cid-own": owned_aid,
        canonicless[0]: canonicless_aid,
    }
    # Only the phone without a canonical ID is asked with action 4 automatically.
    assert {r["id"]: r.get("locate_route") for r in rows} == {
        "cid-own": None,
        canonicless[0]: "phone",
    }


def test_without_a_token_cache_there_is_no_target_and_no_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _owned_api(monkeypatch, token_cache=False)
    rows = api._process_device_list_response(
        _listing(_phone(_android_id(), phone_canonic=("cid-flow",)))
    )
    assert api._phone_targets == {}
    assert [row.get("locate_route") for row in rows] == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["manual", "auto", None])
async def test_an_owned_phone_takes_the_phone_path_only_for_a_manual_request(
    monkeypatch: pytest.MonkeyPatch, kind: str | None
) -> None:
    aid = _android_id()
    api = _owned_api(monkeypatch)
    receiver = _ready(api, monkeypatch)
    sink = mock.Mock()
    api._phone_status_sink = sink
    routes = _Routes(monkeypatch)
    api._process_device_list_response(
        _listing(
            _phone(aid, phone_canonic=("cid-own",)),
            _phone(_android_id(), phone_canonic=("cid-no4",), actions=(1, 2, 5)),
            _tracker("cid-tag"),
        )
    )

    with _labelled(kind):
        for device_id in ("cid-own", "cid-no4", "cid-tag"):
            assert await api.async_get_device_location(device_id, "Device") == {}

    if kind != "manual":
        # Automatic and unlabelled requests stay silent: the tracker request.
        assert routes.tracker == ["cid-own", "cid-no4", "cid-tag"]
        assert routes.phone == []
        return
    assert routes.tracker == ["cid-no4", "cid-tag"]
    assert len(routes.phone) == 1
    call = routes.phone[0]
    assert call["phone_id"] == "cid-own"
    assert call["android_id"] == aid
    assert call["receiver"] is receiver
    assert call["status_sink"] is sink


@pytest.mark.asyncio
async def test_a_later_list_without_action_4_returns_to_the_tracker_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = _android_id()
    api = _owned_api(monkeypatch)
    _ready(api, monkeypatch)
    routes = _Routes(monkeypatch)
    api._process_device_list_response(_listing(_phone(aid, phone_canonic=("cid-own",))))
    rows = api._process_device_list_response(
        _listing(_phone(aid, phone_canonic=("cid-own",), actions=(1, 2, 5)))
    )

    await api.async_get_device_location("cid-own", "Phone")

    assert api._phone_targets == {}
    assert "locate_route" not in rows[0]
    assert routes.phone == []
    assert routes.tracker == ["cid-own"]


@pytest.mark.asyncio
async def test_an_owned_phone_puts_only_action_4_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_phone_locate_flow as flow

    rx = flow._receiver({flow._ENTRY_A: flow._TOKEN_A})
    nova = flow._answering_nova(rx)
    flow._install_nova(monkeypatch, nova)
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda _entry: rx)
    api = GoogleFindMyAPI(cache=flow._Cache(flow._ENTRY_A))
    api._phone_targets["cid-own"] = _android_id()

    with _labelled("manual"):
        best = await api.async_get_device_location("cid-own", "Phone")

    assert best["latitude"] == pytest.approx(flow._LAT)
    assert best["_decrypt_proven"] is False
    assert [flow._raw_action_numbers(p) for p, _ in nova.calls] == [{4}]


# --- Ring and stop keep the tracker route ------------------------------------------


@pytest.mark.asyncio
async def test_ring_and_stop_of_an_owned_phone_keep_their_tracker_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_phone_ring_paths as ring

    wire = ring._Wire()
    ring._install_wire(monkeypatch, wire)
    receiver = ring._Receiver()
    api = ring._api(monkeypatch, receiver)
    api._phone_targets["cid-own"] = _android_id()

    await api.async_play_sound("cid-own")
    await api.async_stop_sound("cid-own", "cancel-1")

    requests = [ring._decode(p) for p in wire.payloads]
    assert [ring._raw_fields(r.action.SerializeToString()) for r in requests] == [
        [31],
        [32],
    ]
    assert all(r.scope.device.canonicId.id == "cid-own" for r in requests)
    assert all(not r.scope.HasField("phone") for r in requests)
    assert receiver.ready_calls == []


@pytest.mark.asyncio
async def test_the_coordinator_treats_an_owned_phone_by_the_tracker_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_phone_ring_paths as ring

    wire = ring._Wire()
    ring._install_wire(monkeypatch, wire)
    api = ring._api(monkeypatch, ring._Receiver())
    api._phone_targets["cid-own"] = _android_id()
    coordinator = ring._coordinator(api, caps={})
    c = cast(Any, coordinator)
    c._device_location_data = {"cid-own": {}}
    c._ensure_device_name_cache = lambda: {"cid-own": "Phone"}

    async def _phone_stop(_device_id: str) -> Any:
        raise AssertionError("an owned phone must not take the phone stop path")

    c._async_stop_phone_sound = _phone_stop

    # Tracker rule: optimistic for a known device without cached capability; the
    # phone rule would answer False here.
    assert coordinator.can_play_sound("cid-own") is True
    assert coordinator.can_stop_sound("cid-own") is True
    await coordinator.async_stop_sound("cid-own")
    assert [
        ring._raw_fields(ring._decode(p).action.SerializeToString())
        for p in wire.payloads
    ] == [[32]]


# --- Cycle, switch, open request and age rule for an owned phone -------------------
#
# The cycle asks the owned phone like a tracker: its row carries no marker
# ``locate_route``, so the phone floor does not hold it, and the API sends an
# automatic request over the tracker path. The switch is read per row, the
# register locks per phone row and the cache drops an older fix.

_OWNED = {"id": "cid-own", "name": "Phone"}
_OWNED_TRACKER = {"id": "cid-tag", "name": "Tag"}


@pytest.mark.asyncio
async def test_the_cycle_asks_an_owned_phone_like_a_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows from the real list parse, the real API, stand-ins for both paths.

    The tracker stand-in stamps the request like the request layer, so a floor
    on the phone would hold the second cycle back.
    """
    from tests import test_phone_auto_poll_floor as floor

    api = _owned_api(monkeypatch)
    _ready(api, monkeypatch)
    routes = _Routes(monkeypatch)
    rows = api._process_device_list_response(
        _listing(_phone(_android_id(), phone_canonic=("cid-own",)), _tracker("cid-tag"))
    )
    assert api.has_phone_target("cid-own") is True
    coord, _ = floor._coordinator(monkeypatch, interval=300, devices=rows, api=api)
    coord.api = api
    monkeypatch.setattr(api, "is_push_ready", lambda: True, raising=False)

    await floor._cycle(coord, 0.0)
    await floor._cycle(coord, 300.0)

    assert routes.phone == []
    assert routes.tracker.count("cid-own") == 2
    assert routes.tracker.count("cid-tag") == 2
    # The cycle's label reaches the API through asyncio.wait_for.
    assert set(routes.tracker_kinds) == {"auto"}


def _owned_wire(
    monkeypatch: pytest.MonkeyPatch, nova_factory: Any = None
) -> tuple[GoogleFindMyAPI, Any, Any]:
    """A real API with the owned phone in its table and a recording transport."""
    from tests import test_phone_locate_flow as flow

    rx = flow._receiver({flow._ENTRY_A: flow._TOKEN_A})
    nova = (nova_factory or flow._answering_nova)(rx)
    flow._install_nova(monkeypatch, nova)
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda _entry: rx)
    api = GoogleFindMyAPI(cache=flow._Cache(flow._ENTRY_A))
    api._phone_targets["cid-own"] = _android_id()
    return api, nova, rx


def _tracker_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the tracker request with a stand-in that stamps and records."""
    asked: list[str] = []

    async def _tracker(device_id: str, *_a: Any, **_k: Any) -> list[Any]:
        asked.append(device_id)
        nova_request._notify_request_dispatch()
        return []

    monkeypatch.setattr(api_module, "get_location_data_for_device", _tracker)
    return asked


@pytest.mark.asyncio
async def test_switch_off_sends_no_automatic_action_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_phone_auto_poll_floor as floor
    from tests import test_phone_locate_flow as flow

    real_api, nova, _rx = _owned_wire(monkeypatch)
    asked = _tracker_recorder(monkeypatch)
    coord, _ = floor._coordinator(
        monkeypatch,
        interval=300,
        options={"automatic_polling_disabled": ["cid-own"]},
        devices=[_OWNED],
        api=real_api,
    )
    coord.api = real_api

    await floor._cycle(coord, 0.0)
    await floor._cycle(coord, 5000.0)

    assert [flow._raw_action_numbers(p) for p, _ in nova.calls] == []
    assert asked == []

    # Positive control: switched on, the same cycle asks the tracker path once.
    coord_on, _ = floor._coordinator(
        monkeypatch, interval=300, devices=[_OWNED], api=real_api
    )
    coord_on.api = real_api
    await floor._cycle(coord_on, 0.0)

    assert asked == ["cid-own"]
    assert nova.calls == []


@pytest.mark.asyncio
async def test_switch_off_during_the_cycle_skips_the_owned_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cycle was planned with the phone; it is switched off by its turn.

    The planning filter cannot see this; only the live check per row can.
    """
    from tests import test_phone_auto_poll_floor as floor
    from tests import test_phone_locate_flow as flow
    from tests.test_coordinator_automatic_polling import (
        nova_request_module,
        polling_module,
    )

    real_api, nova, _rx = _owned_wire(monkeypatch)
    asked = _tracker_recorder(monkeypatch)
    coord, _ = floor._coordinator(
        monkeypatch, interval=300, devices=[_OWNED_TRACKER, _OWNED], api=real_api
    )

    class _Front:
        """Tracker answered in place; the owned phone goes to the real API."""

        def is_push_ready(self) -> bool:
            return True

        async def async_get_device_location(self, dev_id: str, name: str) -> Any:
            if dev_id != "cid-tag":
                return await real_api.async_get_device_location(dev_id, name)
            options = dict(coord.config_entry.options)
            options["automatic_polling_disabled"] = ["cid-own"]
            coord.config_entry.options = options
            nova_request_module._notify_request_dispatch()
            return {}

    coord.api = _Front()
    with mock.patch.object(polling_module, "time", floor._ShiftedTime(0.0)):
        await coord._async_start_poll_cycle(
            [dict(_OWNED_TRACKER), dict(_OWNED)], force=True
        )
    await flow._drain_background(coord)

    assert asked == []
    assert nova.calls == []


@pytest.mark.asyncio
async def test_manual_locate_with_the_switch_off_sends_one_action_4(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tests import test_phone_locate_flow as flow
    from tests.helpers.config_entries_stub import make_config_entry
    from tests.helpers.locate_mixin_stub import LocateStub

    real_api, nova, _rx = _owned_wire(monkeypatch)
    entry = make_config_entry(
        entry_id=flow._ENTRY_A, options={"automatic_polling_disabled": ["cid-own"]}
    )
    coord = LocateStub(config_entry=entry, api=real_api)
    # The stub replaces the locate call with a mock; give the real one back.
    del real_api.async_get_device_location
    caplog.set_level(logging.DEBUG)

    await coord.async_locate_device("cid-own")

    assert [flow._raw_action_numbers(p) for p, _ in nova.calls] == [{4}]
    assert any(
        r.getMessage() == f"Phone action=4 trigger=manual entry={flow._ENTRY_A}"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_service_call_locates_an_owned_phone_with_action_4(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The service ``locate_device``, also from a user's automation, is manual."""
    from custom_components.googlefindmy import services
    from tests import test_phone_locate_flow as flow
    from tests import test_services_handler_contracts as contracts
    from tests.helpers.config_entries_stub import make_config_entry
    from tests.helpers.locate_mixin_stub import LocateStub

    real_api, nova, _rx = _owned_wire(monkeypatch)
    asked = _tracker_recorder(monkeypatch)
    coord = LocateStub(
        config_entry=make_config_entry(entry_id=flow._ENTRY_A), api=real_api
    )
    del real_api.async_get_device_location
    # The service resolves the device through the coordinator's display name.
    cast(Any, coord).get_device_display_name = lambda _device_id: "Phone"
    ctx = {
        "domain": services.DOMAIN,
        "resolve_canonical": mock.Mock(return_value=("cid-own", "Phone")),
        "is_active_entry": mock.Mock(return_value=True),
        "primary_active_entry": mock.Mock(return_value=None),
        "opt": mock.Mock(side_effect=lambda _entry, _key, default: default),
        "default_map_view_token_expiration": False,
        "opt_map_view_token_expiration_key": "map_view_token_expiration",
        "redact_url_token": mock.Mock(return_value="<redacted-url>"),
        "soft_migrate_entry": mock.AsyncMock(),
        "migrate_unique_ids": mock.AsyncMock(),
        "relink_button_devices": mock.AsyncMock(),
        "relink_subentry_entities": mock.AsyncMock(),
        "coalesce_account_entries": mock.AsyncMock(),
        "extract_normalized_email": mock.Mock(return_value=None),
    }
    monkeypatch.setattr(
        services.dr, "async_get", lambda _h: SimpleNamespace(async_get=lambda _d: None)
    )
    handlers = contracts._register(contracts._hass_with_coordinator(coord), ctx)
    caplog.set_level(logging.DEBUG)

    await handlers[services.SERVICE_LOCATE_DEVICE](
        contracts._FakeCall({"device_id": "dev-phone"})
    )

    assert [flow._raw_action_numbers(p) for p, _ in nova.calls] == [{4}]
    assert asked == []
    assert any(
        r.getMessage() == f"Phone action=4 trigger=manual entry={flow._ENTRY_A}"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_second_locate_of_an_open_owned_phone_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
        LocationRequestNotAcceptedError,
    )
    from tests import test_phone_locate_flow as flow

    release = asyncio.Event()
    sending = asyncio.Event()

    def _held(_rx: Any) -> Any:
        async def _hold(_payload: str, _kwargs: dict[str, Any]) -> None:
            sending.set()
            await release.wait()

        return flow._Nova(_hold)

    api, nova, _rx = _owned_wire(monkeypatch, _held)
    with _labelled("manual"):
        first = asyncio.create_task(api.async_get_device_location("cid-own", "Phone"))
        await asyncio.wait_for(sending.wait(), timeout=5)

        # Bounded: a register without the per-phone lock would let the second
        # request wait on the held transport; the bound turns that into a failure.
        with pytest.raises(LocationRequestNotAcceptedError) as info:
            await asyncio.wait_for(
                api.async_get_device_location("cid-own", "Phone"), timeout=2
            )

    assert info.value.stage == "phone_request_open"
    assert len(nova.calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


def _cache_coordinator(stored: dict[str, Any]) -> Any:
    from tests.test_cache_round_trip import _make_update_coordinator

    coord = _make_update_coordinator(stored)
    coord._device_location_data = {"cid-own": dict(stored)}
    coord._record_last_good_location("cid-own", stored)
    return coord


def _phone_fix_row(seconds: float, lat: float) -> dict[str, Any]:
    from tests import test_phone_locate_flow as flow

    crypto = flow._mod("NovaApi.ExecuteAction.LocatePhone.phone_location_crypto")
    fix = crypto.PhoneFix(latitude=lat, longitude=11.4, timestamp=seconds, accuracy=8.0)
    row: dict[str, Any] = flow._phone_locate().phone_location_row(
        fix, phone_id="cid-own"
    )
    return row


_STORED_TS = 1_700_000_000.0


def _tracker_fix() -> dict[str, Any]:
    return {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 20.0,
        "last_seen": _STORED_TS,
        "location_type": "sensor",
    }


def test_the_age_rule_rejects_an_older_action_4_fix_for_the_row() -> None:
    from custom_components.googlefindmy.coordinator.cache import CacheOperations

    coord = _cache_coordinator(_tracker_fix())
    older = _phone_fix_row(_STORED_TS - 60, 48.1001)
    newer = _phone_fix_row(_STORED_TS + 60, 48.1001)
    assert CacheOperations._is_significant_update(coord, "cid-own", older) is False
    assert CacheOperations._is_significant_update(coord, "cid-own", newer) is True


@pytest.mark.parametrize("source", ["poll", None])
def test_an_older_action_4_fix_leaves_the_stored_tracker_fix(
    source: str | None,
) -> None:
    coord = _cache_coordinator(_tracker_fix())
    coord.update_device_cache(
        "cid-own", _phone_fix_row(_STORED_TS - 60, 48.1001), source=source
    )
    stored = coord._device_location_data["cid-own"]
    assert stored["last_seen"] == _STORED_TS
    assert stored["latitude"] == pytest.approx(48.1)


@pytest.mark.parametrize("source", ["poll", None])
def test_a_newer_action_4_fix_replaces_the_stored_tracker_fix(
    source: str | None,
) -> None:
    coord = _cache_coordinator(_tracker_fix())
    coord.update_device_cache(
        "cid-own", _phone_fix_row(_STORED_TS + 600, 48.1001), source=source
    )
    stored = coord._device_location_data["cid-own"]
    assert stored["last_seen"] == pytest.approx(_STORED_TS + 600)
    assert stored["latitude"] == pytest.approx(48.1001)


# --- A failed second answer keeps the status ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "phone_fcm_not_ready"])
async def test_a_failed_second_answer_keeps_the_stored_status(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
        LocationRequestNotAcceptedError,
    )
    from tests import test_phone_locate_flow as flow
    from tests import test_phone_status_sensors as status

    def _answer_with_status(rx: Any) -> Any:
        async def _answer(payload: str, _kwargs: dict[str, Any]) -> None:
            request_uuid, key, android_id = flow._request_parts(payload)
            await flow._push(
                rx,
                flow._ENTRY_A,
                status._status_push(
                    request_uuid,
                    android_id,
                    envelope=flow._envelope(key),
                    wifi=status._wifi(f'"{status._SSID}"'.encode()),
                    battery=status._battery(66),
                ),
            )

        return flow._Nova(_answer)

    api, nova, rx = _owned_wire(monkeypatch, _answer_with_status)
    coord, _sink_api = await status._coordinator(monkeypatch)
    coord.data = [{"id": "cid-own", "name": "Phone"}]
    coord.async_update_listeners = lambda: None
    api.set_phone_status_sink(coord._store_phone_status)

    with _labelled("manual"):
        await api.async_get_device_location("cid-own", "Phone")
    first = coord.get_phone_status("cid-own")
    assert first is not None and first.battery_pct == 66

    if failure == "timeout":
        nova.on_call = None  # the phone does not answer any more
        real_locate = api_module.locate_phone

        async def _short(**kwargs: Any) -> Any:
            return await real_locate(**{**kwargs, "timeout_s": 0.2})

        monkeypatch.setattr(api_module, "locate_phone", _short)
        with _labelled("manual"):
            assert await api.async_get_device_location("cid-own", "Phone") == {}
    else:

        async def _not_ready(_entry_id: str, cache: object = None) -> bool:
            del cache
            return False

        rx.async_ensure_phone_channel = _not_ready
        with (
            _labelled("manual"),
            pytest.raises(LocationRequestNotAcceptedError) as info,
        ):
            await api.async_get_device_location("cid-own", "Phone")
        assert info.value.stage == "phone_fcm_not_ready"

    assert coord.get_phone_status("cid-own") is first


# --- The answer to action 4 reaches the open phone request ------------------------
# A push of an owned phone carries its canonical ID. It must reach the phone
# register by (entry, requestUuid) before the canonical ID sends it down the
# tracker path; without an open request the tracker path stays as it was.

_RX_ENTRY = "entry-owned-push"
_RX_OTHER = "entry-owned-push-other"
_RX_CANONIC = "owned-push-canonic"


def _owned_push(request_uuid: str, canonic: str = _RX_CANONIC) -> str:
    """Hex ``DeviceUpdate`` of an owned phone: canonical ID and request id set."""
    update = DeviceUpdate_pb2.DeviceUpdate()
    if request_uuid:
        update.fcmMetadata.requestUuid = request_uuid
    ident = update.deviceMetadata.identifierInformation
    ident.type = DeviceUpdate_pb2.IDENTIFIER_ANDROID
    ident.phoneInformation.phoneInformationField1 = _android_id()
    ident.phoneInformation.canonicIds.canonicId.add().id = canonic
    return update.SerializeToString().hex()


class _Paths:
    """Real receiver; records the phone, tracker and background paths."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from custom_components.googlefindmy.Auth.fcm_receiver_ha import (
            FcmReceiverHA,
        )

        rx = FcmReceiverHA()
        rx.attach_hass(SimpleNamespace(data={}, loop=None))  # type: ignore[arg-type]
        self.rx = rx
        self.phone: list[str] = []
        self.tracker: list[str] = []
        self.background: list[str] = []
        self.uuid_parses = 0
        original = rx._extract_request_uuid_from_response

        def _count(hex_response: str) -> str | None:
            self.uuid_parses += 1
            return original(hex_response)

        async def _run(_cb: Any, canonic_id: str, _hex: str) -> None:
            self.tracker.append(canonic_id)

        async def _bg(_entry: str, canonic_id: str, *_rest: Any) -> None:
            self.background.append(canonic_id)

        monkeypatch.setattr(rx, "_extract_request_uuid_from_response", _count)
        monkeypatch.setattr(rx, "_run_callback_async", _run)
        monkeypatch.setattr(rx, "_process_background_update", _bg)
        monkeypatch.setattr(rx, "_is_tracked", lambda *_a: True)
        rx.location_update_callbacks[_RX_CANONIC] = lambda *_a: None

    def open_request(self, entry_id: str, request_uuid: str) -> None:
        self.rx.register_phone_request(entry_id, request_uuid, self.phone.append)

    async def push(self, entry_id: str, push_hex: str) -> None:
        raw = base64.b64encode(bytes.fromhex(push_hex)).decode()
        await self.rx._handle_notification_async(
            entry_id, {"data": {"com.google.android.apps.adm.FCM_PAYLOAD": raw}}
        )


@pytest.mark.asyncio
async def test_an_owned_phone_push_reaches_the_open_phone_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _Paths(monkeypatch)
    paths.open_request(_RX_ENTRY, "req-owned")
    push = _owned_push("req-owned")
    await paths.push(_RX_ENTRY, push)
    assert paths.phone == [push]
    assert paths.tracker == []
    assert paths.background == []


@pytest.mark.asyncio
async def test_without_an_open_request_no_request_id_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _Paths(monkeypatch)
    await paths.push(_RX_ENTRY, _owned_push("req-none"))
    assert paths.uuid_parses == 0
    assert paths.tracker == [_RX_CANONIC]
    assert paths.phone == []


@pytest.mark.asyncio
async def test_an_owned_push_for_another_request_stays_on_the_tracker_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _Paths(monkeypatch)
    paths.open_request(_RX_ENTRY, "req-open")
    await paths.push(_RX_ENTRY, _owned_push("req-foreign"))
    assert paths.phone == []
    assert paths.tracker == [_RX_CANONIC]


@pytest.mark.asyncio
async def test_a_push_of_another_entry_never_reaches_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _Paths(monkeypatch)
    paths.open_request(_RX_ENTRY, "req-shared")
    await paths.push(_RX_OTHER, _owned_push("req-shared"))
    assert paths.phone == []
    assert paths.uuid_parses == 0
    assert paths.tracker == [_RX_CANONIC]


@pytest.mark.asyncio
async def test_an_owned_push_without_tracker_callback_reaches_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``location_update_callbacks`` entry: the request still wins over background."""
    paths = _Paths(monkeypatch)
    paths.rx.location_update_callbacks.clear()
    paths.open_request(_RX_ENTRY, "req-bg")
    push = _owned_push("req-bg")
    await paths.push(_RX_ENTRY, push)
    assert paths.phone == [push]
    assert paths.background == []


# --- Battery and Wi-Fi sensors of an owned phone ------------------------------------
#
# The sensor platform runs over a real API target table: the stub coordinator gets the
# real ``is_on_phone_route`` bound to it, so the check reads ``has_phone_target`` of an
# API that processed a device list. No stored fix is involved anywhere.

_OWN = "cid-own"


class _SensorWorld:
    """Sensor (and tracker) platform of one account over a real target table."""

    def __init__(
        self,
        factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[dict[str, Any]],
    ) -> None:
        import types

        from custom_components.googlefindmy.coordinator.main import (
            GoogleFindMyCoordinator,
        )
        from tests.test_ble_battery_sensor_setup import (
            TRACKER_KEY,
            TRACKER_SUBENTRY_ID,
            _Harness,
        )

        self.rows = rows
        self.harness = _Harness(
            factory, devices_by_key={TRACKER_KEY: rows}, resolver=None
        )
        self.api = _owned_api(monkeypatch)
        coordinator = self.harness.coordinator
        coordinator.api = self.api
        coordinator.is_on_phone_route = types.MethodType(
            GoogleFindMyCoordinator.is_on_phone_route, coordinator
        )
        self.store: dict[str, Any] = {}
        coordinator.get_phone_status = self.store.get
        self.subentry_id = TRACKER_SUBENTRY_ID

    def prefix(self, dev_id: str) -> str:
        return f"googlefindmy_entry-ble-setup_{self.subentry_id}_{dev_id}"

    def list_devices(self, *devices: DeviceUpdate_pb2.DeviceMetadata) -> None:
        self.api._process_device_list_response(_listing(*devices))

    def status(self, dev_id: str, battery: int | None = 66) -> None:
        import time

        from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
            PhoneStatus,
        )

        self.store[dev_id] = PhoneStatus(
            battery_pct=battery, wifi_ssid="Beispiel-Testnetz", observed_at=time.time()
        )

    def phone_sensors(self) -> list[Any]:
        from custom_components.googlefindmy.sensor import (
            GoogleFindMyPhoneBatterySensor,
            GoogleFindMyPhoneWifiSensor,
        )

        return [
            entity
            for entity, _ in self.harness.added
            if isinstance(
                entity, (GoogleFindMyPhoneBatterySensor, GoogleFindMyPhoneWifiSensor)
            )
        ]

    def phone_uids(self) -> list[str]:
        return sorted(str(e.unique_id) for e in self.phone_sensors())

    def all_uids(self) -> list[str]:
        return sorted(str(e.unique_id) for e, _ in self.harness.added)


def _phone_suffixes(world: _SensorWorld, dev_id: str) -> list[str]:
    prefix = world.prefix(dev_id)
    return [f"{prefix}_phone_battery", f"{prefix}_phone_wifi_ssid"]


@pytest.mark.asyncio
async def test_the_snapshot_row_does_not_carry_the_route_marker() -> None:
    # The list marker never reaches the sensor rows, only a stored
    # phone fix would bring it. The sensor gate therefore reads the target table.
    from custom_components.googlefindmy.coordinator.helpers.cache import (
        build_base_snapshot_entry,
    )

    row = build_base_snapshot_entry(
        {"id": _OWN, "name": "Phone", "locate_route": "phone"}
    )
    assert "locate_route" not in row


def test_the_coordinator_reads_the_route_from_the_api_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy.coordinator.main import (
        GoogleFindMyCoordinator,
    )

    api = _owned_api(monkeypatch)
    api._process_device_list_response(
        _listing(
            _phone(_android_id(), phone_canonic=(_OWN,)),
            _phone(_android_id(), phone_canonic=("cid-no4",), actions=(1, 2, 5)),
            _tracker("cid-tag"),
        )
    )
    coordinator = SimpleNamespace(api=api)
    check = GoogleFindMyCoordinator.is_on_phone_route
    assert [check(cast(Any, coordinator), d) for d in (_OWN, "cid-no4", "cid-tag")] == [
        True,
        False,
        False,
    ]
    # A coordinator without an API (or an API without the check) says no.
    assert check(cast(Any, SimpleNamespace(api=None)), _OWN) is False
    assert check(cast(Any, SimpleNamespace()), _OWN) is False


@pytest.mark.asyncio
async def test_an_owned_phone_gets_both_sensors_on_its_device(
    stub_coordinator_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.googlefindmy.device_tracker import GoogleFindMyDeviceTracker

    world = _SensorWorld(
        stub_coordinator_factory,
        monkeypatch,
        [{"id": _OWN, "name": "Phone"}, {"id": "cid-tag", "name": "Tag"}],
    )
    world.list_devices(
        _phone(_android_id(), phone_canonic=(_OWN,)), _tracker("cid-tag")
    )
    await world.harness.setup()
    await world.harness.fire_listeners()
    before = world.all_uids()
    assert world.phone_uids() == []

    world.status(_OWN)
    await world.harness.fire_listeners()
    await world.harness.fire_listeners()

    assert world.phone_uids() == _phone_suffixes(world, _OWN)
    # The existing entities keep their unique IDs, and nothing else is new.
    after = world.all_uids()
    assert sorted(set(after) - set(before)) == _phone_suffixes(world, _OWN)
    assert set(before) <= set(after)
    assert len(after) == len(set(after))
    # The sensor platform adds no tracker; the sensors hang on the device of the
    # canonic ID, the one the device_tracker of the same scope names.
    assert not any(
        isinstance(e, GoogleFindMyDeviceTracker) for e, _ in world.harness.added
    )
    sensors = world.phone_sensors()
    tracker = GoogleFindMyDeviceTracker(
        world.harness.coordinator,
        {"id": _OWN, "name": "Phone"},
        subentry_key=sensors[0].subentry_key,
        subentry_identifier=str(sensors[0].subentry_identifier),
    )
    tracker.hass = world.harness.hass
    tracker_ids = tracker.device_info.identifiers
    assert tracker_ids
    assert all(s.device_info.identifiers == tracker_ids for s in sensors)


@pytest.mark.asyncio
async def test_a_canonicless_phone_of_another_account_keeps_its_sensors(
    stub_coordinator_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.googlefindmy.const import PHONE_ID_PREFIX

    shared = PHONE_ID_PREFIX + "b" * 32
    owner = _SensorWorld(
        stub_coordinator_factory, monkeypatch, [{"id": _OWN, "name": "Phone"}]
    )
    other = _SensorWorld(
        stub_coordinator_factory, monkeypatch, [{"id": shared, "name": "Phone"}]
    )
    owner.list_devices(_phone(_android_id(), phone_canonic=(_OWN,)))
    await owner.harness.setup()
    await other.harness.setup()
    owner.status(_OWN)
    other.status(shared)
    await owner.harness.fire_listeners()
    await other.harness.fire_listeners()

    assert owner.phone_uids() == _phone_suffixes(owner, _OWN)
    assert other.phone_uids() == _phone_suffixes(other, shared)


@pytest.mark.asyncio
async def test_no_sensor_without_an_owned_phone_or_without_a_status(
    stub_coordinator_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.googlefindmy.const import PHONE_ID_PREFIX

    canonicless = PHONE_ID_PREFIX + "c" * 32
    # An account with a tracker and a canonicless phone only, and an owned phone
    # that is on the phone route but has no status yet.
    world = _SensorWorld(
        stub_coordinator_factory,
        monkeypatch,
        [
            {"id": "cid-tag", "name": "Tag"},
            {"id": canonicless, "name": "Phone"},
            {"id": _OWN, "name": "Phone"},
        ],
    )
    world.list_devices(
        _phone(_android_id(), phone_canonic=(_OWN,)), _tracker("cid-tag")
    )
    world.status("cid-tag")
    await world.harness.setup()
    await world.harness.fire_listeners()

    assert world.phone_uids() == []


@pytest.mark.asyncio
async def test_sensors_follow_the_table_not_the_fix(
    stub_coordinator_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Setup before the first list builds nothing; the first list
    # with a status builds both, without a restart and without any stored fix.
    world = _SensorWorld(
        stub_coordinator_factory, monkeypatch, [{"id": _OWN, "name": "Phone"}]
    )
    world.status(_OWN)
    await world.harness.setup()
    assert world.phone_uids() == []

    aid = _android_id()
    world.list_devices(_phone(aid, phone_canonic=(_OWN,)))
    await world.harness.fire_listeners()
    assert world.phone_uids() == _phone_suffixes(world, _OWN)

    # A later list without action 4: the entities stay, keep their value, and no
    # second one appears.
    world.list_devices(_phone(aid, phone_canonic=(_OWN,), actions=(1, 2, 5)))
    await world.harness.fire_listeners()
    await world.harness.fire_listeners()
    assert world.phone_uids() == _phone_suffixes(world, _OWN)
    battery = next(s for s in world.phone_sensors() if s.unique_id.endswith("_battery"))
    assert battery.native_value == 66


@pytest.mark.asyncio
async def test_a_failed_query_keeps_the_sensor_value(
    stub_coordinator_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed query writes no status; the sensors show the stored one.
    world = _SensorWorld(
        stub_coordinator_factory, monkeypatch, [{"id": _OWN, "name": "Phone"}]
    )
    world.list_devices(_phone(_android_id(), phone_canonic=(_OWN,)))
    world.status(_OWN, battery=58)
    await world.harness.setup()
    stored = world.store[_OWN]
    await world.harness.fire_listeners()  # the cycle with the failed query
    assert world.store[_OWN] is stored
    values = {
        s.unique_id.rsplit("_", 1)[-1]: s.native_value for s in world.phone_sensors()
    }
    assert values == {"battery": 58, "ssid": "Beispiel-Testnetz"}


@pytest.mark.asyncio
async def test_a_registry_entry_brings_the_sensor_back_without_a_status(
    stub_coordinator_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Restart: the registry holds the battery sensor, the status store is empty.
    world = _SensorWorld(
        stub_coordinator_factory, monkeypatch, [{"id": _OWN, "name": "Phone"}]
    )
    battery_uid = _phone_suffixes(world, _OWN)[0]
    world.harness.register("sensor.phone_battery", battery_uid, world.subentry_id)
    await world.harness.setup()
    assert world.phone_uids() == []  # before the first list: not on the route yet

    world.list_devices(_phone(_android_id(), phone_canonic=(_OWN,)))
    await world.harness.fire_listeners()
    assert world.phone_uids() == [battery_uid]


# --- Docs: README and integration contract name the owned phone ---------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OLD_README_SENTENCE = (
    "A phone that Google lists with a canonical ID is located like a tracker and does "
    "not get these two sensors."
)


def _readme_paragraph_with(marker: str) -> str:
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    paragraphs = [p for p in text.split("\n") if marker in p]
    assert len(paragraphs) == 1, f"expected one README paragraph with {marker!r}"
    return paragraphs[0]


def test_readme_names_the_sensors_of_an_owned_phone() -> None:
    """The README paragraph on phone sensors keeps the corrected claims.

    Up to PR #1463 the README said that a phone listed with a canonical ID
    gets neither of the two sensors (``_OLD_README_SENTENCE``). This test pins
    the corrected wording, rejects a draft claim of a silent own-account
    request that a measurement replaced before release, and keeps the floor
    value in one place.

    Blind spot: these are substring and presence checks. They cannot tell an
    accurate paragraph from one that contains the right phrases next to a new
    wrong claim, and a reworded return of a withdrawn claim passes unless it
    reuses the exact text checked below. It is a ratchet, not a proof.

    Retirement: delete this test once the corrected paragraph has shipped
    unchanged in a release (case (a) of "Docstring semantics guards" in
    ``tests/AGENTS.md``).
    """
    paragraph = _readme_paragraph_with('"Battery"')
    assert _OLD_README_SENTENCE not in paragraph
    assert "canonical ID and that offers the locate action" in paragraph
    assert "gets the same two sensors" in paragraph
    assert "measured on one phone" in paragraph
    # The measurement of 2026-11-18 replaced the claim of a silent own-account request.
    assert "saw no notification" not in paragraph
    assert "Automatic requests ask it like a tracker" in paragraph
    assert "ask the phone directly with the locate action" in paragraph
    assert "Every direct request showed a notification" in paragraph
    # The floor value stays at one place, so the polling-interval plan changes one spot.
    assert paragraph.count("900 s") == 1


def test_contract_names_the_second_phone_class_and_the_loop_parse() -> None:
    """The integration contract names the owned-phone route and its limits.

    Up to PR #1463 ``custom_components/googlefindmy/AGENTS.md`` gave
    "every request may show a notification" as the reason for spacing
    automatic requests to a phone; it now cites the measured requests instead.
    This test rejects the old sentence and a draft citation of one silent
    own-account request, and pins the replacement statements: the owned-phone
    target, ring and stop staying on the tracker path, the synchronous parse
    on the event loop and the rule for the ``"manual"`` dispatch label.

    Blind spot: these are substring and presence checks. They cannot verify
    that the contract describes the code correctly, and a withdrawn claim that
    returns in different words passes. It is a ratchet, not a proof.

    Retirement: delete this test once the corrected contract text has shipped
    unchanged in a release (case (a) of "Docstring semantics guards" in
    ``tests/AGENTS.md``).
    """
    contract = (
        _REPO_ROOT / "custom_components" / "googlefindmy" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "owned_phone_target" in contract
    assert "Ring and stop of such a phone stay on the tracker" in contract
    assert "parses the push synchronously on the event loop" in contract
    assert "because every\n  request may show a notification" not in contract
    assert (
        "for a request from the owner's account (one phone, 2026-11-17)" not in contract
    )
    assert 'nova_request.current_dispatch_kind() == "manual"' in contract
    assert 'may stamp `"manual"` only when a person' in contract
