# tests/test_phone_ring_paths.py
"""Ring (action 1) and stop ring (action 5) for phones without a canonical ID.

Every path that can start a ring or a stop is driven down to the transport,
where the payload is intercepted and its action message decoded raw: the
button (via the service), the service handlers, the coordinator, the API and
the API's sync wrappers. For a phone row the only action numbers that
may appear are 1, 4 and 5; a ring is exactly {1}, a stop exactly {5}.

The readiness gate is pinned on both sides: the API sends nothing for
any of the three actions while the entry's push channel is not listening, and
the receiver's gate itself never sets the locate-sent stamp.

All identifiers below are invented. The tests are offline (pytest-socket) and
the transport is replaced, so nothing here can reach a phone.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy import services
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import (
    PHONE_ID_PREFIX,
    PHONE_MAX_AUTH_RESENDS,
    PlaySoundOutcome,
    SoundDispatchOutcome,
    StopSoundOutcome,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
    phone_locate as phone_locate_module,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
    phone_sound as phone_sound_module,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound import (
    start_sound_request as start_sound_module,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound import (
    stop_sound_request as stop_sound_module,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaError,
    NovaHTTPError,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from tests.helpers.service_doubles import FakeServiceCall, FakeServiceRegistry

pytestmark = pytest.mark.asyncio

_ENTRY = "entry-ring"
_PHONE = PHONE_ID_PREFIX + "c3" * 16
_ANDROID_ID = 0x0F1E2D3C4B5A6978
_TOKEN = "push-token-ring-entry"
_TRACKER = "tracker-canonic-ring"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_fields(data: bytes) -> list[int]:
    """Top-level field numbers of a serialized message, read without a schema."""
    out: list[int] = []
    pos = 0
    while pos < len(data):
        key = 0
        shift = 0
        while True:
            byte = data[pos]
            pos += 1
            key |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        number, wire_type = key >> 3, key & 7
        out.append(number)
        if wire_type == 0:
            while data[pos] & 0x80:
                pos += 1
            pos += 1
        elif wire_type == 2:
            length = 0
            shift = 0
            while True:
                byte = data[pos]
                pos += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            pos += length
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:  # pragma: no cover - a group would be a builder defect
            raise AssertionError(f"unexpected wire type {wire_type}")
    return out


def _decode(payload_hex: str) -> Any:
    request = DeviceUpdate_pb2.ExecuteActionRequest()
    request.ParseFromString(bytes.fromhex(payload_hex))
    return request


def _action_numbers(payload_hex: str) -> set[int]:
    return set(_raw_fields(_decode(payload_hex).action.SerializeToString()))


class _Wire:
    """Replacement for ``async_nova_request`` on every sound and locate site."""

    def __init__(self, error: Exception | None = None) -> None:
        self.payloads: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.error = error

    async def __call__(self, _scope: str, payload: str, **kwargs: Any) -> str:
        self.payloads.append(payload)
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        return "0a00"

    def actions(self) -> list[set[int]]:
        return [_action_numbers(p) for p in self.payloads]


def _install_wire(monkeypatch: pytest.MonkeyPatch, wire: _Wire) -> None:
    for module in (
        phone_sound_module,
        phone_locate_module,
        start_sound_module,
        stop_sound_module,
    ):
        monkeypatch.setattr(module, "async_nova_request", wire)


class _Cache:
    entry_id = _ENTRY

    async def async_get_cached_value(self, _key: str) -> Any:
        return None

    async def async_set_cached_value(self, _key: str, _value: Any) -> None:
        return None


class _Receiver:
    """The two receiver calls the phone sound path makes."""

    def __init__(self, *, ready: bool = True, token: str | None = _TOKEN) -> None:
        self.ready = ready
        self.token = token
        self.ready_calls: list[str] = []

    async def async_ensure_phone_channel(
        self, entry_id: str, cache: object = None
    ) -> bool:
        del cache
        self.ready_calls.append(entry_id)
        return self.ready

    def get_entry_fcm_token(self, entry_id: str) -> str | None:
        return self.token if entry_id == _ENTRY else None

    def get_fcm_token(self, _entry_id: str | None = None) -> str | None:
        return self.token

    def register_phone_request(self, *_args: Any) -> None:
        return None

    def unregister_phone_request(self, *_args: Any) -> None:
        return None


def _api(monkeypatch: pytest.MonkeyPatch, receiver: object) -> GoogleFindMyAPI:
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda *_a: receiver)
    api = GoogleFindMyAPI(cache=cast(Any, _Cache()))
    api._phone_targets[_PHONE] = _ANDROID_ID
    return api


def _coordinator(
    api: GoogleFindMyAPI, caps: dict[str, dict[str, Any]] | None = None
) -> GoogleFindMyCoordinator:
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    c = cast(Any, coordinator)
    c.api = api
    c._device_caps = (
        {_PHONE: {"can_ring": True, "can_stop_ring": True}} if caps is None else caps
    )
    c._sound_request_uuids = {}
    c._sound_request_timestamps = {}
    c._sound_play_accepted_at = {}
    c._note_push_transport_problem = lambda: None
    c._note_stop_transport_problem_without_extending = lambda: None
    c._set_auth_state = lambda **_kw: None
    c._api_push_ready = lambda: True
    c._push_cooldown_until = 0.0
    c.note_error = lambda *_a, **_kw: None
    c.is_device_present = lambda _device_id: True
    c.get_device_display_name = lambda _device_id: "Phone"

    async def _save() -> None:
        return None

    c._async_save_sound_uuids = _save
    return coordinator


def _ctx(device_id: str) -> dict[str, Any]:
    """Service ctx as in test_services_handler_contracts, resolving to ``device_id``."""
    return {
        "domain": services.DOMAIN,
        "resolve_canonical": mock.Mock(return_value=(device_id, "Phone")),
        "is_active_entry": mock.Mock(return_value=True),
        "primary_active_entry": mock.Mock(return_value=None),
        "opt": mock.Mock(side_effect=lambda entry, key, default: default),
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


def _service_handlers(
    coordinator: GoogleFindMyCoordinator,
    device_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Callable[..., Any]]:
    entry = SimpleNamespace(
        entry_id=_ENTRY,
        runtime_data=SimpleNamespace(coordinator=coordinator),
        title="Account",
    )
    registry = FakeServiceRegistry()
    hass = SimpleNamespace(
        services=registry,
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
        data={},
    )
    monkeypatch.setattr(
        services.dr,
        "async_get",
        lambda _h: SimpleNamespace(async_get=lambda _d: None),
    )
    coro = services.async_register_services(cast(Any, hass), _ctx(device_id))
    try:
        coro.send(None)
    except StopIteration:
        pass
    return {service: handler for (_d, service), handler in registry.handlers.items()}


async def _press(
    cls_name: str,
    coordinator: GoogleFindMyCoordinator,
    handlers: dict[str, Callable[..., Any]],
) -> None:
    """Press the real button class; its service call reaches the real handler."""
    button_module = importlib.import_module("custom_components.googlefindmy.button")
    base = getattr(button_module, cls_name)

    class _Pressable(base):  # type: ignore[misc, valid-type]
        device_id = _PHONE

        def device_label(self) -> str:
            return "Phone"

        def coordinator_has_device(self) -> bool:
            return True

        def _service_target_id(self) -> str:
            return _PHONE

        def _update_last_pressed(self) -> None:
            return None

    class _Services:
        async def async_call(
            self, _domain: str, service: str, data: dict[str, Any], **_kw: Any
        ) -> None:
            await handlers[service](FakeServiceCall(data))

    button = _Pressable.__new__(_Pressable)
    b = cast(Any, button)
    b.coordinator = coordinator
    b.hass = SimpleNamespace(services=_Services())
    b._context = None
    await button.async_press()


# ---------------------------------------------------------------------------
# Action whitelist over every path
# ---------------------------------------------------------------------------


async def test_every_ring_path_sends_exactly_action_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Button, service, coordinator and API each put exactly {1} on the wire."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api)
    handlers = _service_handlers(coordinator, _PHONE, monkeypatch)

    await _press("GoogleFindMyPlaySoundButton", coordinator, handlers)
    await handlers[services.SERVICE_PLAY_SOUND](FakeServiceCall({"device_id": _PHONE}))
    assert await coordinator.async_play_sound(_PHONE) is PlaySoundOutcome.ACCEPTED
    result = await api.async_play_sound(_PHONE)

    assert result.outcome is SoundDispatchOutcome.ACCEPTED
    assert wire.actions() == [{1}, {1}, {1}, {1}]
    assert all(kw["max_retries"] == 0 for kw in wire.kwargs)
    # At most one resend after a 401.
    assert PHONE_MAX_AUTH_RESENDS == 1
    assert all(kw["max_auth_resends"] == PHONE_MAX_AUTH_RESENDS for kw in wire.kwargs)


async def test_every_stop_path_sends_exactly_action_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop through every path is {5}, device-scoped and CANCELLED."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api)
    handlers = _service_handlers(coordinator, _PHONE, monkeypatch)

    await _press("GoogleFindMyStopSoundButton", coordinator, handlers)
    await handlers[services.SERVICE_STOP_SOUND](FakeServiceCall({"device_id": _PHONE}))
    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.CANCELLED
    # A key passed in is not used: the stop carries its own fresh UUID.
    assert (
        await api.async_stop_sound(_PHONE, "some-foreign-key")
        is SoundDispatchOutcome.ACCEPTED
    )

    assert wire.actions() == [{5}, {5}, {5}, {5}]
    assert all(kw["max_retries"] == 0 for kw in wire.kwargs)
    assert all(kw["max_auth_resends"] == PHONE_MAX_AUTH_RESENDS for kw in wire.kwargs)
    uuids = [_decode(p).requestMetadata.requestUuid for p in wire.payloads]
    assert "some-foreign-key" not in uuids
    assert all(uuids) and len(set(uuids)) == len(uuids)


async def test_sync_wrappers_send_exactly_1_and_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI wrappers run the same branch (outside a running loop)."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())

    async def _direct(factory: Callable[[], Any], **_kw: Any) -> Any:
        return await factory()

    results: list[Any] = []

    def _run_sync(factory: Callable[[], Any], **kw: Any) -> Any:
        results.append((factory, kw))
        return kw["default"]

    monkeypatch.setattr(api, "_run_sync_helper", _run_sync)
    assert api.play_sound(_PHONE) is False  # the default the stub returns
    assert api.stop_sound(_PHONE) is False
    for factory, _kw in results:
        await factory()
    assert wire.actions() == [{1}, {5}]


async def test_cancel_key_of_a_phone_ring_is_its_request_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())

    result = await api.async_play_sound(_PHONE)

    assert result.cancel_key
    assert _decode(wire.payloads[0]).requestMetadata.requestUuid == result.cancel_key


# ---------------------------------------------------------------------------
# Raw form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "number"), [("play", 1), ("stop", 5)])
async def test_raw_form_scope_type_empty_action_no_key(
    monkeypatch: pytest.MonkeyPatch, method: str, number: int
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())

    if method == "play":
        await api.async_play_sound(_PHONE)
    else:
        await api.async_stop_sound(_PHONE)

    request = _decode(wire.payloads[0])
    assert int(request.scope.phone.phoneInformationField1) == _ANDROID_ID
    assert int(request.scope.type) == 1
    assert request.requestMetadata.gcmRegistrationId.id == _TOKEN
    raw = request.action.SerializeToString()
    assert _raw_fields(raw) == [number]
    # The action message is the field header plus a zero length: empty, no key.
    assert raw == bytes([(number << 3) | 2, 0])
    assert int(request.requestMetadata.type) == 1


# ---------------------------------------------------------------------------
# Readiness gate
# ---------------------------------------------------------------------------


async def test_not_ready_sends_nothing_for_any_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    receiver = _Receiver(ready=False)
    api = _api(monkeypatch, receiver)

    play = await api.async_play_sound(_PHONE)
    stop = await api.async_stop_sound(_PHONE)
    with pytest.raises(LocationRequestNotAcceptedError) as excinfo:
        await api._async_get_phone_location(_PHONE)

    assert play.outcome is SoundDispatchOutcome.NOT_SENT
    assert play.cancel_key is None
    assert stop is SoundDispatchOutcome.NOT_SENT
    assert excinfo.value.stage == "phone_fcm_not_ready"
    assert excinfo.value.stage in phone_locate_module.PHONE_LOCATE_NOT_ACCEPTED_STAGES
    assert wire.payloads == []
    assert receiver.ready_calls == [_ENTRY, _ENTRY, _ENTRY]


async def test_receiver_without_the_gate_counts_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: an older receiver that cannot confirm readiness sends nothing."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)

    class _OldReceiver(_Receiver):
        async_ensure_phone_channel = None  # type: ignore[assignment]

    api = _api(monkeypatch, _OldReceiver())

    assert (await api.async_play_sound(_PHONE)).outcome is SoundDispatchOutcome.NOT_SENT
    assert wire.payloads == []


async def test_a_raising_gate_counts_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)

    class _Broken(_Receiver):
        async def async_ensure_phone_channel(
            self, entry_id: str, cache: object = None
        ) -> bool:
            raise RuntimeError("boom")

    api = _api(monkeypatch, _Broken())

    assert await api.async_stop_sound(_PHONE) is SoundDispatchOutcome.NOT_SENT
    assert wire.payloads == []


async def test_ready_without_own_token_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver(token=None))

    assert (await api.async_play_sound(_PHONE)).outcome is SoundDispatchOutcome.NOT_SENT
    assert wire.payloads == []


async def test_foreign_token_is_never_used_for_a_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Own token missing, another entry's token available: nothing is sent."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)

    class _ForeignOnly(_Receiver):
        def get_entry_fcm_token(self, entry_id: str) -> str | None:
            return None

        def get_fcm_token(self, _entry_id: str | None = None) -> str | None:
            return "token-of-another-entry"

    api = _api(monkeypatch, _ForeignOnly())

    assert (await api.async_play_sound(_PHONE)).outcome is SoundDispatchOutcome.NOT_SENT
    assert await api.async_stop_sound(_PHONE) is SoundDispatchOutcome.NOT_SENT
    assert wire.payloads == []


async def test_unknown_phone_target_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    receiver = _Receiver()
    api = _api(monkeypatch, receiver)
    api._phone_targets.clear()

    assert await api.async_stop_sound(_PHONE) is SoundDispatchOutcome.NOT_SENT
    assert wire.payloads == []
    assert receiver.ready_calls == []


def _bare_receiver() -> FcmReceiverHA:
    rx = FcmReceiverHA()
    rx.attach_hass(SimpleNamespace(data={}, loop=None))  # type: ignore[arg-type]
    return rx


async def test_receiver_gate_sets_no_locate_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate shares the readiness steps but never stamps a locate."""
    rx = _bare_receiver()
    rx.creds[_ENTRY] = {"fcm": {"registration": {"token": _TOKEN}}}
    seen: list[tuple[str, str]] = []

    async def _listening(
        entry_id: str, cache: Any, *, purpose: str, strict_token: bool = False
    ) -> str | None:
        seen.append((entry_id, purpose))
        assert strict_token is True
        return _TOKEN

    monkeypatch.setattr(rx, "_async_ensure_entry_listening", _listening)

    assert await rx.async_ensure_phone_channel(_ENTRY) is True
    assert seen == [(_ENTRY, "Phone request readiness")]
    assert rx._entry_last_locate_sent_monotonic == {}
    assert rx.location_update_callbacks == {}


async def test_receiver_gate_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    rx = _bare_receiver()

    async def _not_listening(*_a: Any, **_kw: Any) -> str | None:
        return None

    monkeypatch.setattr(rx, "_async_ensure_entry_listening", _not_listening)
    assert await rx.async_ensure_phone_channel(_ENTRY) is False
    assert await rx.async_ensure_phone_channel("") is False

    async def _listening(*_a: Any, **_kw: Any) -> str | None:
        return "token-of-someone"

    # Listening, but the entry has no token of its own: not ready.
    monkeypatch.setattr(rx, "_async_ensure_entry_listening", _listening)
    assert await rx.async_ensure_phone_channel(_ENTRY) is False
    assert rx._entry_last_locate_sent_monotonic == {}


async def test_real_gate_never_borrows_another_entrys_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two accounts, B without a token: the real readiness steps fail closed for B,
    leave A's push route alone and set no stamp (no foreign token)."""
    rx = _bare_receiver()
    rx.creds["entry-a"] = {"fcm": {"registration": {"token": "token-of-a"}}}
    rx._update_token_routing("token-of-a", {"entry-a"})
    registered: list[str] = []

    async def _client(entry_id: str, cache: Any, generation: Any = None) -> object:
        return object()

    async def _supervisor(entry_id: str, cache: Any) -> None:
        return None

    async def _register(entry_id: str) -> bool:
        registered.append(entry_id)
        return False

    monkeypatch.setattr(rx, "_ensure_client_for_entry", _client)
    monkeypatch.setattr(rx, "_start_supervisor_for_entry", _supervisor)
    monkeypatch.setattr(rx, "_register_for_fcm_entry", _register)

    assert await rx.async_ensure_phone_channel("entry-b") is False
    assert registered == ["entry-b"]
    assert rx._token_to_entries["token-of-a"] == {"entry-a"}
    assert rx._entry_last_locate_sent_monotonic == {}
    assert rx.location_update_callbacks == {}


async def test_tracker_registration_still_stamps_through_the_shared_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tracker path keeps its stamp and its callback after the refactor."""
    rx = _bare_receiver()
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id=_ENTRY),
        cache=_Cache(),
        is_device_present=lambda _cid: True,
    )
    rx.coordinators.append(coordinator)  # type: ignore[arg-type]
    purposes: list[str] = []

    async def _listening(
        entry_id: str, cache: Any, *, purpose: str, strict_token: bool = False
    ) -> str | None:
        purposes.append(purpose)
        assert strict_token is False
        return _TOKEN

    monkeypatch.setattr(rx, "_async_ensure_entry_listening", _listening)

    token = await rx.async_register_for_location_updates(_TRACKER, lambda *_a: None)

    assert token == _TOKEN
    assert purposes == ["Manual locate registration"]
    assert _ENTRY in rx._entry_last_locate_sent_monotonic
    assert _TRACKER in rx.location_update_callbacks


# ---------------------------------------------------------------------------
# Capability and the coordinator's phone rules
# ---------------------------------------------------------------------------


async def test_phone_without_action_1_cannot_play(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never optimistic for a phone row."""
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api, caps={_PHONE: {"can_ring": False}})
    cast(Any, coordinator)._device_location_data = {_PHONE: {}}
    cast(Any, coordinator)._ensure_device_name_cache = lambda: {_PHONE: "Phone"}

    assert coordinator.can_play_sound(_PHONE) is False
    cast(Any, coordinator)._device_caps = {}
    assert coordinator.can_play_sound(_PHONE) is False
    cast(Any, coordinator)._device_caps = {_PHONE: {"can_ring": True}}
    assert coordinator.can_play_sound(_PHONE) is True


async def test_coordinator_refuses_ring_and_stop_without_the_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api, caps={_PHONE: {}})

    assert await coordinator.async_play_sound(_PHONE) is PlaySoundOutcome.FAILED
    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.FAILED
    assert coordinator.can_stop_sound(_PHONE) is False
    assert wire.payloads == []


async def test_stop_button_follows_can_stop_ring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api, caps={_PHONE: {"can_ring": True}})
    button_module = importlib.import_module("custom_components.googlefindmy.button")

    class _Stop(button_module.GoogleFindMyStopSoundButton):  # type: ignore[misc, name-defined]
        device_id = _PHONE

        def device_label(self) -> str:
            return "Phone"

        def coordinator_has_device(self) -> bool:
            return True

    button = _Stop.__new__(_Stop)
    cast(Any, button).coordinator = coordinator
    assert button.available is False
    cast(Any, coordinator)._device_caps[_PHONE]["can_stop_ring"] = True
    assert button.available is True
    # Trackers keep their rule: present means stoppable.
    assert coordinator.can_stop_sound(_TRACKER) is True


async def test_phone_stop_is_never_uncorrelated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api)
    assert coordinator.classify_stop_correlation(_PHONE, None) is None
    assert coordinator.classify_stop_correlation(_PHONE, "foreign") is None


async def test_phone_stop_suppressed_while_push_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api)
    cast(Any, coordinator)._api_push_ready = lambda: False

    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.SUPPRESSED
    assert wire.payloads == []


async def test_phone_stop_breaks_through_a_running_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A play that lost its answer arms the window; the device-scoped stop still goes."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    coordinator = _coordinator(api)
    c = cast(Any, coordinator)
    c._api_push_ready = lambda: False
    c._push_cooldown_until = float("inf")

    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.CANCELLED
    assert wire.actions() == [{5}]


async def test_phone_failures_keep_the_sound_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared ladder classifies a phone refusal like a tracker refusal."""
    api = _api(monkeypatch, _Receiver())
    cases: list[tuple[Exception, SoundDispatchOutcome]] = [
        (NovaHTTPError(503, "unavailable"), SoundDispatchOutcome.REJECTED_SERVER),
        (NovaError("network"), SoundDispatchOutcome.TRANSPORT_FAILED),
        (ValueError("lock"), SoundDispatchOutcome.INTERNAL_ERROR),
    ]
    for error, expected in cases:
        wire = _Wire(error=error)
        _install_wire(monkeypatch, wire)
        assert (await api.async_play_sound(_PHONE)).outcome is expected
        assert await api.async_stop_sound(_PHONE) is expected
        assert wire.actions() == [{1}, {5}]


async def test_phone_counters_count_ring_and_stop_as_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    counted: list[tuple[int, str]] = []
    api.set_phone_action_observer(
        lambda action, trigger: counted.append((action, trigger))
    )

    await api.async_play_sound(_PHONE)
    await api.async_stop_sound(_PHONE)

    assert counted == [(1, "manual"), (5, "manual")]


async def test_ring_and_stop_counters_exist_on_the_coordinator(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The observer's labels for 1 and 5 hit real counters, no unknown-stat warning."""
    from tests.test_coordinator_automatic_polling import (
        _DispatchingAPI,
        _make_coordinator,
    )

    caplog.set_level(logging.DEBUG)
    coord = _make_coordinator(monkeypatch, options={}, api=_DispatchingAPI())
    coord._count_phone_action(1, "manual")
    coord._count_phone_action(5, "manual")
    coord._count_phone_action(5, "manual")

    assert coord.stats["phone_action_1_manual"] == 1
    assert coord.stats["phone_action_5_manual"] == 2
    assert "phone_action_1_auto" not in coord.stats
    assert not any("unknown stat" in r.getMessage() for r in caplog.records)
    save_task = getattr(coord, "_stats_save_task", None)
    if save_task is not None:
        save_task.cancel()


async def test_sound_module_refuses_locate_and_unknown_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module's own check stops 4 and every other number before the builder."""
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    built: list[int] = []

    def _builder(_android_id: int, action: int, *_a: Any, **_kw: Any) -> str:
        built.append(int(action))
        raise AssertionError("builder reached")

    monkeypatch.setattr(phone_sound_module, "build_phone_action_request", _builder)
    for action in (4, 2, 30):
        with pytest.raises(ValueError, match="only actions 1 and 5"):
            await phone_sound_module.async_submit_phone_sound_request(
                cast(Any, action),
                _ANDROID_ID,
                _TOKEN,
                "uuid-x",
                entry_id=_ENTRY,
                session=None,
                cache=None,
                namespace=_ENTRY,
            )
    assert built == []
    assert wire.payloads == []


@pytest.mark.parametrize(
    ("can_ring", "can_stop_ring"), [(True, True), (True, False), (False, True)]
)
async def test_poll_cycle_caches_both_phone_capabilities(
    monkeypatch: pytest.MonkeyPatch, can_ring: bool, can_stop_ring: bool
) -> None:
    """The coordinator takes ``can_ring`` and ``can_stop_ring`` from the row."""
    from tests.test_coordinator_automatic_polling import (
        _DispatchingAPI,
        _make_coordinator,
    )

    coord = _make_coordinator(monkeypatch, options={}, api=_DispatchingAPI())
    cast(Any, coord)._last_device_list = [
        {
            "id": _PHONE,
            "name": "Phone",
            "can_ring": can_ring,
            "can_stop_ring": can_stop_ring,
        }
    ]
    coord.data = await coord._async_update_data()
    for task in list(cast(Any, coord.hass).background):
        task.cancel()

    caps = cast(Any, coord)._device_caps[_PHONE]
    assert caps["can_ring"] is can_ring
    assert caps["can_stop_ring"] is can_stop_ring
    assert coord.can_play_sound(_PHONE) is can_ring
    assert coord.can_stop_sound(_PHONE) is can_stop_ring


# ---------------------------------------------------------------------------
# Regression of the tracker ring
# ---------------------------------------------------------------------------


async def test_tracker_ring_and_stop_keep_their_spot_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    receiver = _Receiver()
    api = _api(monkeypatch, receiver)

    await api.async_play_sound(_TRACKER)
    await api.async_stop_sound(_TRACKER, "cancel-1")

    requests = [_decode(p) for p in wire.payloads]
    assert [_raw_fields(r.action.SerializeToString()) for r in requests] == [[31], [32]]
    assert all(r.scope.device.canonicId.id == _TRACKER for r in requests)
    assert all(not r.scope.HasField("phone") for r in requests)
    assert receiver.ready_calls == []


# ---------------------------------------------------------------------------
# Log hygiene of the new paths
# ---------------------------------------------------------------------------


async def test_ring_stop_and_refusals_never_log_the_android_id_or_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    wire = _Wire()
    _install_wire(monkeypatch, wire)
    api = _api(monkeypatch, _Receiver())
    await api.async_play_sound(_PHONE)
    await api.async_stop_sound(_PHONE)
    api_not_ready = _api(monkeypatch, _Receiver(ready=False))
    await api_not_ready.async_play_sound(_PHONE)
    _install_wire(monkeypatch, _Wire(error=NovaHTTPError(500, "x")))
    await api.async_stop_sound(_PHONE)

    forbidden = {
        str(_ANDROID_ID),
        f"{_ANDROID_ID:x}",
        f"{_ANDROID_ID:016x}",
        f"{_ANDROID_ID:X}",
        _TOKEN,
    }
    text = "\n".join(r.getMessage() for r in caplog.records)
    for value in forbidden:
        assert value not in text
    assert "Phone action=1 trigger=manual entry=entry-ring" in text
    assert "Phone action=5 trigger=manual entry=entry-ring" in text
    assert "stage=phone_fcm_not_ready" in text
