import asyncio
from types import SimpleNamespace

import pytest

import custom_components.googlefindmy.coordinator as coordinator_module
import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EID_LENGTH,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import ROTATION_PERIOD


def _fixed_length_eid(key: bytes, timestamp: int) -> bytes:
    """Generate a deterministic 20-byte EID for test fixtures."""

    ts_bytes = timestamp.to_bytes(8, "big", signed=False)
    seed = key + ts_bytes
    return seed.ljust(EID_LENGTH, b"\x00")[:EID_LENGTH]


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


class _StubHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    def async_create_task(self, coro: asyncio.Future, name: str | None = None) -> asyncio.Task:
        return asyncio.create_task(coro, name=name)


@pytest.mark.asyncio
async def test_active_device_identities_prefer_registry_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-1", registry_id="registry-1", custom_fields={"identity_key": "0f0e0d"})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")
    coordinator._enabled_poll_device_ids = {"dev-1"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(device, "identifier", None)
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
    coordinator._extract_our_identifier = lambda device: getattr(device, "identifier", None)
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
        [_StubDevice("dev-3", registry_id="registry-3", custom_fields={"identity_key": "1234"})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-3")
    coordinator._enabled_poll_device_ids = {"dev-3"}
    coordinator._get_ignored_set = lambda: {"dev-3"}
    coordinator._extract_our_identifier = lambda device: getattr(device, "identifier", None)
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert identities == []


@pytest.mark.asyncio
async def test_resolver_refreshes_all_rotation_windows(monkeypatch: pytest.MonkeyPatch) -> None:
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
    hass = _StubHass({DOMAIN: {"entries": {"entry-4": SimpleNamespace(coordinator=coordinator)}}})

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None

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
async def test_resolver_aggregates_multiple_entries(monkeypatch: pytest.MonkeyPatch) -> None:
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

    coordinator_one = SimpleNamespace(get_active_device_identities=lambda: [identity_one])
    coordinator_two = SimpleNamespace(get_active_device_identities=lambda: [identity_two])
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

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    eid_one = _fixed_length_eid(identity_one.identity_key, rotation_start)
    eid_two = _fixed_length_eid(identity_two.identity_key, rotation_start)

    assert resolver.resolve_eid(eid_one).config_entry_id == "entry-a"
    assert resolver.resolve_eid(eid_two).config_entry_id == "entry-b"


@pytest.mark.asyncio
async def test_resolver_excludes_disabled_or_ignored_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice("dev-1", registry_id="registry-1", custom_fields={"identity_key": "abcd"}, disabled=True),
            _StubDevice("dev-2", registry_id="registry-2", custom_fields={"identity_key": "dcba"}),
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
    coordinator._extract_our_identifier = lambda device: getattr(device, "identifier", None)
    coordinator.data = []
    coordinator._device_location_data = {}

    hass = _StubHass({DOMAIN: {"entries": {"entry-ignore": SimpleNamespace(coordinator=coordinator)}}})

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert resolver._lookup == {}


@pytest.mark.asyncio
async def test_concurrent_refresh_requests_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(resolver_module.asyncio, "sleep", lambda delay: _delayed_refresh(delay))

    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="canonical-lock",
        identity_key=b"\x0a",
        config_entry_id="entry-lock",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass({DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}})

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await asyncio.gather(resolver.async_refresh(), resolver.async_refresh(), resolver.async_refresh())

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_eid = _fixed_length_eid(identity.identity_key, rotation_start)
    assert expected_eid in resolver._lookup


@pytest.mark.asyncio
async def test_resolver_learns_offsets_and_endianness(monkeypatch: pytest.MonkeyPatch) -> None:
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
    hass = _StubHass({DOMAIN: {"entries": {"entry-learn": SimpleNamespace(coordinator=coordinator)}}})

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
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

    def _fake_generate_eid_p256(
        key: bytes, timestamp: int, rotation_period: int = 3600
    ) -> bytes:
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
    hass = _StubHass({DOMAIN: {"entries": {"entry-modern": SimpleNamespace(coordinator=coordinator)}}})

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._known_offsets = {identity.registry_id: 0}
    resolver._known_endianness = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    legacy_eid = _fake_generate_eid(identity.identity_key, rotation_start)
    modern_eid = _fake_generate_eid_p256(identity.identity_key, rotation_start)[:20]

    assert len(resolver._lookup) == 6
    assert resolver.resolve_eid(legacy_eid).device_id == identity.registry_id
    match = resolver.resolve_eid(modern_eid)
    assert match is not None
    assert match.device_id == identity.registry_id

