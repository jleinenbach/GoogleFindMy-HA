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
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    ROTATION_PERIOD,
    ROTATION_PERIOD_900,
    ROTATION_PERIOD_3600,
)


def test_rotation_time_counters_cover_all_three_periods() -> None:
    """The signature must carry one time_counter term per rotation period.

    This pins the *independent* presence of the 1024 s, 900 s, and 3600 s terms.
    The 3600 s term cannot be isolated by a clock rollover (every 3600 s
    boundary is also a 900 s boundary, since 3600 == 4 * 900), so its standalone
    contribution is asserted here directly rather than via the rebuild path.
    """

    now_unix = 1_234_567
    counters = GoogleFindMyEIDResolver._rotation_time_counters(now_unix)

    by_period = dict(counters)
    assert set(by_period) == {
        ROTATION_PERIOD,
        ROTATION_PERIOD_900,
        ROTATION_PERIOD_3600,
    }
    assert by_period[ROTATION_PERIOD] == now_unix // ROTATION_PERIOD
    assert by_period[ROTATION_PERIOD_900] == now_unix // ROTATION_PERIOD_900
    assert by_period[ROTATION_PERIOD_3600] == now_unix // ROTATION_PERIOD_3600


def test_build_signature_changes_when_only_3600_counter_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clocks with equal 1024 s/900 s counters but a different 3600 s
    counter would be physically impossible (3600 == 4 * 900), so we pin the
    3600 s term's effect on the signature by injecting the counter tuple
    directly: dropping the 3600 s term changes the digest, proving it is hashed.
    """

    resolver = _build_refreshable_resolver()
    work_items: list = []
    now_unix = 230_400  # all counters on a boundary

    full = resolver._build_signature(work_items, now_unix=now_unix)

    # Recompute with the 3600 s term suppressed to confirm it participates.
    # monkeypatch auto-restores the staticmethod wrapper after the test.
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_rotation_time_counters",
        staticmethod(
            lambda n: tuple((p, n // p) for p in (ROTATION_PERIOD, ROTATION_PERIOD_900))
        ),
    )
    without_3600 = resolver._build_signature(work_items, now_unix=now_unix)

    assert full != without_3600


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
    """Crossing any single rotation window changes the signature -> rebuild.

    The phone-window protection (F5): a one-period signature would miss the
    900 s / 3600 s rollovers. We pick a base time and a one-second advance that
    flips the ``//period`` counter under test, then assert the rebuild fires.

    Note on 3600 s: because ``ROTATION_PERIOD_3600 == 4 * ROTATION_PERIOD_900``,
    every 3600 s boundary is also a 900 s boundary, so a 3600 s rollover can
    never be isolated from the 900 s counter (a mathematical property of the
    periods, not a test defect). For 1024 s and 900 s we require exact
    isolation; for 3600 s we require that the target counter flips (the 900 s
    counter is allowed to flip with it). The independent contribution of the
    3600 s term to the signature is pinned by the mutation cross-check
    documented in the AP-B1 report.
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
