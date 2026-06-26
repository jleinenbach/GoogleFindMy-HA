# tests/test_eid_resolver_executor_offload.py
"""Executor-offload gate for the resolver build phase (AP-C).

The synchronous EID build inside ``GoogleFindMyEIDResolver._refresh_cache`` is
offloaded to a worker thread via ``hass.async_add_executor_job`` so the event
loop stays reactive under Home Assistant's watchdog budget. The extracted pure
worker is ``GoogleFindMyEIDResolver._build_lookup_sync``.

This module pins four contracts:

* **Off-loop execution** -- the build runs on a worker thread whose identity
  differs from the loop thread (asserted via a real executor, not a mock).
* **Bit-identical output** -- the offloaded build produces a lookup/metadata
  table byte-for-byte equal to a direct reference build over the same inputs.
* **Deterministic memo-cache race safety** -- a bounded rendezvous instruments
  the AP-A memo cache's lock so two threads attempt to co-occupy its critical
  section; with the real lock the peer is parked on ``acquire()``, peak
  occupancy stays 1, and the cache raises nothing.
* **Mutation counter-probe** -- disabling the real lock lets both threads enter
  the critical section together (peak occupancy reaches 2, possibly with
  corruption), turning the same deterministic rendezvous red and proving the
  lock is load-bearing rather than incidental.

Byte-exact derivation per variant is pinned separately by
``tests/test_eid_resolver_characterization.py``; here the golden assertion is
relative (offloaded == reference) so it tracks the live build inputs.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import eid_resolver as resolver_mod
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    GoogleFindMyEIDResolver,
    _BoundedLRUCache,
)


def _identity(*, identity_key: bytes = b"\xaa" * 32) -> DeviceIdentity:
    """Return a minimal active identity that yields a non-empty build."""

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


def _build_refreshable_resolver(
    hass: SimpleNamespace,
) -> GoogleFindMyEIDResolver:
    """Construct a resolver wired enough to run ``_refresh_cache`` end to end."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
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


def _close_coro(coro: object, name: object = None) -> None:
    """Close a scheduled coroutine to avoid RuntimeWarning in tests."""

    if hasattr(coro, "close"):
        coro.close()


def _patch_collection(
    monkeypatch: pytest.MonkeyPatch, identities: list[DeviceIdentity]
) -> None:
    """Patch the device-secret collection seam and enable the unix basis."""

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


@pytest.mark.asyncio
async def test_build_runs_on_worker_thread_not_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build must execute off the event-loop thread (AP-C contract a).

    A real single-worker ``ThreadPoolExecutor`` backs
    ``hass.async_add_executor_job``; ``_build_lookup_sync`` records the thread
    identity it ran on, which must differ from the loop thread identity.
    """

    loop_thread_ident = threading.get_ident()
    seen_idents: list[int] = []

    real_build = GoogleFindMyEIDResolver._build_lookup_sync

    def _recording_build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen_idents.append(threading.get_ident())
        return real_build(self, *args, **kwargs)

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_build_lookup_sync", _recording_build)

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:

        async def _add_executor_job(func, *args):  # type: ignore[no-untyped-def]
            return await loop.run_in_executor(pool, func, *args)

        hass = SimpleNamespace(
            async_create_task=_close_coro,
            async_create_background_task=_close_coro,
            async_add_executor_job=_add_executor_job,
            data={},
        )
        resolver = _build_refreshable_resolver(hass)
        _patch_collection(monkeypatch, [_identity()])

        await resolver._refresh_cache()

    assert seen_idents, "the build was never invoked"
    assert all(ident != loop_thread_ident for ident in seen_idents)
    assert resolver._lookup, "the offloaded build produced no EIDs"


@pytest.mark.asyncio
async def test_offloaded_build_is_bit_identical_to_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offloaded build must be byte-identical to a direct build (contract b).

    The lookup and metadata tables produced through the executor path must equal
    a reference ``_build_lookup_sync`` run over the same work items, proving the
    offload is correctness-preserving (EIDs bit-identical).
    """

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=2) as pool:

        async def _add_executor_job(func, *args):  # type: ignore[no-untyped-def]
            return await loop.run_in_executor(pool, func, *args)

        hass = SimpleNamespace(
            async_create_task=_close_coro,
            async_create_background_task=_close_coro,
            async_add_executor_job=_add_executor_job,
            data={},
        )
        resolver = _build_refreshable_resolver(hass)
        _patch_collection(monkeypatch, [_identity()])

        # Freeze the clock so the reference build sees identical inputs.
        monkeypatch.setattr(resolver_mod.time, "time", lambda: 1024.0)

        await resolver._refresh_cache()

        offloaded_lookup = dict(resolver._lookup)
        offloaded_metadata = dict(resolver._lookup_metadata)

        # Reference build over the exact same inputs on a fresh resolver.
        reference = _build_refreshable_resolver(hass)
        now_unix = 1024
        identities = await reference._collect_device_secrets()
        reference._cached_identities = list(identities)
        work_items = reference._collect_work_items(identities, now_unix=now_unix)
        rotation_params = reference._build_rotation_params()
        ref_lookup, ref_metadata, _ids = reference._build_lookup_sync(
            work_items, now_unix, rotation_params
        )

    assert offloaded_lookup, "no EIDs were built"
    assert offloaded_lookup == ref_lookup
    assert offloaded_metadata == ref_metadata


@pytest.mark.asyncio
async def test_invalid_basis_hint_is_popped_on_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invalid-hint ``_known_timebases.pop`` must run loop-side (AP-C).

    ``_build_lookup_sync`` only *collects* the registry ids whose basis hint was
    invalid; the resolver mutates ``_known_timebases`` after the executor
    returns. Seeding an unrecognized basis hint forces ``invalid_hint`` true and
    pins that the stale entry is removed on the loop, not in the worker thread.
    """

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:

        async def _add_executor_job(func, *args):  # type: ignore[no-untyped-def]
            return await loop.run_in_executor(pool, func, *args)

        hass = SimpleNamespace(
            async_create_task=_close_coro,
            async_create_background_task=_close_coro,
            async_add_executor_job=_add_executor_job,
            data={},
        )
        resolver = _build_refreshable_resolver(hass)
        _patch_collection(monkeypatch, [_identity()])

        # Seed a basis hint that is not among the available bases: this makes
        # ``_compute_relative_windows`` report ``invalid_hint`` for the item.
        resolver._known_timebases["registry-id"] = "not-a-real-basis"

        await resolver._refresh_cache()

    # The stale hint was popped loop-side after the offloaded build returned.
    assert "registry-id" not in resolver._known_timebases


@pytest.mark.asyncio
async def test_build_tolerates_flags_mask_failure_in_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flags-mask failure inside the worker must not abort the build (AP-C).

    The offloaded build wraps ``_compute_flags_xor_mask`` in a defensive guard:
    a mask computation error skips the mask but still registers the EID. Forcing
    the mask to raise pins that guard and proves the worker-thread build is
    resilient (the EIDs are still produced without a mask).
    """

    def _boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("mask boom")

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_compute_flags_xor_mask", _boom)

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:

        async def _add_executor_job(func, *args):  # type: ignore[no-untyped-def]
            return await loop.run_in_executor(pool, func, *args)

        hass = SimpleNamespace(
            async_create_task=_close_coro,
            async_create_background_task=_close_coro,
            async_add_executor_job=_add_executor_job,
            data={},
        )
        resolver = _build_refreshable_resolver(hass)
        _patch_collection(monkeypatch, [_identity()])

        await resolver._refresh_cache()

    # EIDs were still produced, but no entry carries a flags XOR mask.
    assert resolver._lookup, "the build aborted on a mask failure"
    assert all(
        "flags_xor_mask" not in meta for meta in resolver._lookup_metadata.values()
    )


# ---------------------------------------------------------------------------
# Memo-cache race contract (c) and mutation counter-probe (d).
# ---------------------------------------------------------------------------

# Two contending threads suffice: the probe detects whether more than one
# thread is ever simultaneously inside the cache's critical section.
_RACE_THREADS = 2
_RACE_MAXSIZE = 4
_RACE_ROUNDS = 300


class _WitnessLock:
    """Lock wrapper that witnesses concurrent occupancy of the critical section.

    Wraps the cache's real ``threading.Lock``. On ``__enter__`` it bumps a
    shared occupancy counter, records the peak, then performs a bounded
    "I am inside" handshake: it signals its own presence and waits a short,
    timeout-bounded interval for the peer to also signal presence. This forces
    both threads to be *inside* the section at the same time when (and only
    when) mutual exclusion is absent.

    * ``exclusive=True`` keeps the real lock: the peer is parked on
      ``acquire()`` and can never enter, so its presence event never fires; the
      wait simply times out, peak occupancy stays 1, and the OrderedDict
      mutations are atomic.
    * ``exclusive=False`` drops the real lock: both threads enter and signal
      together, so peak occupancy reaches 2 -- a direct, deadlock-free witness
      that mutual exclusion is gone and the unguarded mutation runs concurrently.

    All waits are timeout-bounded, so this can never hang regardless of the
    test harness's event-loop or threading instrumentation.
    """

    _HANDSHAKE_TIMEOUT = 0.5

    def __init__(self, *, exclusive: bool, parties: int) -> None:
        self._exclusive = exclusive
        self._real = threading.Lock()
        self._occupancy = 0
        self._counter_guard = threading.Lock()
        self.peak_occupancy = 0
        # A single bounded rendezvous performed once per thread on its first
        # entry. Under mutual exclusion only one thread can ever be inside, so
        # the rendezvous can never complete and falls through on timeout
        # (peak occupancy 1). Without exclusion all parties reach it together
        # while each holds the section open, so peak occupancy equals ``parties``.
        self._rendezvous = threading.Barrier(parties, timeout=self._HANDSHAKE_TIMEOUT)
        self._handshaked: set[int] = set()

    def __enter__(self) -> _WitnessLock:
        if self._exclusive:
            self._real.acquire()
        ident = threading.get_ident()
        first_entry = False
        with self._counter_guard:
            self._occupancy += 1
            self.peak_occupancy = max(self.peak_occupancy, self._occupancy)
            if ident not in self._handshaked:
                self._handshaked.add(ident)
                first_entry = True
        if first_entry:
            # Bounded rendezvous while holding the section open. Times out
            # harmlessly under exclusion; completes (co-occupancy) without it.
            try:
                self._rendezvous.wait()
            except threading.BrokenBarrierError:
                pass
        return self

    def __exit__(self, *exc: object) -> bool:
        with self._counter_guard:
            self._occupancy -= 1
        if self._exclusive:
            self._real.release()
        return False


def _hammer_cache(cache: _BoundedLRUCache, rounds: int) -> None:
    """Drive put/get churn that forces eviction (``popitem``) every few calls."""

    for round_idx in range(rounds):
        for key in range(_RACE_MAXSIZE + 2):
            cache.put((round_idx % 3, key), key)
            cache.get((round_idx % 3, key))


def _run_race(cache: _BoundedLRUCache) -> list[BaseException]:
    """Run the two-thread interleaving and return any worker exceptions."""

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker() -> None:
        try:
            _hammer_cache(cache, _RACE_ROUNDS)
        except BaseException as exc:  # noqa: BLE001 - surface any race failure
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(_RACE_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    return errors


def test_memo_cache_race_is_safe_with_real_lock() -> None:
    """Mutual exclusion must hold the memo cache to one occupant (contract c).

    Two threads hammer interleaved put/get and the witness lock yields the GIL
    while each thread sits inside the critical section. With the real lock the
    peer is parked on ``acquire()``: peak occupancy stays 1, no worker raises,
    and the cache stays within its declared bound. The single-occupancy
    invariant is exactly what keeps the OrderedDict mutations atomic.
    """

    witness = _WitnessLock(exclusive=True, parties=_RACE_THREADS)
    cache = _BoundedLRUCache(_RACE_MAXSIZE)
    cache._lock = witness  # type: ignore[assignment]

    errors = _run_race(cache)

    assert not errors, f"locked cache raised under contention: {errors!r}"
    assert witness.peak_occupancy == 1, (
        f"real lock admitted {witness.peak_occupancy} concurrent occupants"
    )
    assert len(cache) <= _RACE_MAXSIZE


def test_memo_cache_race_is_red_without_lock() -> None:
    """Removing mutual exclusion must let two threads co-occupy (contract d).

    The same witnessed interleaving with the real lock disabled lets both
    threads enter the critical section together while one is preempted
    mid-mutation. Peak occupancy deterministically reaches 2 (and the unguarded
    OrderedDict mutation may additionally raise or overflow the bound). This
    counter-probe proves the AP-A lock is load-bearing: it fails closed if the
    unguarded run somehow never co-occupies.
    """

    witness = _WitnessLock(exclusive=False, parties=_RACE_THREADS)
    cache = _BoundedLRUCache(_RACE_MAXSIZE)
    # Drop the real lock: the witness records occupancy but enforces nothing.
    cache._lock = witness  # type: ignore[assignment]

    errors = _run_race(cache)

    overflowed = len(cache) > _RACE_MAXSIZE
    assert witness.peak_occupancy >= 2 or errors or overflowed, (
        "unguarded cache was never co-occupied and never corrupted; the lock "
        "probe is not exercising the critical section"
    )
    # The intended, deterministic signal is co-occupancy of the section.
    assert witness.peak_occupancy >= 2, (
        f"expected concurrent occupancy without the lock, saw peak "
        f"{witness.peak_occupancy}"
    )
