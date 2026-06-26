# tests/test_eid_resolver_skip_guard.py
"""Skip-guard gate for the resolver's expensive build phase (AP-B1).

The build phase of ``GoogleFindMyEIDResolver._refresh_cache`` (constructing a
``CacheBuilder``, fanning out work items into windows/variants, and calling
``builder.finalize()``) is pure with respect to a deterministic signature over
the active devices. AP-B1 adds a skip guard that recomputes that signature on
every refresh and bypasses the build phase when it is unchanged, while keeping
the cheap mandatory side effects (lock purging, identity caching,
secret collection) running on every trigger.

These tests pin three contracts:

* **Skip on unchanged input** -- a second ``_refresh_cache`` over an identical
  device set and frozen clock does not call ``builder.finalize`` again, leaves
  ``self._lookup`` content-identical, yet still runs ``_purge_stale_locks``.
* **Work-item sensitivity** -- mutating any signature-relevant work-item field
  (here ``key_bytes``) forces a rebuild (no skip).
* **Rotation-window sensitivity** -- advancing the clock across *any* of the
  three rotation periods (1024 s, 900 s, 3600 s) changes the signature and
  forces a rebuild. This is the phone-window protection (F5): a single 1024 s
  term would miss the 900 s / 3600 s phone-window rollovers and leave those
  trackers unresolvable.

The byte-exact correctness of the cached EIDs is enforced separately by
``tests/test_eid_resolver_characterization.py``; here we only prove the
skip/rebuild decision logic.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import eid_resolver as resolver_mod
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    GoogleFindMyEIDResolver,
    WindowCandidate,
    WindowSpec,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    ROTATION_PERIOD,
    ROTATION_PERIOD_900,
    ROTATION_PERIOD_3600,
)


def test_signature_folds_realized_window_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signature replays ``_compute_time_windows`` and folds its content.

    Variant B (correctness by construction): the skipped crypto consumes only
    the realized window/variant specs, so the signature must change iff that
    spec set changes. Here we hold the work item and clock fixed but inject one
    extra realized window via ``_compute_time_windows``; the digest must move,
    proving the windows -- not a ``now_unix // period`` formula -- drive the key.
    """

    resolver = _build_refreshable_resolver()
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    params = resolver._build_rotation_params()
    items = resolver._collect_work_items([_identity()], now_unix=1024)

    baseline = resolver._build_signature(items, now_unix=1024, params=params)

    real_windows = GoogleFindMyEIDResolver._compute_time_windows

    def _extra_window(self: GoogleFindMyEIDResolver, work_item, *, now_unix, params):  # type: ignore[no-untyped-def]
        windows, invalid_hint = real_windows(
            self, work_item, now_unix=now_unix, params=params
        )
        extra = WindowSpec(
            time_basis="synthetic",
            candidate_value=now_unix,
            windows=(
                WindowCandidate(
                    timestamp=now_unix + params.rotation_period,
                    semantic_offset=params.rotation_period,
                    time_basis="synthetic",
                    candidate_value=now_unix,
                ),
            ),
        )
        return [*windows, extra], invalid_hint

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_compute_time_windows", _extra_window)
    with_extra = resolver._build_signature(items, now_unix=1024, params=params)

    assert baseline != with_extra


def _close_coro(coro: object, name: object = None) -> None:
    """Close a coroutine to avoid RuntimeWarning in the test context."""

    if hasattr(coro, "close"):
        coro.close()


async def _run_in_executor(func: object, *args: object) -> object:
    """Synchronous stand-in for ``hass.async_add_executor_job`` (AP-C).

    Runs the offloaded build inline so the skip/rebuild assertions keep
    exercising the same build surface; the dedicated executor-offload suite
    pins the genuine worker-thread behavior.
    """

    return func(*args)  # type: ignore[operator]


def _fake_hass() -> SimpleNamespace:
    """Return a lightweight hass stand-in that closes scheduled coroutines."""

    return SimpleNamespace(
        async_create_task=_close_coro,
        async_create_background_task=_close_coro,
        async_add_executor_job=_run_in_executor,
        data={},
    )


def _build_refreshable_resolver() -> GoogleFindMyEIDResolver:
    """Construct a resolver wired enough to run ``_refresh_cache`` end to end.

    Mirrors the helper in ``tests/test_eid_resolver_memoization.py`` so both
    performance levers exercise the same build-loop surface.
    """

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._ensure_cache_defaults()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}

    async def _async_noop(payload: object = None) -> None:
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    return resolver


def _identity(*, identity_key: bytes = b"\xaa" * 32) -> DeviceIdentity:
    """Return a minimal active identity for build-loop coverage."""

    return DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=identity_key,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )


def _install_common_patches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    identities: list[DeviceIdentity],
) -> dict[str, int]:
    """Patch collection seams and a finalize counter; return the counter dict."""

    finalize_calls = {"count": 0}
    real_finalize = resolver_mod.CacheBuilder.finalize

    def _counting_finalize(self: resolver_mod.CacheBuilder):  # type: ignore[no-untyped-def]
        finalize_calls["count"] += 1
        return real_finalize(self)

    monkeypatch.setattr(resolver_mod.CacheBuilder, "finalize", _counting_finalize)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return list(identities)

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    return finalize_calls


@pytest.mark.asyncio
async def test_second_refresh_skips_build_but_still_purges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged input set: second refresh skips finalize, still purges locks."""

    resolver = _build_refreshable_resolver()
    finalize_calls = _install_common_patches(monkeypatch, identities=[_identity()])

    purge_calls = {"count": 0}
    real_purge = GoogleFindMyEIDResolver._purge_stale_locks

    def _counting_purge(self: GoogleFindMyEIDResolver, *, now: int) -> None:
        purge_calls["count"] += 1
        real_purge(self, now=now)

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_purge_stale_locks", _counting_purge)
    monkeypatch.setattr(time, "time", lambda: 1024.0)

    # Pass one must do the real build.
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1
    assert resolver._lookup  # sanity: produced EIDs
    lookup_after_first = dict(resolver._lookup)

    # Pass two over identical input and frozen clock skips the build phase ...
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1  # no second finalize
    # ... but keeps the lookup content-identical ...
    assert resolver._lookup == lookup_after_first
    # ... and still runs the mandatory side effect on every trigger.
    assert purge_calls["count"] == 2


@pytest.mark.asyncio
async def test_work_item_field_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing a signature-relevant work-item field (key_bytes) forces rebuild."""

    resolver = _build_refreshable_resolver()
    # Two-element holder so the second collect returns a different key.
    state = {"identities": [_identity(identity_key=b"\xaa" * 32)]}
    finalize_calls = {"count": 0}
    real_finalize = resolver_mod.CacheBuilder.finalize

    def _counting_finalize(self: resolver_mod.CacheBuilder):  # type: ignore[no-untyped-def]
        finalize_calls["count"] += 1
        return real_finalize(self)

    monkeypatch.setattr(resolver_mod.CacheBuilder, "finalize", _counting_finalize)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return list(state["identities"])

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    monkeypatch.setattr(time, "time", lambda: 1024.0)

    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1

    # Mutate the key material -> different work-item signature -> rebuild.
    state["identities"] = [_identity(identity_key=b"\xbb" * 32)]
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 2


@pytest.mark.asyncio
async def test_anchor_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing the window anchor (pair_date) forces a rebuild.

    ``pair_date`` is not one of the plan's five core work-item fields, but it
    drives the relative-window anchor in ``_compute_relative_windows`` and thus
    the build output. The signature folds it in so the skip stays
    correctness-preserving (no stale lookup table). This pins that extension.
    """

    resolver = _build_refreshable_resolver()
    state = {"identities": [_identity()]}
    finalize_calls = {"count": 0}
    real_finalize = resolver_mod.CacheBuilder.finalize

    def _counting_finalize(self: resolver_mod.CacheBuilder):  # type: ignore[no-untyped-def]
        finalize_calls["count"] += 1
        return real_finalize(self)

    monkeypatch.setattr(resolver_mod.CacheBuilder, "finalize", _counting_finalize)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return list(state["identities"])

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    monkeypatch.setattr(time, "time", lambda: 1024.0)

    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1

    # Re-provision with a different pairing anchor -> rebuild required.
    state["identities"] = [
        DeviceIdentity(
            registry_id="registry-id",
            canonical_id="canonical-id",
            identity_key=b"\xaa" * 32,
            encrypted_identity_key=None,
            owner_key_version=None,
            device_type=None,
            config_entry_id="entry-id",
            fast_pair_model_id=None,
            pair_date=999,
        )
    ]
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 2


@pytest.mark.parametrize(
    ("period", "label"),
    [
        (ROTATION_PERIOD, "1024s"),
        (ROTATION_PERIOD_900, "900s"),
        (ROTATION_PERIOD_3600, "3600s"),
    ],
)
@pytest.mark.asyncio
async def test_rotation_window_rollover_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    period: int,
    label: str,
) -> None:
    """Advancing the clock across a rotation boundary forces a rebuild.

    Under Variant B the signature folds the realized window timestamps, which
    derive from ``now_unix`` (the unix basis is ``now_unix`` itself, with a
    drift/tz-padded window), so advancing the clock shifts the window set and
    forces a rebuild. We pick a base time and a one-second advance that flips
    the ``//period`` counter under test, then assert the rebuild fires; this
    keeps the original phone-window intent (no stale lookup across a rollover)
    while exercising the new realized-window signature.

    Note on 3600 s: because ``ROTATION_PERIOD_3600 == 4 * ROTATION_PERIOD_900``,
    every 3600 s boundary is also a 900 s boundary, so a 3600 s rollover can
    never be isolated from the 900 s counter (a mathematical property of the
    periods, not a test defect). For 1024 s and 900 s we require exact
    isolation; for 3600 s we require that the target counter flips (the 900 s
    counter is allowed to flip with it).
    """

    resolver = _build_refreshable_resolver()
    finalize_calls = _install_common_patches(monkeypatch, identities=[_identity()])

    all_periods = (ROTATION_PERIOD, ROTATION_PERIOD_900, ROTATION_PERIOD_3600)
    require_isolation = period != ROTATION_PERIOD_3600

    def _acceptable(base: int, advanced: int) -> bool:
        flipped = [p for p in all_periods if base // p != advanced // p]
        if require_isolation:
            return flipped == [period]
        return period in flipped

    # Search for a base time whose next ``period`` boundary satisfies the
    # isolation requirement above. Start from a fixed anchor for determinism.
    anchor = 1_000_000
    base = 0
    advanced = 0
    for candidate in range(anchor, anchor + 8 * max(all_periods)):
        nxt = candidate + 1
        if candidate // period != nxt // period and _acceptable(candidate, nxt):
            base, advanced = candidate, nxt
            break
    assert advanced, f"no qualifying base found for {label}"

    monkeypatch.setattr(time, "time", lambda: float(base))
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1

    monkeypatch.setattr(time, "time", lambda: float(advanced))
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 2, (
        f"rotation window {label} rollover did not force a rebuild"
    )


def _lock_identity() -> DeviceIdentity:
    """Return an identity that resolves through the lock-tracking window path."""

    return DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\xaa" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )


def _install_lock(resolver: GoogleFindMyEIDResolver, *, created_at: int) -> None:
    """Register a non-legacy lock so ``_compute_lock_windows`` produces windows.

    ``rotation_timestamp=0`` is a valid (non-``None``) counter, so the lock is
    kept rather than discarded as legacy; ``created_at`` is phase-offset from the
    rotation period so the lock-tracking index ``(now - created_at) // period``
    rolls at a different ``now`` than the absolute ``now // period`` counter.
    """

    resolver._locks["registry-id"] = resolver_mod.EIDGenerationLock(
        device_id="registry-id",
        canonical_id="canonical-id",
        variant=resolver_mod.EidVariant.LEGACY_SECP160R1_X20_BE.value,
        advertisement_reversed=False,
        eid_length=20,
        rotation_timestamp=0,
        frame_type=None,
        time_basis="lock_tracking",
        created_at=created_at,
    )
    # Keep the lock confirmed at both clock points so ``_purge_stale_locks`` does
    # not drop it between the two refreshes.
    resolver._last_lock_confirmation["registry-id"] = created_at


@pytest.mark.asyncio
async def test_phase_offset_lock_window_drift_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a phase-offset lock must rebuild when a drift window appears.

    Repro of the AP-B1 skip-guard correctness defect. With ``created_at=500`` and
    period 1024, the lock-tracking window set is ``{0, 1024, 2048}`` at
    ``now=1100`` but ``{0, 1024, 2048, 3072}`` at ``now=1524`` -- a genuine extra
    drift window. Both clocks share ``now // 1024 == 1``, so the old
    absolute-counter signature was identical and wrongly skipped the rebuild,
    serving a stale lookup missing the 3072 window. The Variant B signature folds
    the realized lock windows, so the spec change forces a rebuild here.
    """

    resolver = _build_refreshable_resolver()
    _install_lock(resolver, created_at=500)
    finalize_calls = _install_common_patches(monkeypatch, identities=[_lock_identity()])
    # Re-arm the confirmation timestamp on every purge so the lock survives.
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_purge_stale_locks",
        lambda self, *, now: None,
    )

    monkeypatch.setattr(time, "time", lambda: 1100.0)
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1

    monkeypatch.setattr(time, "time", lambda: 1524.0)
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 2, (
        "phase-offset lock drift window did not force a rebuild"
    )


@pytest.mark.asyncio
async def test_phase_offset_relative_window_drift_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a phase-offset pairing anchor must rebuild on window drift.

    Repro of the AP-B1 skip-guard correctness defect on the relative path. With
    ``pair_date=500`` the relative window set grows from ``...6144`` at
    ``now=1100`` to ``...7168`` at ``now=1524``. Both clocks share
    ``now // 1024 == 1``, so the old absolute-counter signature was identical and
    wrongly skipped the rebuild. The Variant B signature folds the realized
    relative windows, so the extra window forces a rebuild.
    """

    resolver = _build_refreshable_resolver()
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\xaa" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=500,
    )
    finalize_calls = _install_common_patches(monkeypatch, identities=[identity])

    monkeypatch.setattr(time, "time", lambda: 1100.0)
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 1

    monkeypatch.setattr(time, "time", lambda: 1524.0)
    await resolver._refresh_cache()
    assert finalize_calls["count"] == 2, (
        "phase-offset relative drift window did not force a rebuild"
    )


@pytest.mark.asyncio
async def test_mutation_old_absolute_counter_signature_misses_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation guard: the pre-fix absolute-counter signature would not rebuild.

    Pins that the new regression tests above genuinely catch the defect rather
    than passing trivially. We rebuild the *old* signature logic (the per-period
    ``now // period`` counters plus the static work-item fields, with no realized
    windows) and assert it is byte-identical across ``now=1100`` and
    ``now=1524`` for the phase-offset lock case -- i.e. the old guard would have
    skipped the rebuild and served the stale lookup.
    """

    resolver = _build_refreshable_resolver()
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    _install_lock(resolver, created_at=500)

    def _legacy_signature(work_items: list, *, now_unix: int) -> str:
        import hashlib

        hasher = hashlib.blake2b(digest_size=32)
        for period in sorted(
            (ROTATION_PERIOD, ROTATION_PERIOD_900, ROTATION_PERIOD_3600)
        ):
            hasher.update(b"P")
            hasher.update(f"{period}:{now_unix // period}".encode())
        for item in sorted(
            (i.registry_id, i.key_bytes.hex(), str(i.rotation_ts)) for i in work_items
        ):
            hasher.update(b"W")
            for field_value in item:
                hasher.update(field_value.encode())
                hasher.update(b"\x00")
        return hasher.hexdigest()

    items_1100 = resolver._collect_work_items([_lock_identity()], now_unix=1100)
    items_1524 = resolver._collect_work_items([_lock_identity()], now_unix=1524)

    legacy_1100 = _legacy_signature(items_1100, now_unix=1100)
    legacy_1524 = _legacy_signature(items_1524, now_unix=1524)
    # The old logic could not tell the two clocks apart: it would have skipped.
    assert legacy_1100 == legacy_1524

    params = resolver._build_rotation_params()
    fixed_1100 = resolver._build_signature(items_1100, now_unix=1100, params=params)
    fixed_1524 = resolver._build_signature(items_1524, now_unix=1524, params=params)
    # The Variant B signature does tell them apart: it rebuilds.
    assert fixed_1100 != fixed_1524
