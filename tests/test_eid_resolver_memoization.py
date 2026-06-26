# tests/test_eid_resolver_memoization.py
"""Memoization gate for the resolver's per-variant crypto seams (AP-A).

These tests pin the behaviour of the two instance-bound LRU caches that wrap
``GoogleFindMyEIDResolver._generate_variant`` (EID derivation) and
``GoogleFindMyEIDResolver._compute_flags_xor_mask`` (flags XOR mask):

* a repeated call with an unchanged key recomputes the underlying crypto zero
  times (the caches absorb it), and
* each cache stays bounded at its declared ``maxsize``.

The byte-exact correctness of the cached EIDs is enforced separately by
``tests/test_eid_resolver_characterization.py``; here we only prove the
caching mechanics. A full ``_refresh_cache`` run requires a fully wired
coordinator/storage stack, so the call-count assertions exercise the seams
directly with a fixed key, which is the surface the build loop calls into.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import eid_resolver as resolver_mod
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    _EID_MASK_MEMO_MAXSIZE,
    _EID_MEMO_MAXSIZE,
    _HEURISTIC_MEMO_MAXSIZE,
    GoogleFindMyEIDResolver,
    LearnedHeuristicParams,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    P256_ORDER,
    EidVariant,
    HeuristicBasis,
    generate_heuristic_eid,
)

from .test_eid_generator_variants import SAMPLE_COUNTER, SAMPLE_EIK


def _close_coro(coro: object, name: object = None) -> None:
    """Close a coroutine to avoid RuntimeWarning in the test context."""

    if hasattr(coro, "close"):
        coro.close()


async def _run_in_executor(func: object, *args: object) -> object:
    """Synchronous stand-in for ``hass.async_add_executor_job`` (AP-C).

    Runs the offloaded build inline so the memoization assertions keep
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
    """Construct a resolver wired enough to run ``_refresh_cache`` end to end."""

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


def _build_resolver() -> GoogleFindMyEIDResolver:
    """Construct a resolver without running __init__.

    Mirrors the characterization test's construction: stubs bypass
    ``__init__`` and call ``_ensure_cache_defaults`` directly, which now also
    initializes the memoization caches.
    """

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._ensure_cache_defaults()
    return resolver


def test_ensure_cache_defaults_initializes_memo_caches() -> None:
    """The memo caches must exist after _ensure_cache_defaults runs."""

    resolver = _build_resolver()

    assert resolver._eid_memo is not None
    assert resolver._flags_mask_memo is not None
    assert len(resolver._eid_memo) == 0
    assert len(resolver._flags_mask_memo) == 0


def test_generate_variant_memoizes_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated _generate_variant call must not recompute the EID.

    Requirement 2 (for the derivation seam): with an unchanged key the
    underlying ``generate_eid_variant`` is called exactly once for the first
    miss and zero times on every subsequent hit.
    """

    resolver = _build_resolver()

    calls = {"count": 0}
    real_generate = resolver_mod.generate_eid_variant

    def _counting_generate(*args: object, **kwargs: object) -> bytes:
        calls["count"] += 1
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(resolver_mod, "generate_eid_variant", _counting_generate)

    first = resolver._generate_variant(
        SAMPLE_EIK,
        time_counter=SAMPLE_COUNTER,
        variant=EidVariant.MODERN_P256_X32_BE,
    )
    assert calls["count"] == 1

    for _ in range(5):
        again = resolver._generate_variant(
            SAMPLE_EIK,
            time_counter=SAMPLE_COUNTER,
            variant=EidVariant.MODERN_P256_X32_BE,
        )
        assert again == first

    # Zero further computations after the initial miss.
    assert calls["count"] == 1


def test_compute_flags_xor_mask_memoizes_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated _compute_flags_xor_mask call must not recompute the mask.

    Requirement 2 (for the mask seam): the underlying
    ``compute_flags_xor_mask`` runs once on the miss and zero times on hits.
    """

    resolver = _build_resolver()

    calls = {"count": 0}
    real_mask = resolver_mod.compute_flags_xor_mask

    def _counting_mask(*args: object, **kwargs: object) -> int:
        calls["count"] += 1
        return real_mask(*args, **kwargs)

    monkeypatch.setattr(resolver_mod, "compute_flags_xor_mask", _counting_mask)

    first = resolver._compute_flags_xor_mask(
        SAMPLE_EIK,
        SAMPLE_COUNTER,
        curve_byte_len=MODERN_EID_LENGTH,
        curve_order=P256_ORDER,
    )
    assert calls["count"] == 1

    for _ in range(5):
        again = resolver._compute_flags_xor_mask(
            SAMPLE_EIK,
            SAMPLE_COUNTER,
            curve_byte_len=MODERN_EID_LENGTH,
            curve_order=P256_ORDER,
        )
        assert again == first

    assert calls["count"] == 1


def test_both_seams_recompute_zero_times_on_second_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second pass over the same inputs hits both caches (Requirement 2).

    This emulates the back-to-back ``_refresh_cache`` runs the orchestrator
    cares about: warm both caches in pass one, then assert pass two over the
    identical (key, counter, variant) work set recomputes neither crypto.
    """

    resolver = _build_resolver()

    gen_calls = {"count": 0}
    mask_calls = {"count": 0}
    real_generate = resolver_mod.generate_eid_variant
    real_mask = resolver_mod.compute_flags_xor_mask

    def _counting_generate(*args: object, **kwargs: object) -> bytes:
        gen_calls["count"] += 1
        return real_generate(*args, **kwargs)

    def _counting_mask(*args: object, **kwargs: object) -> int:
        mask_calls["count"] += 1
        return real_mask(*args, **kwargs)

    monkeypatch.setattr(resolver_mod, "generate_eid_variant", _counting_generate)
    monkeypatch.setattr(resolver_mod, "compute_flags_xor_mask", _counting_mask)

    variants = list(EidVariant)

    def _pass() -> None:
        for variant in variants:
            resolver._generate_variant(
                SAMPLE_EIK,
                time_counter=SAMPLE_COUNTER,
                variant=variant,
            )
            resolver._compute_flags_xor_mask(
                SAMPLE_EIK,
                SAMPLE_COUNTER,
                curve_byte_len=MODERN_EID_LENGTH,
                curve_order=P256_ORDER,
            )

    # Pass one warms the caches.
    _pass()
    gen_after_warm = gen_calls["count"]
    mask_after_warm = mask_calls["count"]
    assert gen_after_warm > 0
    assert mask_after_warm > 0

    # Pass two over identical inputs must add zero computations to both.
    _pass()
    assert gen_calls["count"] == gen_after_warm
    assert mask_calls["count"] == mask_after_warm


@pytest.mark.asyncio
async def test_second_refresh_cache_recomputes_no_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second _refresh_cache over an unchanged input set recomputes nothing.

    Requirement 2 in its strongest form: run the full build loop twice with an
    identical identity set and frozen clock, and assert that the second pass
    invokes neither ``generate_eid_variant`` nor ``compute_flags_xor_mask``.
    Both caches, warmed by pass one, absorb the entire second pass. This also
    exercises the memoized call site inside ``_refresh_cache``.
    """

    resolver = _build_refreshable_resolver()

    gen_calls = {"count": 0}
    mask_calls = {"count": 0}
    real_generate = resolver_mod.generate_eid_variant
    real_mask = resolver_mod.compute_flags_xor_mask

    def _counting_generate(*args: object, **kwargs: object) -> bytes:
        gen_calls["count"] += 1
        return real_generate(*args, **kwargs)

    def _counting_mask(*args: object, **kwargs: object) -> int:
        mask_calls["count"] += 1
        return real_mask(*args, **kwargs)

    monkeypatch.setattr(resolver_mod, "generate_eid_variant", _counting_generate)
    monkeypatch.setattr(resolver_mod, "compute_flags_xor_mask", _counting_mask)

    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\xaa" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    # Bound the deep-scan window fan-out so the unique-key count for this single
    # synthetic device stays well under _EID_MEMO_MAXSIZE. Without this the
    # absolute-basis deep scan emits > maxsize unique EID keys for one device,
    # which (correctly) evicts early entries and would make pass two re-derive
    # the evicted keys -- a property of the deliberately oversized deep-scan
    # test path, not of the memoization itself.
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.MIN_UNIX_WINDOW_SIZE",
        8,
    )
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: 1024.0)

    # Pass one warms both caches and must do real work.
    await resolver._refresh_cache()
    gen_after_warm = gen_calls["count"]
    mask_after_warm = mask_calls["count"]
    assert gen_after_warm > 0
    assert mask_after_warm > 0
    assert resolver._lookup  # sanity: the build loop produced EIDs

    # Pass two over the identical input set must recompute zero crypto.
    await resolver._refresh_cache()
    assert gen_calls["count"] == gen_after_warm
    assert mask_calls["count"] == mask_after_warm


def test_eid_memo_is_bounded_at_maxsize() -> None:
    """Inserting more than maxsize EID keys keeps the cache at maxsize.

    Requirement 3 (derivation cache): varying ``time_counter`` produces unique
    keys; size never exceeds ``_EID_MEMO_MAXSIZE``.
    """

    resolver = _build_resolver()

    overflow = _EID_MEMO_MAXSIZE + 50
    for counter in range(overflow):
        resolver._generate_variant(
            SAMPLE_EIK,
            time_counter=SAMPLE_COUNTER + counter,
            variant=EidVariant.MODERN_P256_X32_BE,
        )

    assert len(resolver._eid_memo) == _EID_MEMO_MAXSIZE


def test_flags_mask_memo_is_bounded_at_maxsize() -> None:
    """Inserting more than maxsize mask keys keeps the cache at maxsize.

    Requirement 3 (mask cache): varying ``time_counter`` produces unique keys;
    size never exceeds ``_EID_MASK_MEMO_MAXSIZE``.
    """

    resolver = _build_resolver()

    overflow = _EID_MASK_MEMO_MAXSIZE + 50
    for counter in range(overflow):
        resolver._compute_flags_xor_mask(
            SAMPLE_EIK,
            SAMPLE_COUNTER + counter,
            curve_byte_len=LEGACY_EID_LENGTH,
            curve_order=None,
        )

    assert len(resolver._flags_mask_memo) == _EID_MASK_MEMO_MAXSIZE


# ---------------------------------------------------------------------------
# Heuristic phone-discovery memoization (AP-SWEEP)
# ---------------------------------------------------------------------------
# ``_generate_heuristic_eid`` wraps ``generate_heuristic_eid`` on the resolve
# hot path (per cache-miss FMDN advertisement, on the event loop). It is the
# on-loop sibling of the build-side AP-A caches and is keyed on the rotation-
# aligned time window plus the hypothesis parameters.

_HEURISTIC_PERIOD = 900
_HEURISTIC_VARIANT = EidVariant.MODERN_P256_X32_BE


def test_generate_heuristic_eid_memoizes_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated calls inside one rotation window recompute zero times.

    Two ``now_unix`` values that fall in the same ABSOLUTE rotation window must
    collapse to a single cache key, so the underlying ``generate_heuristic_eid``
    runs once on the miss and never again on the in-window hits.
    """

    resolver = _build_resolver()

    calls = {"count": 0}
    real = resolver_mod.generate_heuristic_eid

    def _counting(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(resolver_mod, "generate_heuristic_eid", _counting)

    # Same window: floor(t / 900) is identical for these timestamps.
    base = 900 * 1000
    first = resolver._generate_heuristic_eid(
        SAMPLE_EIK,
        base,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=_HEURISTIC_VARIANT,
        anchor=None,
    )
    assert calls["count"] == 1

    for delta in (1, 100, 899):
        again = resolver._generate_heuristic_eid(
            SAMPLE_EIK,
            base + delta,
            rotation_period=_HEURISTIC_PERIOD,
            basis=HeuristicBasis.ABSOLUTE,
            variant=_HEURISTIC_VARIANT,
            anchor=None,
        )
        assert again == first

    # Zero further computations: every in-window call hit the cache.
    assert calls["count"] == 1


def test_generate_heuristic_eid_recomputes_on_new_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing a rotation-window boundary forces a fresh (correct) compute.

    A timestamp in the next window must miss the cache and recompute, and the
    new result must be byte-identical to a direct ``generate_heuristic_eid``
    call -- proving the cache never serves a stale list across windows.
    """

    resolver = _build_resolver()

    calls = {"count": 0}
    real = resolver_mod.generate_heuristic_eid

    def _counting(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(resolver_mod, "generate_heuristic_eid", _counting)

    base = 900 * 1000
    resolver._generate_heuristic_eid(
        SAMPLE_EIK,
        base,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=_HEURISTIC_VARIANT,
        anchor=None,
    )
    assert calls["count"] == 1

    next_window = base + _HEURISTIC_PERIOD
    cached_result = resolver._generate_heuristic_eid(
        SAMPLE_EIK,
        next_window,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=_HEURISTIC_VARIANT,
        anchor=None,
    )
    # The new window is a miss: the crypto ran a second time.
    assert calls["count"] == 2

    # Byte-exact equivalence to the unmemoized reference for the same window.
    reference = generate_heuristic_eid(
        SAMPLE_EIK,
        next_window,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=_HEURISTIC_VARIANT,
        anchor=None,
    )
    assert [r.eid_bytes for r in cached_result] == [r.eid_bytes for r in reference]
    assert cached_result == reference


def test_generate_heuristic_eid_matches_unmemoized_reference() -> None:
    """The wrapper is byte-exact with the raw function across hypotheses.

    GOLDEN-style equivalence over the full ABSOLUTE/RELATIVE x variant matrix:
    the memoized wrapper must never alter the derived EID bytes.
    """

    resolver = _build_resolver()
    now_unix = 900 * 1234 + 17
    anchor = 900 * 1000

    cases = [
        (HeuristicBasis.ABSOLUTE, None),
        (HeuristicBasis.RELATIVE, anchor),
    ]
    for basis, anchor_value in cases:
        for variant in EidVariant:
            wrapped = resolver._generate_heuristic_eid(
                SAMPLE_EIK,
                now_unix,
                rotation_period=_HEURISTIC_PERIOD,
                basis=basis,
                variant=variant,
                anchor=anchor_value,
            )
            reference = generate_heuristic_eid(
                SAMPLE_EIK,
                now_unix,
                rotation_period=_HEURISTIC_PERIOD,
                basis=basis,
                variant=variant,
                anchor=anchor_value,
            )
            assert wrapped == reference


def test_heuristic_memo_is_bounded_at_maxsize() -> None:
    """Inserting more than maxsize heuristic keys keeps the cache at maxsize.

    Varying the rotation window produces unique keys; the bounded LRU never
    grows past ``_HEURISTIC_MEMO_MAXSIZE``.
    """

    resolver = _build_resolver()

    overflow = _HEURISTIC_MEMO_MAXSIZE + 50
    for window in range(overflow):
        resolver._generate_heuristic_eid(
            SAMPLE_EIK,
            window * _HEURISTIC_PERIOD,
            rotation_period=_HEURISTIC_PERIOD,
            basis=HeuristicBasis.ABSOLUTE,
            variant=_HEURISTIC_VARIANT,
            anchor=None,
        )

    assert len(resolver._heuristic_memo) == _HEURISTIC_MEMO_MAXSIZE


def _heuristic_identity() -> DeviceIdentity:
    """Return a minimal identity whose key derives heuristic EIDs."""

    return DeviceIdentity(
        registry_id="phone-registry-id",
        canonical_id="phone-canonical-id",
        identity_key=SAMPLE_EIK,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )


def test_heuristic_check_learned_routes_through_memo() -> None:
    """The learned fast path resolves a real EID via the memoized wrapper.

    Drives ``_heuristic_check_learned`` (the call site redirected to
    ``self._generate_heuristic_eid``) end to end: a candidate set seeded with a
    genuine ABSOLUTE-basis EID must yield a HEURISTIC match, and the memo cache
    must be populated by the resolve.
    """

    resolver = _build_resolver()
    identity = _heuristic_identity()
    resolver._cached_identities = [identity]

    now_unix = 900 * 2000 + 5
    reference = generate_heuristic_eid(
        SAMPLE_EIK,
        now_unix,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=_HEURISTIC_VARIANT,
        anchor=None,
    )
    candidate_set = {reference[0].eid_bytes}

    learned = LearnedHeuristicParams(
        device_id=identity.registry_id,
        canonical_id=identity.canonical_id,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=_HEURISTIC_VARIANT,
        discovered_at=now_unix,
        last_confirmed_at=now_unix,
    )

    match = resolver._heuristic_check_learned(
        identity.registry_id,
        learned,
        candidate_set,
        now_unix=now_unix,
    )

    assert match is not None
    assert match.device_id == identity.registry_id
    assert len(resolver._heuristic_memo) == 1


def test_heuristic_test_hypotheses_routes_through_memo() -> None:
    """The slow discovery path resolves a real EID via the memoized wrapper.

    Drives ``_heuristic_test_hypotheses`` (the second redirected call site) end
    to end: a candidate seeded with a genuine EID for one of the tested
    hypotheses yields a match and records the learned parameters.
    """

    resolver = _build_resolver()
    identity = _heuristic_identity()
    resolver._cached_identities = [identity]

    now_unix = 900 * 3000 + 11
    reference = generate_heuristic_eid(
        SAMPLE_EIK,
        now_unix,
        rotation_period=_HEURISTIC_PERIOD,
        basis=HeuristicBasis.ABSOLUTE,
        variant=EidVariant.MODERN_P256_X20_TRUNC_BE,
        anchor=None,
    )
    candidate_set = {reference[0].eid_bytes}

    match = resolver._heuristic_test_hypotheses(
        identity,
        candidate_set,
        now_unix=now_unix,
    )

    assert match is not None
    assert match.device_id == identity.registry_id
    # The discovery path caches the learned parameters for the fast path.
    assert identity.registry_id in resolver._learned_heuristic_params
    assert len(resolver._heuristic_memo) >= 1
