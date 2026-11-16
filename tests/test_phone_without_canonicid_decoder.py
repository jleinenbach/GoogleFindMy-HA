# tests/test_phone_without_canonicid_decoder.py
"""An Android phone listed without a canonical ID becomes its own row.

Google lists a phone that has no canonical ID in this account with its Android ID
(``identifierInformation.phoneInformation.phoneInformationField1``). With a per-entry
``phone_id_for`` callable the decoder emits one row for it, keyed by a keyed hash of
the Android ID (``phone_identity``), never by the Android ID itself. Without the
callable (capability probe, CLI, no key) the decoder behaves as before.

Every Android ID here is synthetic: drawn at run time, never a measured value.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
from typing import Any

import pytest

from custom_components.googlefindmy.const import (
    PHONE_ID_HEX_LEN,
    PHONE_ID_KEY_CACHE_NAME,
    PHONE_ID_PREFIX,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder

_ALL_224F_ACTIONS = (1, 2, 4, 5, 6, 7, 13, 14)


@pytest.fixture(autouse=True)
def _reset_decoder_state() -> None:
    decoder._reset_canonicless_warning_state()


# Invented, distinct Android IDs of the observed shape (19 digits, 62 bit). A counter
# instead of ``secrets``: another test module may leave ``secrets.randbelow``
# patched for the rest of the session, which made two "random" IDs equal.
_ANDROID_IDS = itertools.count(1_234_567_890_123_456_789, 7_919)


def _random_android_id() -> int:
    """Return a new invented 19-digit Android ID, distinct within the session."""
    return next(_ANDROID_IDS)


def _action_bytes(number: int) -> bytes:
    action = DeviceUpdate_pb2.DeviceMetadata.SupportedActions()
    getattr(action.type, f"phoneActionField{number}").SetInParent()
    return action.SerializeToString()


def _phone(
    name: str,
    android_id: int,
    *,
    phone_canonic: str | None = None,
    direct_canonic: str | None = None,
    id_type: int = DeviceUpdate_pb2.IDENTIFIER_ANDROID,
    actions: tuple[int, ...] = _ALL_224F_ACTIONS,
    model: str = "SM-S938B",
) -> DeviceUpdate_pb2.DeviceMetadata:
    device = DeviceUpdate_pb2.DeviceMetadata()
    device.userDefinedDeviceName = name
    ident = device.identifierInformation
    ident.type = id_type
    ident.phoneInformation.phoneInformationField1 = android_id
    if phone_canonic:
        ident.phoneInformation.canonicIds.canonicId.add().id = phone_canonic
    if direct_canonic:
        ident.canonicIds.canonicId.add().id = direct_canonic
    for number in actions:
        device.supportedActions.append(_action_bytes(number))
    info = DeviceUpdate_pb2.DeviceMetadata.PhoneInformation()
    info.modelNumber = model
    device.phoneInformation = info.SerializeToString()
    device.metadataField11 = str(android_id).encode()
    return device


def _tracker(name: str, canonic: str | None) -> DeviceUpdate_pb2.DeviceMetadata:
    device = DeviceUpdate_pb2.DeviceMetadata()
    device.userDefinedDeviceName = name
    device.identifierInformation.type = DeviceUpdate_pb2.IDENTIFIER_SPOT
    if canonic:
        device.identifierInformation.canonicIds.canonicId.add().id = canonic
    device.information.deviceRegistration.manufacturer = "Motorola"
    return device


def _listing(*devices: DeviceUpdate_pb2.DeviceMetadata) -> DeviceUpdate_pb2.DevicesList:
    listing = DeviceUpdate_pb2.DevicesList()
    for device in devices:
        listing.deviceMetadata.add().CopyFrom(device)
    return listing


def _id_for(key: bytes) -> Any:
    from custom_components.googlefindmy.phone_identity import phone_id_from_android_id

    return lambda android_id: phone_id_from_android_id(key, android_id)


def _rows(
    listing: DeviceUpdate_pb2.DevicesList, key: bytes | None
) -> list[dict[str, Any]]:
    return decoder.get_devices_with_location(
        listing,
        emit_canonicless_diagnostics=True,
        phone_id_for=_id_for(key) if key is not None else None,
    )


_KEY_A = bytes(range(32))
_KEY_B = bytes(range(1, 33))


# --- The predicate and the row, as a table ---------------------------------------


@pytest.mark.parametrize(
    ("case", "build", "expect"),
    [
        ("phone without canonic id", lambda a: _phone("Phone", a), "phone"),
        (
            "phone with canonic id",
            lambda a: _phone("Phone", a, phone_canonic="cid-phone"),
            "cid-phone",
        ),
        ("tracker with canonic id", lambda a: _tracker("Tag", "cid-tag"), "cid-tag"),
        ("tracker without canonic id", lambda a: _tracker("Tag", None), None),
        (
            "android id on a non-android entry",
            lambda a: _phone("Odd", a, id_type=DeviceUpdate_pb2.IDENTIFIER_SPOT),
            None,
        ),
        (
            "android phone with a direct canonic id only",
            lambda a: _phone("Phone", a, direct_canonic="cid-direct"),
            None,
        ),
        ("android phone with android id 0", lambda a: _phone("Phone", 0), None),
    ],
)
def test_table(case: str, build: Any, expect: str | None) -> None:
    android_id = _random_android_id()
    device = build(android_id)
    rows = _rows(_listing(device), _KEY_A)
    if expect is None:
        assert rows == [], case
        assert decoder.canonicless_android_id(device) is None, case
        return
    assert len(rows) == 1, case
    row = rows[0]
    if expect == "phone":
        assert decoder.canonicless_android_id(device) == android_id
        assert row["id"].startswith(PHONE_ID_PREFIX)
        assert row["id"] == row["device_id"]
        assert row["locate_route"] == "phone"
        assert row["identifier_type"] == "android"
        assert row["last_seen"] is None
        assert row["name"] == "Phone"
    else:
        assert decoder.canonicless_android_id(device) is None, case
        assert row["id"] == expect
        assert "locate_route" not in row


def test_224f_shaped_entry_gives_one_row_without_the_android_id() -> None:
    """The anonymised observed entry: a phone row whose fields never hold the ID."""
    android_id = _random_android_id()
    rows = _rows(_listing(_phone("Galaxy", android_id)), _KEY_A)
    assert len(rows) == 1
    text = repr(rows)
    for form in (str(android_id), f"{android_id:x}", f"{android_id:X}"):
        assert form not in text
    unkeyed = hashlib.sha256(str(android_id).encode()).hexdigest()
    assert unkeyed[:PHONE_ID_HEX_LEN] not in text


def test_unique_id_is_built_from_the_phone_id() -> None:
    from custom_components.googlefindmy.coordinator.helpers.registry import (
        build_canonical_unique_id,
    )

    android_id = _random_android_id()
    row = _rows(_listing(_phone("Galaxy", android_id)), _KEY_A)[0]
    unique_id = build_canonical_unique_id("entry", "sub", row["id"])
    assert unique_id == f"entry:sub:{row['id']}"
    assert str(android_id) not in unique_id
    assert len(row["id"]) == len(PHONE_ID_PREFIX) + PHONE_ID_HEX_LEN
    int(row["id"][len(PHONE_ID_PREFIX) :], 16)


def test_second_android_model_gives_its_own_row() -> None:
    first, second = _random_android_id(), _random_android_id()
    rows = _rows(
        _listing(_phone("Phone", first), _phone("Tablet", second, model="SM-X910")),
        _KEY_A,
    )
    assert [r["name"] for r in rows] == ["Phone", "Tablet"]
    assert rows[0]["id"] != rows[1]["id"]


def test_same_android_id_in_two_accounts() -> None:
    """Account A lists the phone with a canonic id, account B without (G-6)."""
    android_id = _random_android_id()
    rows_a = _rows(_listing(_phone("S25", android_id, phone_canonic="cid-a")), _KEY_A)
    rows_b = _rows(_listing(_phone("S25", android_id)), _KEY_B)
    assert [r["id"] for r in rows_a] == ["cid-a"]
    assert "locate_route" not in rows_a[0]
    assert rows_b[0]["locate_route"] == "phone"
    assert rows_a[0]["id"] != rows_b[0]["id"]
    # Same phone without canonic id in both accounts: still two different ids.
    rows_a2 = _rows(_listing(_phone("S25", android_id)), _KEY_A)
    assert rows_a2[0]["id"] != rows_b[0]["id"]


def test_phone_row_is_visible_and_not_counted_canonicless() -> None:
    android_id = _random_android_id()
    rows = _rows(_listing(_phone("Phone", android_id), _tracker("Tag", None)), _KEY_A)
    assert [r["name"] for r in rows] == ["Phone"]
    assert decoder.get_canonicless_counts("<no-entry>") == {
        "total": 1,
        "benign": 0,
        "warn": 1,
    }
    assert decoder.get_canonicless_name_counts("<no-entry>") == {
        ("Tag", decoder.CANONICLESS_KIND_TRACKER): 1
    }
    assert "Phone" in decoder._visible_device_names_by_entry["<no-entry>"]


def test_without_phone_id_for_the_phone_drops_as_before() -> None:
    android_id = _random_android_id()
    rows = decoder.get_devices_with_location(
        _listing(_phone("Phone", android_id)), emit_canonicless_diagnostics=True
    )
    assert rows == []
    assert decoder.get_canonicless_name_counts("<no-entry>") == {
        ("Phone", decoder.CANONICLESS_KIND_ANDROID): 1
    }


def test_failing_phone_id_for_drops_the_phone_without_crashing() -> None:
    def _broken(_android_id: int) -> str:
        raise ValueError("no key")

    rows = decoder.get_devices_with_location(
        _listing(_phone("Phone", _random_android_id()), _tracker("Tag", "cid-tag")),
        emit_canonicless_diagnostics=True,
        phone_id_for=_broken,
    )
    assert [r["id"] for r in rows] == ["cid-tag"]
    assert decoder.get_canonicless_name_counts("<no-entry>") == {
        ("Phone", decoder.CANONICLESS_KIND_ANDROID): 1
    }


@pytest.mark.parametrize(
    ("actions", "can_ring", "can_stop_ring"),
    [
        (_ALL_224F_ACTIONS, True, True),
        ((2, 4, 5), False, True),
        ((1, 4), True, False),
        ((), False, False),
    ],
)
def test_ring_capabilities_follow_supported_actions(
    actions: tuple[int, ...], can_ring: bool, can_stop_ring: bool
) -> None:
    row = _rows(
        _listing(_phone("Phone", _random_android_id(), actions=actions)), _KEY_A
    )[0]
    assert row["can_ring"] is can_ring
    assert row["can_stop_ring"] is can_stop_ring


def test_supported_action_numbers_skip_garbage() -> None:
    device = _phone("Phone", _random_android_id(), actions=(1, 5))
    device.supportedActions.append(b"\xff\xff\xff")
    assert decoder.supported_action_numbers(device) == frozenset({1, 5})


def test_supported_action_numbers_skip_an_entry_without_type() -> None:
    device = _phone("Phone", _random_android_id(), actions=(1,))
    device.supportedActions.append(
        DeviceUpdate_pb2.DeviceMetadata.SupportedActions().SerializeToString()
    )
    assert decoder.supported_action_numbers(device) == frozenset({1})


def test_canonicless_android_id_rejects_unreadable_or_foreign_shapes() -> None:
    from types import SimpleNamespace

    class _Raising:
        @property
        def identifierInformation(self) -> Any:
            raise AttributeError("identifierInformation")

    def _shaped(value: Any) -> Any:
        no_ids = SimpleNamespace(canonicIds=SimpleNamespace(canonicId=[]))
        phone_info = SimpleNamespace(
            canonicIds=no_ids.canonicIds, phoneInformationField1=value
        )
        ident = SimpleNamespace(
            type=DeviceUpdate_pb2.IDENTIFIER_ANDROID,
            phoneInformation=phone_info,
            canonicIds=no_ids.canonicIds,
        )
        return SimpleNamespace(identifierInformation=ident)

    assert decoder.canonicless_android_id(_Raising()) is None
    assert decoder.canonicless_android_id(_shaped(True)) is None
    assert decoder.canonicless_android_id(_shaped("12345")) is None
    assert decoder.canonicless_android_id(_shaped(7)) == 7  # the shape itself is valid


def test_an_unreadable_entry_leaves_a_record_without_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The broad handler records the error class, never the error text."""

    class _Raising:
        @property
        def identifierInformation(self) -> Any:
            raise ValueError("4711471147114711471")

    caplog.set_level(logging.DEBUG, logger="custom_components.googlefindmy")
    assert decoder.canonicless_android_id(_Raising()) is None
    records = [r for r in caplog.records if "canonical ID" in r.getMessage()]
    assert len(records) == 1
    assert "ValueError" in records[0].getMessage()
    assert "4711471147114711471" not in caplog.text


@pytest.mark.parametrize("phone_id", [None, 42, "no-prefix"])
def test_phone_row_needs_a_prefixed_string_id(phone_id: Any) -> None:
    device = _phone("Phone", _random_android_id())
    assert decoder._build_phone_row(device, "Phone", lambda _aid: phone_id) is None


def test_other_rows_are_unchanged_by_phone_id_for() -> None:
    """Snapshot: every other row stays as it was when a phone row joins."""
    listing = _listing(
        _tracker("Tag", "cid-tag"),
        _tracker("Lost", None),
        _phone("Phone", _random_android_id(), phone_canonic="cid-phone"),
        _phone("Bare", _random_android_id()),
    )
    before = decoder.get_devices_with_location(
        listing, emit_canonicless_diagnostics=True
    )
    decoder._reset_canonicless_warning_state()
    after = _rows(listing, _KEY_A)
    others = [r for r in after if r.get("locate_route") != "phone"]
    assert others == before
    assert [r["id"] for r in before] == ["cid-tag", "cid-phone"]
    assert [r["name"] for r in after if r.get("locate_route") == "phone"] == ["Bare"]


def test_probe_pass_without_phone_id_for_has_no_phone_rows() -> None:
    from custom_components.googlefindmy.api import _build_can_ring_index

    index = _build_can_ring_index(_listing(_phone("Phone", _random_android_id())))
    assert index == {}


def test_android_id_never_reaches_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    android_id = _random_android_id()
    caplog.set_level(logging.DEBUG, logger="custom_components.googlefindmy")
    _rows(_listing(_phone("Phone", android_id)), _KEY_A)
    decoder.get_devices_with_location(
        _listing(_phone("Phone", android_id)), emit_canonicless_diagnostics=True
    )

    def _broken(_android_id: int) -> str:
        raise ValueError(f"cannot map {_android_id}")

    decoder.get_devices_with_location(
        _listing(_phone("Phone", android_id)),
        emit_canonicless_diagnostics=True,
        phone_id_for=_broken,
    )
    assert caplog.records
    forms = (str(android_id), f"{android_id:x}", f"{android_id:X}")
    for record in caplog.records:
        text = " ".join((record.getMessage(), record.exc_text or "", repr(record.args)))
        assert not any(form in text for form in forms)


def test_blank_canonic_ids_count_as_absent() -> None:
    android_id = _random_android_id()
    device = _phone("Phone", android_id)
    device.identifierInformation.phoneInformation.canonicIds.canonicId.add().id = ""
    device.identifierInformation.canonicIds.canonicId.add().id = ""
    assert decoder.canonicless_android_id(device) == android_id
    assert _rows(_listing(device), _KEY_A)[0]["locate_route"] == "phone"


def test_same_android_id_twice_in_one_list_gives_one_id() -> None:
    android_id = _random_android_id()
    rows = _rows(
        _listing(_phone("Phone", android_id), _phone("Phone", android_id)), _KEY_A
    )
    assert len({r["id"] for r in rows}) == 1


# --- Id stability, removal and the name guard ------------------------------------


def test_new_android_id_new_id_and_rename_keeps_id() -> None:
    android_id = _random_android_id()
    first = _rows(_listing(_phone("Phone", android_id)), _KEY_A)[0]["id"]
    renamed = _rows(_listing(_phone("Renamed", android_id)), _KEY_A)[0]["id"]
    other = _rows(_listing(_phone("Phone", _random_android_id())), _KEY_A)[0]["id"]
    assert renamed == first
    assert other != first


def _plan(
    device: Any,
    listing: DeviceUpdate_pb2.DevicesList,
    key: bytes | None,
    state: dict[str, Any],
) -> Any:
    from custom_components.googlefindmy.coordinator.helpers.registry import (
        plan_stale_device_removals,
    )

    rows = _rows(listing, key)
    counts = decoder.get_canonicless_counts("<no-entry>")
    plan = plan_stale_device_removals(
        [device],
        entry_id="e1",
        domain="googlefindmy",
        service_identifiers=(),
        fresh_list_ids={r["id"] for r in rows},
        dropped_candidates=0,
        canonicless_warn=counts["warn"],
        canonicless_names=decoder.get_canonicless_name_counts("<no-entry>"),
        previous_misses=state.get("misses", {}),
        name_baseline=state.get("baseline"),
    )
    state["misses"] = plan.misses
    state["baseline"] = plan.name_baseline
    return plan


def _registry_device(phone_id: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id="dev-phone",
        name="Phone",
        entry_type=None,
        identifiers={("googlefindmy", f"e1:sub:{phone_id}")},
        config_entries={"e1"},
        config_entries_subentries={"e1": {"sub"}},
        config_entry_id="e1",
    )


def test_phone_missing_from_two_lists_is_removed() -> None:
    android_id = _random_android_id()
    tag = _tracker("Tag", "cid-tag")
    phone_id = _rows(_listing(_phone("Phone", android_id)), _KEY_A)[0]["id"]
    device = _registry_device(phone_id)
    state: dict[str, Any] = {}
    assert not _plan(
        device, _listing(tag, _phone("Phone", android_id)), _KEY_A, state
    ).first_misses
    first = _plan(device, _listing(tag), _KEY_A, state)
    assert [d.id for d, _ in first.first_misses] == ["dev-phone"]
    second = _plan(device, _listing(tag), _KEY_A, state)
    assert [d.id for d, _ in second.remove] == ["dev-phone"]


def test_without_key_the_name_guard_keeps_the_phone() -> None:
    android_id = _random_android_id()
    tag = _tracker("Tag", "cid-tag")
    phone_id = _rows(_listing(_phone("Phone", android_id)), _KEY_A)[0]["id"]
    device = _registry_device(phone_id)
    state: dict[str, Any] = {}
    _plan(device, _listing(tag, _phone("Phone", android_id)), _KEY_A, state)
    for _ in range(3):
        plan = _plan(device, _listing(tag, _phone("Phone", android_id)), None, state)
        assert not plan.remove
        assert not plan.first_misses


# --- The per-entry HMAC key (phone_identity) ---------------------------------------


class _DictCache:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    async def async_get_cached_value(self, key: str) -> Any:
        return self.data.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.data[key] = value


@pytest.mark.asyncio
async def test_key_store_roundtrip_gives_the_same_id_after_restart() -> None:
    from custom_components.googlefindmy.phone_identity import (
        async_get_or_create_phone_id_key,
        phone_id_from_android_id,
    )

    storage: dict[str, Any] = {}
    android_id = _random_android_id()
    key = await async_get_or_create_phone_id_key(_DictCache(storage))
    stored = storage[PHONE_ID_KEY_CACHE_NAME]
    assert isinstance(stored, str) and len(stored) == 64
    assert bytes.fromhex(stored) == key
    again = await async_get_or_create_phone_id_key(_DictCache(storage))
    assert again == key
    assert phone_id_from_android_id(again, android_id) == phone_id_from_android_id(
        key, android_id
    )
    fresh = await async_get_or_create_phone_id_key(_DictCache({}))
    assert phone_id_from_android_id(fresh, android_id) != phone_id_from_android_id(
        key, android_id
    )


@pytest.mark.parametrize("stored", ["zz" * 32, "ab" * 16, 12345])
@pytest.mark.asyncio
async def test_unreadable_stored_key_is_replaced(
    stored: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from custom_components.googlefindmy.phone_identity import (
        async_get_or_create_phone_id_key,
    )

    storage: dict[str, Any] = {PHONE_ID_KEY_CACHE_NAME: stored}
    key = await async_get_or_create_phone_id_key(_DictCache(storage))
    assert len(key) == 32
    assert storage[PHONE_ID_KEY_CACHE_NAME] == key.hex()
    assert all(key.hex() not in r.getMessage() for r in caplog.records)


def test_phone_id_is_keyed_not_a_plain_hash() -> None:
    from custom_components.googlefindmy.phone_identity import phone_id_from_android_id

    android_id = _random_android_id()
    plain = hashlib.sha256(str(android_id).encode()).hexdigest()[:PHONE_ID_HEX_LEN]
    assert phone_id_from_android_id(_KEY_A, android_id) != PHONE_ID_PREFIX + plain
    with pytest.raises(ValueError):
        phone_id_from_android_id(b"short", android_id)


# --- The target table in api._process_device_list_response ----------------------


class _StubCache:
    entry_id = "phone-entry"

    async def async_get_cached_value(self, key: str) -> Any:
        return None

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        return None


def test_api_fills_and_replaces_the_phone_target_table() -> None:
    from custom_components.googlefindmy.api import GoogleFindMyAPI

    api = GoogleFindMyAPI(cache=_StubCache())
    first, second = _random_android_id(), _random_android_id()

    rows = api._process_device_list_response(
        _listing(_phone("Phone", first)).SerializeToString().hex()
    )
    assert rows == [] and api._phone_targets == {}

    api._phone_id_key = _KEY_A
    rows = api._process_device_list_response(
        _listing(_phone("Phone", first), _tracker("Tag", "cid-tag"))
        .SerializeToString()
        .hex()
    )
    phone_rows = [r for r in rows if r.get("locate_route") == "phone"]
    assert len(phone_rows) == 1
    assert api._phone_targets == {phone_rows[0]["id"]: first}
    assert phone_rows[0]["can_ring"] is True
    assert str(first) not in repr(rows)

    api._process_device_list_response(
        _listing(_phone("Other", second)).SerializeToString().hex()
    )
    assert list(api._phone_targets.values()) == [second]


@pytest.mark.asyncio
async def test_api_without_a_readable_cache_has_no_phone_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy.api import GoogleFindMyAPI

    class _Broken(_DictCache):
        async def async_get_cached_value(self, key: str) -> Any:
            raise OSError("store unavailable")

    api = GoogleFindMyAPI(cache=_StubCache())
    monkeypatch.setattr(api, "_decoder_token_cache", lambda: _Broken({}))
    assert await api._async_load_phone_id_key() is None
    rows = api._process_device_list_response(
        _listing(_phone("Phone", _random_android_id())).SerializeToString().hex()
    )
    assert rows == [] and api._phone_targets == {}


@pytest.mark.asyncio
async def test_two_overlapping_first_lists_create_one_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy.api import GoogleFindMyAPI

    entered = asyncio.Event()
    release = asyncio.Event()

    class _Slow(_DictCache):
        async def async_set_cached_value(self, key: str, value: Any) -> None:
            entered.set()
            await release.wait()
            await super().async_set_cached_value(key, value)

    storage: dict[str, Any] = {}
    api = GoogleFindMyAPI(cache=_StubCache())
    monkeypatch.setattr(api, "_decoder_token_cache", lambda: _Slow(storage))

    first_task = asyncio.create_task(api._async_load_phone_id_key())
    await entered.wait()  # the first load is inside the write
    second_task = asyncio.create_task(api._async_load_phone_id_key())
    # Released before the second load first runs: without the lock it would read
    # the empty store ahead of the first write and create a second key.
    release.set()
    first, second = await asyncio.gather(first_task, second_task)
    assert first is not None and first == second
    assert storage[PHONE_ID_KEY_CACHE_NAME] == first.hex()


def test_key_name_is_redacted_in_diagnostics() -> None:
    from custom_components.googlefindmy.diagnostics import TO_REDACT

    assert PHONE_ID_KEY_CACHE_NAME in TO_REDACT


@pytest.mark.asyncio
async def test_api_loads_the_key_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy import api as api_module
    from custom_components.googlefindmy.api import GoogleFindMyAPI

    storage: dict[str, Any] = {}
    api = GoogleFindMyAPI(cache=_StubCache())
    assert await api._async_load_phone_id_key() is None  # not a TokenCache

    monkeypatch.setattr(api, "_decoder_token_cache", lambda: _DictCache(storage))
    seen: list[bytes | None] = []

    async def _fake_request(*_a: Any, **_k: Any) -> str:
        return _listing(_phone("Phone", _random_android_id())).SerializeToString().hex()

    original = api._process_device_list_response

    def _spy(result_hex: str) -> list[dict[str, Any]]:
        seen.append(api._phone_id_key)
        return original(result_hex)

    monkeypatch.setattr(api_module, "async_request_device_list", _fake_request)
    monkeypatch.setattr(api, "_process_device_list_response", _spy)
    rows = await api.async_get_basic_device_list("user@example.com")
    assert seen and seen[0] is not None
    assert seen[0].hex() == storage[PHONE_ID_KEY_CACHE_NAME]
    assert rows[0]["id"].startswith(PHONE_ID_PREFIX)
