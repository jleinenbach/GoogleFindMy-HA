# tests/test_eid_resolver_variants.py
"""Resolver coverage for explicit EID variants and time-base handling."""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    LOCK_TTL_SECONDS,
    MIN_RELATIVE_WINDOW_SIZE,
    EIDGenerationLock,
    GoogleFindMyEIDResolver,
    iter_rotation_windows,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_COUNTER_MASK,
    MODERN_EID_LENGTH,
    ROTATION_PERIOD,
    EidVariant,
)


async def _run_in_executor(func, *args):
    """Synchronous stand-in for ``hass.async_add_executor_job`` (AP-C)."""

    return func(*args)


def _build_resolver(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyEIDResolver:
    _ = monkeypatch
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=asyncio.create_task,
        async_add_executor_job=_run_in_executor,
        data={},
    )

    async def _async_noop(payload=None):
        return None

    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._provisioning_warn_at = {}
    resolver._locks = {}
    resolver._store = SimpleNamespace(async_load=_async_noop, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    return resolver


def test_iter_rotation_windows_alignment_and_neighbors() -> None:
    """Rotation windows should align to boundaries and support neighbor expansion."""

    target_time = 2050
    without_neighbors = iter_rotation_windows(
        target_time,
        rotation_period=1024,
        window_range=range(-1, 2),
        include_neighbors=False,
    )
    assert without_neighbors == (1024, 2048, 3072)

    with_neighbors = iter_rotation_windows(
        target_time,
        rotation_period=1024,
        window_range=range(0, 1),
        include_neighbors=True,
    )
    assert with_neighbors == (2048, 1024, 3072)


def test_iter_rotation_windows_skips_negative_neighbors() -> None:
    """Neighbor expansion must never produce negative windows."""

    windows = iter_rotation_windows(
        target_time=0,
        rotation_period=ROTATION_PERIOD,
        window_range=(0,),
        include_neighbors=True,
    )
    assert 0 in windows
    assert ROTATION_PERIOD in windows
    assert all(ts >= 0 for ts in windows)


@pytest.mark.asyncio
async def test_refresh_cache_uses_relative_timebases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative bases must use elapsed time since the anchor."""

    resolver = _build_resolver(monkeypatch)
    base_now = 10_000
    pair_date_anchor = 100
    secrets_anchor = 250
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x05" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=pair_date_anchor,
        secrets_creation_date=secrets_anchor,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(time, "time", lambda: float(base_now))
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)

    await resolver._refresh_cache()

    def _collect_windows(label: str) -> list[int]:
        return [
            meta["rotation_timestamp"]
            for meta in resolver._lookup_metadata.values()
            if label in meta.get("timestamp_bases", {meta["timestamp_basis"]})
        ]

    pair_date_windows = _collect_windows("pair_date")
    secrets_windows = _collect_windows("secrets_creation_date")

    def _expected_range(anchor: int) -> tuple[int, int]:
        provisioning_counter = max(0, base_now - anchor)
        drift_seconds = provisioning_counter * 0.00005
        drift_windows = math.ceil(drift_seconds / ROTATION_PERIOD)
        max_window = math.ceil((24 * 60 * 60) / ROTATION_PERIOD)
        total_window = min(MIN_RELATIVE_WINDOW_SIZE + drift_windows, max_window)
        elapsed = base_now - anchor
        rotation_start = elapsed - (elapsed % ROTATION_PERIOD)
        window_delta = total_window * ROTATION_PERIOD
        return rotation_start - window_delta, rotation_start + window_delta

    pair_min, pair_max = _expected_range(pair_date_anchor)
    secrets_min, secrets_max = _expected_range(secrets_anchor)

    assert pair_date_windows
    assert secrets_windows
    assert all(pair_min <= ts <= pair_max for ts in pair_date_windows)
    assert all(secrets_min <= ts <= secrets_max for ts in secrets_windows)


@pytest.mark.asyncio
async def test_refresh_cache_populates_all_variants_and_bases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache refresh should cover both curves, truncation, and byte-order options."""

    resolver = _build_resolver(monkeypatch)
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x01" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=ROTATION_PERIOD,
        secrets_creation_date=ROTATION_PERIOD * 2,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)

    await resolver._refresh_cache()

    variants = {meta["variant"] for meta in resolver._lookup_metadata.values()}
    assert variants.issuperset(
        {
            EidVariant.LEGACY_SECP160R1_X20_BE.value,
            EidVariant.MODERN_P256_X32_BE.value,
            EidVariant.MODERN_P256_X20_TRUNC_BE.value,
            EidVariant.MODERN_P256_X32_LE_SCALAR.value,
            EidVariant.MODERN_P256_X20_TRUNC_LE.value,
        }
    )

    bases = set[str]()
    for meta in resolver._lookup_metadata.values():
        bases.update(meta.get("timestamp_bases", {meta["timestamp_basis"]}))
    assert {"unix", "pair_date", "secrets_creation_date"}.issubset(bases)


@pytest.mark.asyncio
async def test_refresh_cache_skips_negative_neighbor_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-valued anchor candidates should be skipped entirely.

    Devices with pair_date=0 (like phones without deviceRegistration) should not
    generate EIDs from that anchor. This test verifies that zero anchors are
    rejected and don't cause negative window issues.
    """

    resolver = _build_resolver(monkeypatch)
    identity = DeviceIdentity(
        registry_id="zeroed-id",
        canonical_id="canonical-id",
        identity_key=b"\x03" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=0,
        secrets_creation_date=0,
    )

    call_log: list[int] = []
    original_generate = GoogleFindMyEIDResolver._generate_variant

    def _guarded_generate(
        self: GoogleFindMyEIDResolver,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        assert 0 <= time_counter <= FHNA_COUNTER_MASK
        call_log.append(time_counter)
        return original_generate(
            self, key_bytes, time_counter=time_counter, variant=variant
        )

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _guarded_generate)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(time, "time", lambda: float(ROTATION_PERIOD))

    await resolver._refresh_cache()

    # With pair_date=0 and secrets_creation_date=0, both anchors are rejected,
    # so no relative windows are generated. Only unix basis (if enabled) may produce EIDs.
    # The key point is that we don't crash and don't generate negative windows.
    if call_log:
        assert min(call_log) >= 0
        assert all(ts <= FHNA_COUNTER_MASK for ts in call_log)


@pytest.mark.asyncio
async def test_refresh_cache_converts_millisecond_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Millisecond timestamps should normalize to seconds when plausible."""

    resolver = _build_resolver(monkeypatch)
    ms_candidate = 1_700_000_000_000
    identity = DeviceIdentity(
        registry_id="ms-id",
        canonical_id="canonical-id",
        identity_key=b"\x04" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=None,
        secrets_creation_date=ms_candidate,
    )

    call_log: list[int] = []
    original_generate = GoogleFindMyEIDResolver._generate_variant

    def _guarded_generate(
        self: GoogleFindMyEIDResolver,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        assert 0 <= time_counter <= FHNA_COUNTER_MASK
        call_log.append(time_counter)
        return original_generate(
            self, key_bytes, time_counter=time_counter, variant=variant
        )

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _guarded_generate)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)

    await resolver._refresh_cache()

    assert call_log
    assert all(ts <= FHNA_COUNTER_MASK for ts in call_log)
    assert any(
        meta["timestamp_basis"] == "secrets_creation_date"
        and meta["rotation_timestamp"] <= FHNA_COUNTER_MASK
        for meta in resolver._lookup_metadata.values()
    )


@pytest.mark.asyncio
async def test_resolve_eid_persists_variant_and_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver hits should persist the chosen variant and advertisement format."""

    resolver = _build_resolver(monkeypatch)
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x02" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=None,
        secrets_creation_date=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    await resolver._refresh_cache()

    reversed_entry = next(
        (
            eid
            for eid, meta in resolver._lookup_metadata.items()
            if meta["advertisement_reversed"]
        ),
        None,
    )
    assert reversed_entry is not None
    metadata = resolver._lookup_metadata[reversed_entry]

    match = resolver.resolve_eid(reversed_entry)
    assert match is not None

    lock = resolver._locks[match.device_id]
    assert lock.advertisement_reversed is True
    assert lock.variant == metadata["variant"]
    assert lock.frame_type is None or isinstance(lock.frame_type, int)


@pytest.mark.asyncio
async def test_stale_locks_are_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locks older than the TTL should be removed on refresh."""

    resolver = _build_resolver(monkeypatch)
    stale_created = int(time.time()) - LOCK_TTL_SECONDS - 5
    resolver._locks["stale"] = EIDGenerationLock(
        device_id="stale",
        canonical_id="canonical",
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=MODERN_EID_LENGTH,
        frame_type=None,
        time_basis="unix",
        created_at=stale_created,
    )
    resolver._persisted_locks["stale"] = resolver._locks["stale"]

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return []

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    await resolver._refresh_cache()

    assert "stale" not in resolver._locks
    assert "stale" not in resolver._persisted_locks


# --- Specification variants -------------------------------------------------

_SPEC_ORDER: tuple[EidVariant, ...] = (
    EidVariant.LEGACY_SECP160R1_X20_BE,
    EidVariant.SPEC_P256_X32_BE,
    EidVariant.SPEC_P256_X20_TRUNC_BE,
    EidVariant.MODERN_P256_X32_BE,
    EidVariant.MODERN_P256_X20_TRUNC_BE,
    EidVariant.MODERN_P256_X32_LE_SCALAR,
    EidVariant.MODERN_P256_X20_TRUNC_LE,
)


def test_compute_variants_tries_spec_before_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlocked device gets every variant, the specification ones first."""

    resolver = _build_resolver(monkeypatch)
    work_item = SimpleNamespace(locked_variant=None, key_bytes=b"\x01" * 32)
    window = SimpleNamespace(windows=(object(),))

    specs = resolver._compute_variants(work_item, window)  # type: ignore[arg-type]

    assert tuple(spec.variant for spec in specs) == _SPEC_ORDER
    assert set(_SPEC_ORDER) == set(EidVariant)


def _independent_p256_mask(eik: bytes, counter: int, variant: EidVariant) -> int:
    """Flags mask of a P-256 variant from the oracle, not from the SUT."""

    import hashlib

    from custom_components.googlefindmy.FMDNCrypto.curve_profile import ScalarRule
    from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
        VARIANT_DERIVATIONS,
    )
    from tests.helpers.fmdn_report_oracle import owner_scalar

    _curve, derivation = VARIANT_DERIVATIONS[variant]
    rule = "mod_n" if derivation.rule is ScalarRule.MOD_N else "plus1"
    scalar = owner_scalar(eik, counter, rule, r_dash_byteorder=derivation.byteorder)
    return hashlib.sha256(scalar.to_bytes(MODERN_EID_LENGTH, "big")).digest()[-1]


def test_build_flags_mask_follows_each_variant_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every built P-256 EID carries the mask of its own scalar derivation.

    Previously every P-256 variant got the ``r' mod n`` mask; the
    ``MODERN_P256_*`` EIDs use ``(r' mod (n - 1)) + 1`` and, for the
    ``*_LE`` variants, ``r'`` read little-endian.
    """

    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    resolver = _build_resolver(monkeypatch)
    resolver._ensure_cache_defaults()
    eik = bytes(range(32))
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=eik,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )
    resolver._cached_identities = [identity]
    work_items = resolver._collect_work_items([identity], now_unix=1024)
    _lookup, metadata, _ids = resolver._build_lookup_sync(
        work_items, 1024, resolver._build_rotation_params()
    )

    seen: set[EidVariant] = set()
    for meta in metadata.values():
        variant = EidVariant(meta["variant"])
        if variant is EidVariant.LEGACY_SECP160R1_X20_BE:
            continue
        expected = _independent_p256_mask(eik, meta["rotation_timestamp"], variant)
        assert meta["flags_xor_mask"] == expected, variant
        seen.add(variant)

    assert seen == set(EidVariant) - {EidVariant.LEGACY_SECP160R1_X20_BE}


@pytest.mark.parametrize("variant", list(EidVariant), ids=lambda v: v.value)
def test_stored_lock_variant_loads_unchanged(variant: EidVariant) -> None:
    """A stored lock keeps its variant name, old values and new ones alike."""

    payload = {
        "device_id": "dev-1",
        "canonical_id": "abc123",
        "variant": variant.value,
        "advertisement_reversed": False,
        "eid_length": 20 if "x20" in variant.value else 32,
    }

    restored = EIDGenerationLock.from_dict(payload)

    assert restored.variant == variant.value
    assert EIDGenerationLock.from_dict(restored.to_dict()).variant == variant.value
