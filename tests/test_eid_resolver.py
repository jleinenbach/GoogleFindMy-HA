import asyncio
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import custom_components.googlefindmy.coordinator as coordinator_module
import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EID_LENGTH,
    LOCK_CUSTOM_FIELD,
    EIDMatch,
    GoogleFindMyEIDResolver,
    TimebaseLabel,
    _build_timebase_candidates,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import ROTATION_PERIOD


def _fixed_length_eid(key: bytes, timestamp: int) -> bytes:
    """Generate a deterministic 20-byte EID for test fixtures."""

    ts_bytes = timestamp.to_bytes(8, "big", signed=False)
    seed = key + ts_bytes
    return seed.ljust(EID_LENGTH, b"\x00")[:EID_LENGTH]


def _build_resolver() -> GoogleFindMyEIDResolver:
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._persisted_locks = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    return resolver


class _StubDevice:
    def __init__(
        self,
        identifier: str,
        *,
        registry_id: str | None = None,
        custom_fields: dict | None = None,
        disabled: bool = False,
    ) -> None:
        self.identifier = identifier
        self.id = registry_id or identifier
        self.custom_fields = custom_fields
        self.disabled_by = "user" if disabled else None


class _StubDeviceRegistry:
    def __init__(self, devices: list[_StubDevice]) -> None:
        self._devices = devices

    def async_entries_for_config_entry(self, _entry_id: str) -> list[_StubDevice]:
        return list(self._devices)

    def async_get(self, device_id: str) -> _StubDevice | None:
        return next(
            (device for device in self._devices if device.id == device_id), None
        )

    def async_update_device(
        self, device_id: str, *, custom_fields: dict | None = None
    ) -> _StubDevice | None:
        device = self.async_get(device_id)
        if device is None:
            return None
        if custom_fields is not None:
            device.custom_fields = custom_fields
        return device


class _StubHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    def async_create_task(
        self, coro: asyncio.Future, name: str | None = None
    ) -> asyncio.Task:
        return asyncio.create_task(coro, name=name)


@pytest.mark.asyncio
async def test_active_device_identities_prefer_registry_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-1",
                registry_id="registry-1",
                custom_fields={"identity_key": "0f0e0d"},
            )
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")
    coordinator._enabled_poll_device_ids = {"dev-1"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.identity_key == bytes.fromhex("0f0e0d")
    assert identity.config_entry_id == "entry-1"
    assert identity.registry_id == "registry-1"
    assert identity.canonical_id == "dev-1"


@pytest.mark.asyncio
async def test_active_device_identities_fall_back_to_location_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-2", registry_id="registry-2", custom_fields={})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-2")
    coordinator._enabled_poll_device_ids = {"dev-2"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {"dev-2": {"identityKey": "abcd"}}

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.registry_id == "registry-2"
    assert identity.canonical_id == "dev-2"
    assert identity.identity_key == bytes.fromhex("abcd")
    assert identity.config_entry_id == "entry-2"


@pytest.mark.asyncio
async def test_active_device_identities_ignore_opt_out_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-3",
                registry_id="registry-3",
                custom_fields={"identity_key": "1234"},
            )
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-3")
    coordinator._enabled_poll_device_ids = {"dev-3"}
    coordinator._get_ignored_set = lambda: {"dev-3"}
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert identities == []


@pytest.mark.asyncio
async def test_active_device_identities_surface_registry_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-meta",
                registry_id="registry-meta",
                custom_fields={
                    "identity_key": "0f0e0d",
                    "pair_date": {"seconds": 1234, "nanos": 500_000_000},
                    "encrypted_user_secrets": {
                        "creationDate": {"seconds": 2468, "nanos": 0}
                    },
                    "timeAnchorsDebug": {"hint": "anchor"},
                },
            )
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-meta")
    coordinator._enabled_poll_device_ids = {"dev-meta"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == 1234
    assert identity.secrets_creation_date == 2468
    assert identity.time_anchors_debug == {"hint": "anchor"}


def test_build_timebase_candidates_with_time_anchor_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = ROTATION_PERIOD * 8
    identity = DeviceIdentity(
        registry_id="registry-anchor",
        canonical_id="device-anchor",
        identity_key=b"\x01",
        time_anchors_debug={
            "anchors": [
                {"label": "REL_DEBUG_HINT", "anchor_epoch": now - ROTATION_PERIOD}
            ]
        },
    )

    candidates = _build_timebase_candidates(
        identity,
        now=now,
        provisioning_counter=now,
        primary_anchor_epoch=None,
    )

    assert any(candidate.label == "REL_DEBUG_HINT" for candidate in candidates)
    debug_candidate = next(
        candidate for candidate in candidates if candidate.label == "REL_DEBUG_HINT"
    )
    assert debug_candidate.anchor_epoch == now - ROTATION_PERIOD


def test_build_timebase_candidates_ignores_unparsed_anchor_list() -> None:
    now = ROTATION_PERIOD * 9
    identity = DeviceIdentity(
        registry_id="registry-anchor-list",
        canonical_id="device-anchor-list",
        identity_key=b"\x02",
        time_anchors_debug=[{"unexpected": True}],
    )

    candidates = _build_timebase_candidates(
        identity,
        now=now,
        provisioning_counter=now,
        primary_anchor_epoch=None,
    )

    labels = {candidate.label for candidate in candidates}
    assert labels == {TimebaseLabel.ABSOLUTE}


@pytest.mark.asyncio
async def test_active_device_identities_surface_cached_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-cache", registry_id="registry-cache", custom_fields={})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-cache")
    coordinator._enabled_poll_device_ids = {"dev-cache"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {
        "dev-cache": {
            "identityKey": "abcd",
            "deviceRegistration": {"pairDate": {"seconds": 10}},
            "encrypted_user_secrets": {
                "creation_date": {"seconds": 20, "nanos": 999_000_000}
            },
            "time_anchors_debug": [1, 2, 3],
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == 10
    assert identity.secrets_creation_date == 20
    assert identity.time_anchors_debug == [1, 2, 3]


@pytest.mark.asyncio
async def test_resolver_refreshes_all_rotation_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 2050
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity = DeviceIdentity(
        registry_id="registry-4",
        canonical_id="device-1",
        identity_key=b"\x01\x02",
        config_entry_id="entry-4",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-4": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    earliest_timestamp = max(0, rotation_start - (ROTATION_PERIOD * 90))
    latest_timestamp = rotation_start + (ROTATION_PERIOD * 90)
    assert min(recorded_timestamps) == earliest_timestamp
    assert max(recorded_timestamps) == latest_timestamp
    expected_windows = ((latest_timestamp - earliest_timestamp) // ROTATION_PERIOD) + 1
    assert len(recorded_timestamps) == expected_windows

    expected_eid = _fixed_length_eid(identity.identity_key, rotation_start)
    match = resolver.resolve_eid(expected_eid)
    assert match is not None
    assert match.device_id == "registry-4"
    assert match.canonical_id == "device-1"
    assert expected_eid[::-1] in resolver._lookup
    assert len(resolver._lookup) == len(recorded_timestamps) * 2
    assert resolver.get_resolved_eid(expected_eid) == "registry-4"
    assert resolver.resolve_eid(b"unknown") is None
    assert resolver.get_resolved_eid(b"unknown") is None


@pytest.mark.asyncio
async def test_resolver_aggregates_multiple_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 4096

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity_one = DeviceIdentity(
        registry_id="registry-1",
        canonical_id="can-1",
        identity_key=b"\x01",
        config_entry_id="entry-a",
    )
    identity_two = DeviceIdentity(
        registry_id="registry-2",
        canonical_id="can-2",
        identity_key=b"\x02",
        config_entry_id="entry-b",
    )

    coordinator_one = SimpleNamespace(
        get_active_device_identities=lambda: [identity_one]
    )
    coordinator_two = SimpleNamespace(
        get_active_device_identities=lambda: [identity_two]
    )
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {
                    "entry-a": SimpleNamespace(coordinator=coordinator_one),
                    "entry-b": SimpleNamespace(coordinator=coordinator_two),
                }
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    eid_one = _fixed_length_eid(identity_one.identity_key, rotation_start)
    eid_two = _fixed_length_eid(identity_two.identity_key, rotation_start)

    assert resolver.resolve_eid(eid_one).config_entry_id == "entry-a"
    assert resolver.resolve_eid(eid_two).config_entry_id == "entry-b"


@pytest.mark.asyncio
async def test_resolver_excludes_disabled_or_ignored_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-1",
                registry_id="registry-1",
                custom_fields={"identity_key": "abcd"},
                disabled=True,
            ),
            _StubDevice(
                "dev-2",
                registry_id="registry-2",
                custom_fields={"identity_key": "dcba"},
            ),
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-ignore")
    coordinator._enabled_poll_device_ids = {"dev-1", "dev-2"}
    coordinator._get_ignored_set = lambda: {"dev-2"}
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-ignore": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert resolver._lookup == {}


@pytest.mark.asyncio
async def test_concurrent_refresh_requests_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 8192

    def _fake_time() -> int:
        return base_time

    async def _delayed_refresh(_: float) -> None:
        await asyncio.sleep(0)

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    # Defensive: keep refresh serialization stable even if future implementations
    # introduce awaits inside the cache rebuild. The current refresh path does not
    # sleep, but the shim ensures concurrent refreshes would still join correctly.
    monkeypatch.setattr(
        resolver_module.asyncio, "sleep", lambda delay: _delayed_refresh(delay)
    )

    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="canonical-lock",
        identity_key=b"\x0a",
        config_entry_id="entry-lock",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await asyncio.gather(
        resolver.async_refresh(), resolver.async_refresh(), resolver.async_refresh()
    )

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_eid = _fixed_length_eid(identity.identity_key, rotation_start)
    assert expected_eid in resolver._lookup


@pytest.mark.asyncio
async def test_resolver_learns_offsets_and_endianness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 200
    current_time = base_time
    generated: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        generated.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity = DeviceIdentity(
        registry_id="registry-learn",
        canonical_id="canonical-learn",
        identity_key=b"\x0b",
        config_entry_id="entry-learn",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-learn": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    assert len(generated) == 181
    target_timestamp = rotation_start + (ROTATION_PERIOD * 2)
    expected_eid = _fixed_length_eid(identity.identity_key, target_timestamp)
    assert expected_eid in resolver._lookup

    match = resolver.resolve_eid(expected_eid[::-1])
    assert match is not None
    assert match.is_reversed is True
    assert resolver._known_endianness[identity.registry_id] is True
    assert resolver._known_offsets[identity.registry_id] == target_timestamp - base_time

    generated.clear()
    resolver._lookup = {}
    current_time = base_time + ROTATION_PERIOD

    await resolver._refresh_cache()

    target_rotation = current_time + resolver._known_offsets[identity.registry_id]
    target_rotation -= target_rotation % ROTATION_PERIOD
    assert set(generated) == {
        target_rotation,
        max(0, target_rotation - ROTATION_PERIOD),
        target_rotation + ROTATION_PERIOD,
    }
    assert len(resolver._lookup) == 3
    assert all(match.is_reversed for match in resolver._lookup.values())


@pytest.mark.asyncio
async def test_resolver_populates_modern_and_legacy_eids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 2

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return (timestamp.to_bytes(8, "big") * 3)[:EID_LENGTH]

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        return (b"m" + timestamp.to_bytes(8, "big") * 4)[:32]

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)

    identity = DeviceIdentity(
        registry_id="registry-modern",
        canonical_id="canonical-modern",
        identity_key=b"\x01" * 32,
        config_entry_id="entry-modern",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-modern": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._known_offsets = {identity.registry_id: 0}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    legacy_eid = _fake_generate_eid(identity.identity_key, rotation_start)
    modern_eid_full = _fake_generate_eid_p256(identity.identity_key, rotation_start)
    modern_eid_truncated = modern_eid_full[: resolver_module.EID_LENGTH]

    assert len(resolver._lookup) == 9
    assert resolver.resolve_eid(legacy_eid).device_id == identity.registry_id
    assert resolver.resolve_eid(modern_eid_full).device_id == identity.registry_id
    match = resolver.resolve_eid(modern_eid_truncated)
    assert match is not None
    assert match.device_id == identity.registry_id


def test_resolve_eid_slices_fmdn_frame() -> None:
    resolver = _build_resolver()

    expected_eid = b"\x01" * EID_LENGTH
    match = EIDMatch("device-1", "entry-1", "canonical-1", 0, False)
    resolver._lookup[expected_eid] = match

    framed_payload = bytes([resolver_module.FMDN_FRAME_TYPE]) + expected_eid + b"\x99"

    result = resolver.resolve_eid(framed_payload)

    assert result == match


def test_resolve_eid_accepts_raw_20_byte_payloads() -> None:
    resolver = _build_resolver()

    expected_eid = b"\x02" * EID_LENGTH
    match = EIDMatch("device-2", "entry-2", "canonical-2", 0, False)
    resolver._lookup[expected_eid] = match

    result = resolver.resolve_eid(expected_eid)

    assert result == match


def test_resolve_eid_rejects_unexpected_lengths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _build_resolver()

    caplog.set_level("DEBUG")

    assert resolver.resolve_eid(b"\x40\x01") is None
    assert any("Unexpected EID length" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_aesgcm_identity_key_unwrap_prefers_valid_shared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext_key = b"\xaa" * 32
    nonce = b"\x00" * 12
    shared_key = b"\x11" * 32
    wrong_owner_key = b"\x22" * 32

    envelope = nonce + AESGCM(shared_key).encrypt(nonce, plaintext_key, b"")

    resolver = _build_resolver()
    identity = DeviceIdentity(
        registry_id="registry-envelope",
        canonical_id="canonical-envelope",
        identity_key=None,
        encrypted_identity_key=envelope,
        config_entry_id="entry-envelope",
    )

    cache = SimpleNamespace()

    async def _fake_owner_key(cache: object) -> resolver_module.OwnerKeyInfo:
        return resolver_module.OwnerKeyInfo(wrong_owner_key, 1)

    async def _fake_shared_key(cache: object) -> bytes:
        return shared_key

    def _raise_invalid_tag(key: bytes, blob: bytes) -> bytes:
        raise InvalidTag("unwrap failed")

    monkeypatch.setattr(resolver_module, "async_get_owner_key", _fake_owner_key)
    monkeypatch.setattr(resolver_module, "async_get_shared_key", _fake_shared_key)
    monkeypatch.setattr(resolver_module, "decrypt_eik", _raise_invalid_tag)

    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == plaintext_key
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["mode"] == "aesgcm_envelope"
    assert result.metadata["key_source"] == "shared"
    assert result.metadata.get("key_sources") == ["owner", "shared"]


@pytest.mark.asyncio
async def test_aesgcm_unwrap_failure_falls_back_to_owner_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext_key = b"\xbb" * 32
    bogus_envelope = b"\x00" * 60
    owner_key = b"\x33" * 32
    decrypt_calls = 0

    resolver = _build_resolver()
    identity = DeviceIdentity(
        registry_id="registry-fallback",
        canonical_id="canonical-fallback",
        identity_key=None,
        encrypted_identity_key=bogus_envelope,
        config_entry_id="entry-fallback",
    )

    cache = SimpleNamespace()

    async def _fake_owner_key(cache: object) -> resolver_module.OwnerKeyInfo:
        return resolver_module.OwnerKeyInfo(owner_key, 1)

    async def _fake_shared_key(cache: object) -> bytes:
        return owner_key

    def _fake_decrypt(key: bytes, blob: bytes) -> bytes:
        nonlocal decrypt_calls
        decrypt_calls += 1
        return plaintext_key

    def _raise_invalid_tag(
        self: resolver_module.AESGCM, *_: object, **__: object
    ) -> bytes:
        raise InvalidTag("unwrap failed")

    monkeypatch.setattr(resolver_module, "async_get_owner_key", _fake_owner_key)
    monkeypatch.setattr(resolver_module, "async_get_shared_key", _fake_shared_key)
    monkeypatch.setattr(resolver_module, "decrypt_eik", _fake_decrypt)
    monkeypatch.setattr(resolver_module.AESGCM, "decrypt", _raise_invalid_tag)

    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == plaintext_key
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["mode"] == "owner_key"
    assert result.metadata["key_source"] == "owner"
    assert decrypt_calls == 1


@pytest.mark.asyncio
async def test_rel_pair_timebase_lock_reduces_scan_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 10
    current_time = base_time
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return (_fixed_length_eid(key, timestamp) + b"p256").ljust(32, b"p")

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)

    pair_date = base_time - ROTATION_PERIOD
    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="device-lock",
        identity_key=b"\x01\x02",
        config_entry_id="entry-lock",
        pair_date=pair_date,
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert recorded_timestamps

    counter_eids = [
        (eid, meta)
        for eid, meta in resolver._lookup_metadata.items()
        if str(meta.get("timestamp_basis", "")).startswith("counter")
    ]
    assert counter_eids

    match = resolver.resolve_eid(counter_eids[0][0])
    assert match is not None
    lock = resolver._known_timebases.get(identity.registry_id)
    assert lock is not None
    assert lock.label == str(counter_eids[0][1].get("timebase"))
    assert lock.anchor_epoch == pair_date

    recorded_timestamps.clear()
    current_time = base_time + ROTATION_PERIOD

    await resolver._refresh_cache()

    rotation_start = current_time - (current_time % ROTATION_PERIOD)
    expected_neighbors = {
        rotation_start,
        max(0, rotation_start - ROTATION_PERIOD),
        max(0, rotation_start - (2 * ROTATION_PERIOD)),
        rotation_start + ROTATION_PERIOD,
    }
    unix_windows = {
        meta.get("rotation_timestamp")
        for meta in resolver._lookup_metadata.values()
        if meta.get("timestamp_basis") == "unix"
    }
    assert unix_windows
    assert unix_windows <= expected_neighbors


@pytest.mark.asyncio
async def test_restores_persisted_timebase_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 12
    current_time = base_time
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    lock_payload = {
        "label": TimebaseLabel.REL_PAIR,
        "anchor_epoch": base_time - ROTATION_PERIOD,
        "rotation_timestamp": base_time - ROTATION_PERIOD,
        "time_offset": -ROTATION_PERIOD,
        "is_reversed": True,
    }

    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "device-lock",
                registry_id="registry-lock",
                custom_fields={LOCK_CUSTOM_FIELD: lock_payload},
            )
        ]
    )
    monkeypatch.setattr(resolver_module.dr, "async_get", lambda hass: registry)

    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="device-lock",
        identity_key=b"\x0a\x0b",
        config_entry_id="entry-lock",
    )
    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._persisted_locks = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    lock = resolver._known_timebases.get(identity.registry_id)
    assert lock is not None
    assert lock.label == TimebaseLabel.REL_PAIR
    assert resolver._known_endianness[identity.registry_id] is True

    recorded_timestamps.clear()
    current_time = base_time + ROTATION_PERIOD
    await resolver._refresh_cache()

    rotation_start = current_time - (current_time % ROTATION_PERIOD)
    expected_neighbors = {
        rotation_start,
        max(0, rotation_start - ROTATION_PERIOD),
        rotation_start + ROTATION_PERIOD,
    }
    assert set(recorded_timestamps) <= expected_neighbors


@pytest.mark.asyncio
async def test_persists_lock_state_on_match(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "device-persist", registry_id="registry-persist", custom_fields={}
            )
        ]
    )
    monkeypatch.setattr(resolver_module.dr, "async_get", lambda hass: registry)

    resolver = _build_resolver()
    resolver.hass = _StubHass({})

    metadata = {
        "timebase": TimebaseLabel.REL_PAIR,
        "anchor_epoch": 123,
        "rotation_timestamp": 456,
        "time_offset": -5,
    }
    eid = b"\x00" * EID_LENGTH
    match = EIDMatch(
        device_id="registry-persist",
        config_entry_id="entry-persist",
        canonical_id="device-persist",
        time_offset=-5,
        is_reversed=False,
    )
    resolver._lookup = {eid: match}
    resolver._lookup_metadata = {eid: metadata}

    result = resolver.resolve_eid(eid)
    assert result == match

    await asyncio.sleep(0)

    stored = registry.async_get("registry-persist").custom_fields.get(LOCK_CUSTOM_FIELD)
    assert stored == {
        "label": TimebaseLabel.REL_PAIR,
        "anchor_epoch": 123,
        "rotation_timestamp": 456,
        "time_offset": -5,
        "is_reversed": False,
    }


@pytest.mark.asyncio
async def test_skips_deep_scan_when_key_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 4
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity = DeviceIdentity(
        registry_id="registry-wrapped",
        canonical_id="device-wrapped",
        identity_key=b"\x01\x02",
        config_entry_id="entry-wrapped",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-wrapped": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {identity.canonical_id: {"status": "wrapped_failed"}}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert recorded_timestamps
    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_min = max(
        0, rotation_start + (min(resolver_module.NARROW_SCAN_RANGE) * ROTATION_PERIOD)
    )
    expected_max = rotation_start + (
        max(resolver_module.NARROW_SCAN_RANGE) * ROTATION_PERIOD
    )
    assert min(recorded_timestamps) == expected_min
    assert max(recorded_timestamps) == expected_max
