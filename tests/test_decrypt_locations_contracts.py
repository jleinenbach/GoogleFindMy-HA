# tests/test_decrypt_locations_contracts.py
"""Contract tests for the pure decode/normalize helpers of ``decrypt_locations``.

Scope: the input-driven data-transformation helpers that are reachable without
real crypto, network, or protobuf fixtures. The deep async crypto paths
(``async_retrieve_identity_key``, ``async_decrypt_location_response_locations``)
are covered by dedicated crypto tests and are out of scope here (see the W1
plan negative protocol).

Each helper is exercised across its enumerated ``return``/``raise`` exits
(0-hit / error / plausible), following the AP-W1.1 counter discipline (CA-F8).

H5 (bool-exclusion parity): ``_parse_epoch_seconds`` now rejects ``bool`` up
front, matching ``normalize_pair_date_value`` /
``normalize_creation_timestamp_value`` (which guard with
``not isinstance(raw, bool)``). The parity is pinned below. Note: the observed
return value for ``True``/``False`` was already ``None`` before the guard (via
``float(True) == 1.0`` falling under the plausibility floor), so this is a
type-parity / defense-in-depth fix, not a changed-output bug — the test pins
the invariant rather than a red-before-green regression.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as d,
)

# A fixed "now" well after 2000-01-01 with headroom below the drift window.
_NOW = 1_700_001_000.0  # 2023-11-14, plausible epoch seconds
_PLAUSIBLE = 1_700_000_000.0  # slightly before _NOW
_FAR_FUTURE = _NOW + 1_000_000_000.0  # ~31 years ahead -> exceeds drift window


# --------------------------------------------------------------------------- #
# create_google_maps_link — 3 exits                                            #
# --------------------------------------------------------------------------- #


def test_maps_link_valid_coordinates() -> None:
    link = d.create_google_maps_link(52.5, 13.4)
    assert link == "https://www.google.com/maps/search/?api=1&query=52.5,13.4"


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(90.1, 0.0), (-90.1, 0.0), (0.0, 180.1), (0.0, -180.1)],
)
def test_maps_link_out_of_bounds_returns_none(lat: float, lon: float) -> None:
    assert d.create_google_maps_link(lat, lon) is None


def test_maps_link_uncoercible_returns_none() -> None:
    assert d.create_google_maps_link(None, 0.0) is None  # type: ignore[arg-type]
    assert d.create_google_maps_link("abc", 0.0) is None  # type: ignore[arg-type]


def test_maps_link_boundary_inclusive() -> None:
    """Exact bounds (+/-90, +/-180) are inclusive."""
    assert d.create_google_maps_link(90.0, 180.0) is not None
    assert d.create_google_maps_link(-90.0, -180.0) is not None


# --------------------------------------------------------------------------- #
# _parse_epoch_seconds — full exit matrix (H5 carrier)                         #
# --------------------------------------------------------------------------- #


def test_parse_epoch_plausible_int() -> None:
    assert d._parse_epoch_seconds(int(_PLAUSIBLE), _NOW) == _PLAUSIBLE


def test_parse_epoch_string_is_sanitized() -> None:
    """Leading/trailing whitespace, BOM and NBSP are stripped before float()."""
    raw = f"﻿  {int(_PLAUSIBLE)}  "
    assert d._parse_epoch_seconds(raw, _NOW) == _PLAUSIBLE


def test_parse_epoch_valid_bytes() -> None:
    assert d._parse_epoch_seconds(str(int(_PLAUSIBLE)).encode(), _NOW) == _PLAUSIBLE


def test_parse_epoch_invalid_utf8_bytes_returns_none() -> None:
    assert d._parse_epoch_seconds(b"\xff\xfe", _NOW) is None


def test_parse_epoch_protobuf_time_object() -> None:
    ts = SimpleNamespace(seconds=int(_PLAUSIBLE), nanos=500_000_000)
    assert d._parse_epoch_seconds(ts, _NOW) == pytest.approx(_PLAUSIBLE + 0.5)


def test_parse_epoch_protobuf_time_bad_field_returns_none() -> None:
    ts = SimpleNamespace(seconds="not-a-number")
    assert d._parse_epoch_seconds(ts, _NOW) is None


def test_parse_epoch_millisecond_scaling() -> None:
    assert d._parse_epoch_seconds(_PLAUSIBLE * 1e3, _NOW) == _PLAUSIBLE


def test_parse_epoch_microsecond_scaling() -> None:
    assert d._parse_epoch_seconds(_PLAUSIBLE * 1e6, _NOW) == _PLAUSIBLE


def test_parse_epoch_non_finite_returns_none() -> None:
    assert d._parse_epoch_seconds(float("nan"), _NOW) is None
    assert d._parse_epoch_seconds(float("inf"), _NOW) is None


def test_parse_epoch_too_old_returns_none() -> None:
    assert d._parse_epoch_seconds(100.0, _NOW) is None  # before 2000-01-01


def test_parse_epoch_too_far_future_returns_none() -> None:
    assert d._parse_epoch_seconds(_FAR_FUTURE, _NOW) is None


def test_parse_epoch_uncoercible_returns_none() -> None:
    assert d._parse_epoch_seconds(object(), _NOW) is None


# --------------------------------------------------------------------------- #
# normalize_pair_date_value / normalize_creation_timestamp_value               #
# --------------------------------------------------------------------------- #

_NORMALIZERS = (
    d.normalize_pair_date_value,
    d.normalize_creation_timestamp_value,
)


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_plausible_numeric(fn) -> None:
    assert fn(int(_PLAUSIBLE), now_wall=_NOW) == int(_PLAUSIBLE)


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_millisecond_scaling(fn) -> None:
    assert fn(_PLAUSIBLE * 1e3, now_wall=_NOW) == int(_PLAUSIBLE)


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_microsecond_scaling(fn) -> None:
    assert fn(_PLAUSIBLE * 1e6, now_wall=_NOW) == int(_PLAUSIBLE)


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_non_finite_returns_none(fn) -> None:
    assert fn(float("nan"), now_wall=_NOW) is None
    assert fn(float("inf"), now_wall=_NOW) is None


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_too_old_returns_none(fn) -> None:
    assert fn(100, now_wall=_NOW) is None


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_too_far_future_returns_none(fn) -> None:
    assert fn(_FAR_FUTURE, now_wall=_NOW) is None


@pytest.mark.parametrize("fn", _NORMALIZERS)
def test_normalizer_string_delegates_to_parser(fn) -> None:
    """A non-numeric (str) input delegates to _parse_epoch_seconds."""
    assert fn(str(int(_PLAUSIBLE)), now_wall=_NOW) == int(_PLAUSIBLE)


def test_normalize_pair_date_none_returns_none() -> None:
    assert d.normalize_pair_date_value(None, now_wall=_NOW) is None


# --------------------------------------------------------------------------- #
# H5 — bool-exclusion parity across all three epoch parsers                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flag", [True, False])
def test_h5_bool_exclusion_parity(flag: bool) -> None:
    """A bool must never be interpreted as an epoch number by any parser.

    All three parsers reject bool -> None. ``_parse_epoch_seconds`` now rejects
    it via an explicit ``isinstance(value, bool)`` guard (parity with the two
    numeric normalizers), rather than only accidentally via the plausibility
    floor. The parity is the contract; the returned value was ``None`` already.
    """
    assert d._parse_epoch_seconds(flag, _NOW) is None
    assert d.normalize_pair_date_value(flag, now_wall=_NOW) is None
    assert d.normalize_creation_timestamp_value(flag, now_wall=_NOW) is None


# --------------------------------------------------------------------------- #
# _ensure_bytes — 3 exits                                                      #
# --------------------------------------------------------------------------- #


def test_ensure_bytes_passthrough() -> None:
    payload = b"\x01\x02"
    assert d._ensure_bytes(payload) is payload


def test_ensure_bytes_bytearray_is_copied_to_bytes() -> None:
    result = d._ensure_bytes(bytearray(b"\x01\x02"))
    assert isinstance(result, bytes) and not isinstance(result, bytearray)
    assert result == b"\x01\x02"


@pytest.mark.parametrize("value", ["str", 42, None, memoryview(b"x")])
def test_ensure_bytes_rejects_other_types(value: object) -> None:
    assert d._ensure_bytes(value) is None


# --------------------------------------------------------------------------- #
# _is_valid_latlon — 4 exits                                                   #
# --------------------------------------------------------------------------- #


def test_is_valid_latlon_accepts_in_bounds() -> None:
    assert d._is_valid_latlon(45.0, 9.0) is True
    assert d._is_valid_latlon(0.0, 0.0) is True  # 0.0 is valid (not falsy-rejected)


def test_is_valid_latlon_uncoercible_returns_false() -> None:
    assert d._is_valid_latlon(None, 0.0) is False  # type: ignore[arg-type]


def test_is_valid_latlon_non_finite_returns_false() -> None:
    assert d._is_valid_latlon(float("nan"), 0.0) is False
    assert d._is_valid_latlon(0.0, float("inf")) is False


def test_is_valid_latlon_out_of_bounds_returns_false() -> None:
    assert d._is_valid_latlon(91.0, 0.0) is False
    assert d._is_valid_latlon(0.0, 181.0) is False


# --------------------------------------------------------------------------- #
# _extract_canonic_id — duck-typed protobuf                                    #
# --------------------------------------------------------------------------- #


def _canonic_proto(ids: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        deviceMetadata=SimpleNamespace(
            identifierInformation=SimpleNamespace(
                canonicIds=SimpleNamespace(canonicId=ids)
            )
        )
    )


def test_extract_canonic_id_returns_first_string() -> None:
    proto = _canonic_proto([SimpleNamespace(id=""), SimpleNamespace(id="canonical-42")])
    assert d._extract_canonic_id(proto) == "canonical-42"


def test_extract_canonic_id_empty_list_returns_none() -> None:
    assert d._extract_canonic_id(_canonic_proto([])) is None


def test_extract_canonic_id_non_string_ids_returns_none() -> None:
    assert d._extract_canonic_id(_canonic_proto([SimpleNamespace(id=123)])) is None


def test_extract_canonic_id_missing_attribute_returns_none() -> None:
    assert d._extract_canonic_id(object()) is None  # attribute chain raises


# --------------------------------------------------------------------------- #
# is_real_location_record / any_real_location_record — reauth-budget allowlist #
# --------------------------------------------------------------------------- #


def test_is_real_location_record_none_and_empty() -> None:
    assert d.is_real_location_record(None) is False
    assert d.is_real_location_record({}) is False


def test_is_real_location_record_true_for_coordinates() -> None:
    assert d.is_real_location_record({"latitude": 52.5, "longitude": 13.4}) is True


def test_is_real_location_record_zero_coordinates_are_real() -> None:
    """0.0/0.0 is a legitimate coordinate (``is not None``, not truthiness)."""
    assert d.is_real_location_record({"latitude": 0.0, "longitude": 0.0}) is True


def test_is_real_location_record_metadata_sentinel_is_false() -> None:
    """SEMANTIC / metadata-only rows carry no coordinates -> not authenticated."""
    assert (
        d.is_real_location_record({"semantic_name": "Home", "latitude": None}) is False
    )


def test_any_real_location_record_mixed_list() -> None:
    records = [
        {"semantic_name": "Home", "latitude": None, "longitude": None},
        {"latitude": 1.0, "longitude": 2.0},
    ]
    assert d.any_real_location_record(records) is True


def test_any_real_location_record_none_empty_and_all_metadata() -> None:
    assert d.any_real_location_record(None) is False
    assert d.any_real_location_record([]) is False
    assert d.any_real_location_record([{"latitude": None, "longitude": None}]) is False


# --------------------------------------------------------------------------- #
# EIK cache helpers                                                            #
# --------------------------------------------------------------------------- #


def test_eik_cache_key_is_deterministic_and_flip_sensitive() -> None:
    blob, version = b"identity-key-blob", 3
    key_a = d._get_eik_cache_key(blob, version, False)
    key_b = d._get_eik_cache_key(blob, version, False)
    assert key_a == key_b  # deterministic
    assert d._get_eik_cache_key(blob, version, True) != key_a  # flip_state matters
    assert d._get_eik_cache_key(blob, version + 1, False) != key_a  # version matters


def test_clear_and_stats_eik_cache() -> None:
    d._eik_cache["k"] = b"v"
    stats = d.get_eik_cache_stats()
    assert stats["size"] >= 1
    d.clear_eik_cache()
    assert d.get_eik_cache_stats()["size"] == 0


# --------------------------------------------------------------------------- #
# Sync facade guard — running-loop rejection                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sync_facade_rejects_running_loop() -> None:
    """The sync facade must refuse to block a running event loop."""
    with pytest.raises(RuntimeError):
        # The running-loop guard fires before ``cache`` is touched.
        d.decrypt_location_response_locations(object(), cache=SimpleNamespace())
