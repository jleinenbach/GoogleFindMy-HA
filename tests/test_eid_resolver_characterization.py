# tests/test_eid_resolver_characterization.py
"""Golden-gate characterization for the resolver EID derivation seam.

This module pins the byte-exact EID output of the resolver seam
``GoogleFindMyEIDResolver._generate_variant`` so that subsequent
performance levers (memoization, skip-guards, work offloading) can prove
they keep the derived EIDs bit-identical. The seam is the surface those
levers will wrap, so we characterize it directly rather than only the
underlying generator module. Each variant is anchored to a vector from
``GOLDEN_VECTORS`` in ``tests/test_eid_generator_variants.py``.
"""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    ROTATION_PERIOD,
    EidVariant,
    generate_eid_variant,
)

from .test_eid_generator_variants import (
    GOLDEN_VECTORS,
    SAMPLE_COUNTER,
    SAMPLE_EIK,
)


def _build_resolver() -> GoogleFindMyEIDResolver:
    """Construct a resolver without running __init__.

    Production stubs and tests bypass ``__init__`` (which schedules timers
    and storage I/O) and call ``_ensure_cache_defaults`` directly to
    initialize the optional caches. ``_generate_variant`` is a pure wrapper
    around ``generate_eid_variant`` and touches no instance state, so this
    minimal construction is sufficient for the characterization pins.
    """

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._ensure_cache_defaults()
    return resolver


def test_seam_pins_golden_vectors_for_all_variants() -> None:
    """The resolver seam must emit the golden hex EID for every variant.

    GOLDEN_VECTORS covers all five EidVariant members; iterating it proves
    the seam reproduces the canonical output for each one.
    """

    resolver = _build_resolver()

    assert set(GOLDEN_VECTORS) == set(EidVariant)

    for variant, expected_hex in GOLDEN_VECTORS.items():
        eid = resolver._generate_variant(
            SAMPLE_EIK,
            time_counter=SAMPLE_COUNTER,
            variant=variant,
        )
        assert eid.hex() == expected_hex


def test_seam_matches_generator_module_per_variant() -> None:
    """The seam must be bit-identical to generate_eid_variant per variant.

    The seam delegates with ``strict=False``; for these in-range fixed
    inputs the lenient path yields the same bytes as the default
    ``generate_eid_variant`` call, which is what the golden vectors assert.
    """

    resolver = _build_resolver()

    for variant in EidVariant:
        seam_eid = resolver._generate_variant(
            SAMPLE_EIK,
            time_counter=SAMPLE_COUNTER,
            variant=variant,
        )
        module_eid = generate_eid_variant(SAMPLE_EIK, SAMPLE_COUNTER, variant)
        assert seam_eid == module_eid


def test_seam_is_deterministic_for_repeated_inputs() -> None:
    """The same (key_bytes, time_counter, variant) twice yields equal bytes.

    Determinism is the precondition for memoization: a follow-up lever may
    only cache the seam output if identical inputs always map to identical
    bytes.
    """

    resolver = _build_resolver()

    for variant in EidVariant:
        first = resolver._generate_variant(
            SAMPLE_EIK,
            time_counter=SAMPLE_COUNTER,
            variant=variant,
        )
        second = resolver._generate_variant(
            SAMPLE_EIK,
            time_counter=SAMPLE_COUNTER,
            variant=variant,
        )
        assert first == second


def test_seam_distinct_counters_yield_distinct_eids() -> None:
    """Counters in different rotation windows must produce different EIDs.

    This guards a future skip-guard / memoization lever against collapsing
    distinct rotation windows onto a single cached EID. The offset is a full
    ROTATION_PERIOD because counters inside the same window are masked to the
    same value by design (see test_rotation_mask_equivalence_within_period).
    """

    resolver = _build_resolver()

    variant = EidVariant.MODERN_P256_X32_BE
    eid_a = resolver._generate_variant(
        SAMPLE_EIK,
        time_counter=SAMPLE_COUNTER,
        variant=variant,
    )
    eid_b = resolver._generate_variant(
        SAMPLE_EIK,
        time_counter=SAMPLE_COUNTER + ROTATION_PERIOD,
        variant=variant,
    )
    assert eid_a != eid_b


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    raise SystemExit(pytest.main([__file__, "-q"]))
