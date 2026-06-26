# tests/test_coordinator_helpers_subentry.py
"""Branch-Coverage tests for ``coordinator.helpers.subentry``.

The 8 helpers in :mod:`custom_components.googlefindmy.coordinator.helpers.subentry`
are pure functions: no Home-Assistant runtime, no I/O, no globals.  These tests
exercise every documented branch via the Aniche-style Specification → Boundary →
Structural progression (PLAN_GFM_TEST_EXPANSION_SPRINT.md AP-1.1a).

Branch budget (notes-sidecar ``helpers/subentry.py``): 30 branches across 8
functions.  Each test docstring names the function and the branch the case
exercises so a failing test points at the spec line, not the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.googlefindmy.coordinator.helpers.subentry import (
    detect_missing_core_subentry_keys,
    extract_subentry_group_key,
    filter_provisional_identifier,
    format_epoch_utc,
    group_devices_by_subentry,
    normalize_epoch_seconds,
    parse_last_seen_timestamp,
    sanitize_subentry_identifier,
)

# ---------------------------------------------------------------------------
# sanitize_subentry_identifier — 3 branches (str/non-str, empty, normal)
# ---------------------------------------------------------------------------


class TestSanitizeSubentryIdentifier:
    """Cover the three documented branches of ``sanitize_subentry_identifier``."""

    @pytest.mark.parametrize(
        "candidate",
        [None, 0, 1, 1.5, b"abc", ["abc"], {"abc"}, object()],
    )
    def test_non_string_returns_none(self, candidate):
        """Branch 1: non-string inputs collapse to ``None``."""
        assert sanitize_subentry_identifier(candidate) is None

    @pytest.mark.parametrize("candidate", ["", "   ", "\t\n", " " * 3 + "   "])
    def test_empty_or_whitespace_returns_none(self, candidate):
        """Branch 2: empty or whitespace-only strings collapse to ``None``.

        Note ``str.strip()`` only handles ASCII + a few unicode whitespace
        characters; `` `` (NBSP) is NOT stripped, so an all-NBSP input
        survives with content.  We test both pure-ASCII whitespace (None) and
        the NBSP edge case (passes through, see below).
        """
        # All-ASCII / all-whitespace cases must collapse
        if candidate.strip():
            # NBSP-only survives ``strip`` -> stays non-empty -> identifier returned
            assert sanitize_subentry_identifier(candidate) == candidate.strip()
        else:
            assert sanitize_subentry_identifier(candidate) is None

    def test_strips_surrounding_whitespace(self):
        """Branch 3: valid identifiers are stripped of leading/trailing space."""
        assert sanitize_subentry_identifier("  core_tracking  ") == "core_tracking"

    def test_passes_through_when_no_whitespace(self):
        """Branch 3 (clean): identifiers without surrounding space pass through."""
        assert sanitize_subentry_identifier("abc-123") == "abc-123"


# ---------------------------------------------------------------------------
# normalize_epoch_seconds — 5 branches
#   (str strip + empty, float-cast fail, non-finite, ms->s, OverflowError)
# ---------------------------------------------------------------------------


class TestNormalizeEpochSeconds:
    """Cover the five branches of ``normalize_epoch_seconds``."""

    def test_int_seconds_pass_through(self):
        """Branch: int input -> identical int return."""
        assert normalize_epoch_seconds(1_700_000_000) == 1_700_000_000

    def test_float_seconds_truncated_to_int(self):
        """Branch: float input is truncated by ``int()``."""
        assert normalize_epoch_seconds(1.7) == 1
        assert normalize_epoch_seconds(-0.4) == 0

    def test_string_numeric_is_parsed(self):
        """Branch: string numerics are parsed via ``float()``."""
        assert normalize_epoch_seconds("1700000000") == 1_700_000_000
        assert normalize_epoch_seconds("  1700000000.5  ") == 1_700_000_000

    def test_milliseconds_auto_converted(self):
        """Branch: |value| >= 1e11 is treated as ms and divided by 1000."""
        assert normalize_epoch_seconds(1_700_000_000_000) == 1_700_000_000
        # Negative ms also normalised
        assert normalize_epoch_seconds(-1_700_000_000_000) == -1_700_000_000

    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_empty_string_returns_none(self, value):
        """Branch: empty stripped string collapses to ``None`` before ``float``."""
        assert normalize_epoch_seconds(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "abc",
            "1.2.3",
            object(),
            b"123",
            bytearray(b"123"),
            memoryview(b"123"),
        ],
    )
    def test_unparseable_returns_none(self, value):
        """Branch: non-string / byte-like / non-numeric -> ``None``.

        ``float(b"123")`` is legal Python but semantically wrong here: callers
        must decode raw bytes before passing them in. The byte-like guard makes
        that contract explicit instead of silently coercing ASCII payloads.
        """
        assert normalize_epoch_seconds(value) is None

    def test_non_finite_returns_none(self):
        """Branch: NaN and ±inf collapse to ``None``."""
        assert normalize_epoch_seconds(float("nan")) is None
        assert normalize_epoch_seconds(float("inf")) is None
        assert normalize_epoch_seconds(float("-inf")) is None
        # via string path too
        assert normalize_epoch_seconds("inf") is None


# ---------------------------------------------------------------------------
# format_epoch_utc — 3 branches (None passthrough, datetime fail, normal)
# ---------------------------------------------------------------------------


class TestFormatEpochUtc:
    """Cover the three branches of ``format_epoch_utc``."""

    def test_normal_path_returns_iso_with_z(self):
        """Branch: valid epoch -> ISO 8601 with trailing ``Z``."""
        # 2023-11-14T22:13:20Z -> 1700000000
        assert format_epoch_utc(1_700_000_000) == "2023-11-14T22:13:20Z"

    def test_milliseconds_input_also_works(self):
        """Branch: ms input gets normalised to s before formatting."""
        assert format_epoch_utc(1_700_000_000_000) == "2023-11-14T22:13:20Z"

    @pytest.mark.parametrize("value", [None, "abc", float("nan"), float("inf")])
    def test_unparseable_returns_none(self, value):
        """Branch: ``normalize_epoch_seconds`` returns ``None`` -> ``None``."""
        assert format_epoch_utc(value) is None

    def test_overflow_returns_none(self):
        """Branch: ``datetime.fromtimestamp`` raises ``OverflowError`` -> ``None``."""
        # 1e18 is far outside datetime's range on every platform
        # But normalize_epoch_seconds may auto-divide; force outside both.
        # We use 1e15 -> 1e12 seconds, still way above year-9999 max.
        # On Python 3.11+ this raises OverflowError or ValueError.
        assert format_epoch_utc(10**16) is None


# ---------------------------------------------------------------------------
# parse_last_seen_timestamp — 3 branches
#   (numeric path, ISO 8601 path, fallback to None)
# ---------------------------------------------------------------------------


class TestParseLastSeenTimestamp:
    """Cover the three branches of ``parse_last_seen_timestamp``."""

    def test_numeric_path(self):
        """Branch: numeric input passes through ``normalize_epoch_seconds``."""
        assert parse_last_seen_timestamp(1_700_000_000) == pytest.approx(
            1_700_000_000.0
        )

    def test_ms_path_via_numeric(self):
        """Branch: ms numeric input is normalised to s."""
        assert parse_last_seen_timestamp(1_700_000_000_000) == pytest.approx(
            1_700_000_000.0
        )

    def test_iso8601_with_z_suffix(self):
        """Branch: ISO 8601 string with ``Z`` is parsed via ``fromisoformat``."""
        # ``"Z"`` is replaced with ``+00:00`` before fromisoformat
        result = parse_last_seen_timestamp("2023-11-14T22:13:20Z")
        expected = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC).timestamp()
        assert result == pytest.approx(expected)

    def test_iso8601_with_offset(self):
        """Branch: ISO 8601 with explicit UTC offset is also parsed."""
        result = parse_last_seen_timestamp("2023-11-14T22:13:20+00:00")
        expected = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC).timestamp()
        assert result == pytest.approx(expected)

    @pytest.mark.parametrize("value", ["nope", "2023-13-99", "not-a-date"])
    def test_invalid_iso_returns_none(self, value):
        """Branch: numeric parse fails AND ISO parse raises ``ValueError`` -> ``None``."""
        assert parse_last_seen_timestamp(value) is None

    @pytest.mark.parametrize("value", [None, object(), [1, 2, 3]])
    def test_non_string_non_numeric_returns_none(self, value):
        """Branch: non-string and non-numeric -> ``None`` via final fallback."""
        assert parse_last_seen_timestamp(value) is None


# ---------------------------------------------------------------------------
# group_devices_by_subentry — 4 branches
#   (init groups, non-Mapping row skip, missing id skip, fallback target)
# ---------------------------------------------------------------------------


class TestGroupDevicesBySubentry:
    """Cover the four branches of ``group_devices_by_subentry``."""

    def test_initial_groups_contain_all_keys(self):
        """Branch: ``subentry_keys`` and ``fallback_key`` initialise the result."""
        result = group_devices_by_subentry(
            devices=[],
            device_to_subentry={},
            fallback_key="core_tracking",
            subentry_keys={"sub_a", "sub_b"},
        )
        assert set(result) == {"sub_a", "sub_b", "core_tracking"}
        assert all(v == [] for v in result.values())

    def test_fallback_key_added_when_missing_from_subentry_keys(self):
        """Branch: ``setdefault`` adds the fallback even when not in ``subentry_keys``."""
        result = group_devices_by_subentry(
            devices=[],
            device_to_subentry={},
            fallback_key="extra_fallback",
            subentry_keys={"sub_a"},
        )
        assert "extra_fallback" in result

    def test_device_routed_to_assigned_subentry(self):
        """Branch: a device with a mapping entry lands in the assigned group."""
        result = group_devices_by_subentry(
            devices=[{"device_id": "d1", "name": "Alpha"}],
            device_to_subentry={"d1": "sub_a"},
            fallback_key="core_tracking",
            subentry_keys={"sub_a"},
        )
        assert result["sub_a"] == [{"device_id": "d1", "name": "Alpha"}]
        assert result["core_tracking"] == []

    def test_device_routed_to_fallback_when_unmapped(self):
        """Branch: unmapped devices land in ``fallback_key``."""
        result = group_devices_by_subentry(
            devices=[{"device_id": "d2", "name": "Bravo"}],
            device_to_subentry={},
            fallback_key="core_tracking",
            subentry_keys={"sub_a"},
        )
        assert result["core_tracking"] == [{"device_id": "d2", "name": "Bravo"}]

    def test_id_field_alternative(self):
        """Branch: ``id`` is accepted as alternative to ``device_id``."""
        result = group_devices_by_subentry(
            devices=[{"id": "d3", "name": "Charlie"}],
            device_to_subentry={"d3": "sub_a"},
            fallback_key="core_tracking",
            subentry_keys={"sub_a"},
        )
        assert result["sub_a"][0]["id"] == "d3"

    def test_non_mapping_rows_skipped(self):
        """Branch: rows that aren't ``Mapping`` instances are skipped silently."""
        result = group_devices_by_subentry(
            devices=["not-a-mapping", None, 42, ["list"]],
            device_to_subentry={},
            fallback_key="core_tracking",
            subentry_keys=set(),
        )
        assert result == {"core_tracking": []}

    def test_missing_or_non_string_id_skipped(self):
        """Branch: rows without a string ``device_id``/``id`` are skipped."""
        result = group_devices_by_subentry(
            devices=[{"foo": "bar"}, {"device_id": 123}, {"id": None}],
            device_to_subentry={},
            fallback_key="core_tracking",
            subentry_keys=set(),
        )
        assert result["core_tracking"] == []

    def test_new_subentry_key_in_mapping_creates_bucket(self):
        """Branch: ``setdefault`` on assignment creates buckets dynamically."""
        result = group_devices_by_subentry(
            devices=[{"device_id": "d1"}],
            device_to_subentry={"d1": "sub_new"},
            fallback_key="core_tracking",
            subentry_keys=set(),
        )
        assert "sub_new" in result
        assert result["sub_new"] == [{"device_id": "d1"}]

    def test_returned_rows_are_copies(self):
        """Branch (defensive): rows in output are ``dict(row)`` copies, not refs."""
        src = {"device_id": "d1"}
        result = group_devices_by_subentry(
            devices=[src],
            device_to_subentry={"d1": "sub_a"},
            fallback_key="core_tracking",
            subentry_keys={"sub_a"},
        )
        # Mutating the source must not bleed into the returned copy
        src["device_id"] = "MUTATED"
        assert result["sub_a"] == [{"device_id": "d1"}]


# ---------------------------------------------------------------------------
# filter_provisional_identifier — 4 branches
#   (None passthrough, non-provisional passthrough,
#    service-key-match, tracker-key-match, mismatch)
# ---------------------------------------------------------------------------


class TestFilterProvisionalIdentifier:
    """Cover the four branches of ``filter_provisional_identifier``."""

    def test_none_passes_through(self):
        """Branch 1: ``None`` -> ``(None, False)``."""
        assert filter_provisional_identifier(
            None, group_key="core_service", entry_subentry_id="x"
        ) == (None, False)

    def test_non_provisional_passes_through(self):
        """Branch 2: identifiers without ``-provisional`` suffix are unchanged."""
        ident = "abc"
        assert filter_provisional_identifier(
            ident, group_key="core_service", entry_subentry_id="x"
        ) == (ident, False)

    def test_provisional_service_match_passes(self):
        """Branch 3a: provisional matching ``entry_subentry_id`` on service-key OK."""
        ident = "abc-provisional"
        assert filter_provisional_identifier(
            ident,
            group_key="core_service",
            entry_subentry_id=ident,
        ) == (ident, False)

    def test_provisional_tracker_match_passes(self):
        """Branch 3b: provisional matching ``entry_subentry_id`` on tracker-key OK."""
        ident = "abc-provisional"
        assert filter_provisional_identifier(
            ident,
            group_key="core_tracking",
            entry_subentry_id=ident,
        ) == (ident, False)

    def test_provisional_mismatch_filtered(self):
        """Branch 4: provisional not matching ``entry_subentry_id`` -> ``(None, True)``."""
        assert filter_provisional_identifier(
            "abc-provisional",
            group_key="core_service",
            entry_subentry_id="different",
        ) == (None, True)

    def test_provisional_unknown_group_key_filtered(self):
        """Branch 4b: provisional with non-core group key falls through to filter."""
        assert filter_provisional_identifier(
            "abc-provisional",
            group_key="some_other_group",
            entry_subentry_id="abc-provisional",
        ) == (None, True)

    def test_custom_core_keys_respected(self):
        """Branch 3 (custom keys): ``core_service_key``/``core_tracker_key`` are honoured."""
        ident = "x-provisional"
        # With custom keys, only the matching one passes
        assert filter_provisional_identifier(
            ident,
            group_key="my_service",
            entry_subentry_id=ident,
            core_service_key="my_service",
            core_tracker_key="my_tracker",
        ) == (ident, False)
        # And the non-matching one filters
        assert filter_provisional_identifier(
            ident,
            group_key="my_service",
            entry_subentry_id="other",
            core_service_key="my_service",
            core_tracker_key="my_tracker",
        ) == (None, True)


# ---------------------------------------------------------------------------
# extract_subentry_group_key — 3 branches
#   (data.group_key wins, subentry_id falls back, hard fallback)
# ---------------------------------------------------------------------------


class TestExtractSubentryGroupKey:
    """Cover the three branches of ``extract_subentry_group_key``."""

    def test_group_key_in_data_wins(self):
        """Branch 1: ``data['group_key']`` takes priority over everything."""
        assert (
            extract_subentry_group_key(
                {"group_key": "core_service"}, subentry_id="ignored"
            )
            == "core_service"
        )

    def test_group_key_coerced_to_str(self):
        """Branch 1 (coerce): non-string ``group_key`` is converted via ``str``."""
        assert (
            extract_subentry_group_key(
                {"group_key": 42}, subentry_id=None, fallback="fb"
            )
            == "42"
        )

    def test_falls_back_to_subentry_id(self):
        """Branch 2: missing ``data`` keys -> ``subentry_id`` is returned."""
        assert extract_subentry_group_key(None, subentry_id="sub_x") == "sub_x"
        assert extract_subentry_group_key({}, subentry_id="sub_y") == "sub_y"

    def test_subentry_id_coerced_to_str(self):
        """Branch 2 (coerce): non-string ``subentry_id`` is converted via ``str``."""
        assert extract_subentry_group_key(None, subentry_id=99) == "99"

    def test_hard_fallback_when_nothing_set(self):
        """Branch 3: both ``data`` and ``subentry_id`` absent -> ``fallback``."""
        assert extract_subentry_group_key(None, subentry_id=None) == "core_tracking"
        assert (
            extract_subentry_group_key(None, subentry_id=None, fallback="custom_fb")
            == "custom_fb"
        )

    def test_data_without_group_key_falls_through(self):
        """Branch 1->2 transition: ``data`` present but no ``group_key``."""
        assert (
            extract_subentry_group_key({"other": "x"}, subentry_id="sub_z") == "sub_z"
        )


# ---------------------------------------------------------------------------
# detect_missing_core_subentry_keys — 3 branches
#   (none missing, one missing, both missing)
# ---------------------------------------------------------------------------


class TestDetectMissingCoreSubentryKeys:
    """Cover the three branches of ``detect_missing_core_subentry_keys``."""

    def test_none_missing(self):
        """Branch 1: both core keys present -> empty set."""
        assert (
            detect_missing_core_subentry_keys({"core_service", "core_tracking"})
            == set()
        )

    def test_extra_keys_ignored(self):
        """Branch 1b: extra keys don't matter, only core-key absence counts."""
        present = {"core_service", "core_tracking", "extra"}
        assert detect_missing_core_subentry_keys(present) == set()

    def test_service_missing(self):
        """Branch 2: only tracker present -> ``{service}`` returned."""
        assert detect_missing_core_subentry_keys({"core_tracking"}) == {"core_service"}

    def test_tracker_missing(self):
        """Branch 2b: only service present -> ``{tracker}`` returned."""
        assert detect_missing_core_subentry_keys({"core_service"}) == {"core_tracking"}

    def test_both_missing(self):
        """Branch 3: empty input -> both required keys returned."""
        assert detect_missing_core_subentry_keys(set()) == {
            "core_service",
            "core_tracking",
        }

    def test_custom_keys_respected(self):
        """Branch (custom): overriding key names is honoured."""
        assert detect_missing_core_subentry_keys(
            {"svc"}, service_key="svc", tracker_key="trk"
        ) == {"trk"}
        assert detect_missing_core_subentry_keys(
            set(), service_key="svc", tracker_key="trk"
        ) == {"svc", "trk"}
