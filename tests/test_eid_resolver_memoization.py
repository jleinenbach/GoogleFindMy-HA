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
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    P256_ORDER,
    EidVariant,
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

    resolver._store = SimpleNamespace(
        async_load=lambda: None, async_save=_async_noop
    )
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
    monkeypatch.setattr(
        GoogleFindMyEIDResolver, "_collect_device_secrets", _collect
    )
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
