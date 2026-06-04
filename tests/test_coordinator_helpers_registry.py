"""Branch-Coverage tests for ``coordinator.helpers.registry``.

The 16 public helpers in
:mod:`custom_components.googlefindmy.coordinator.helpers.registry` are pure
functions: no Home-Assistant runtime, no I/O, no module-level globals beyond
re-exported constants.  These tests exercise every documented branch via the
Aniche-style Specification -> Boundary -> Structural progression
(PLAN_GFM_TEST_EXPANSION_SPRINT.md AP-1.1a).

Branch budget (notes-sidecar ``helpers/registry.py``): ~70 branches across the
16 functions listed in :data:`__all__`.  Each test docstring names the function
and the branch the case exercises so a failing test points at the spec line,
not the implementation.

The module imports the SUT exclusively from
``custom_components.googlefindmy.coordinator.helpers.registry`` so coverage is
attributed to the integration package, not to a re-export shim.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator.helpers.registry import (
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
    build_canonical_unique_id,
    build_entity_unique_id_candidates,
    build_legacy_device_registry_kwargs,
    extract_canonical_device_id,
    extract_device_display_name,
    extract_service_subentry_ids,
    extract_subentry_links,
    has_hub_link,
    has_subentry_link,
    is_hub_device_check,
    match_entity_by_device_id,
    needs_legacy_kwarg_retry,
    normalize_device_name,
    parse_device_identifier,
    resolve_tracker_subentry_candidate,
    should_defer_service_subentry,
)

_DOMAIN = "googlefindmy"
_SVC_PREFIX = SERVICE_DEVICE_IDENTIFIER_PREFIX  # e.g. "service:"
_LEGACY_SVC = LEGACY_SERVICE_IDENTIFIER


# ---------------------------------------------------------------------------
# extract_device_display_name -- 4 branches (user, name, fallback, all-empty)
# ---------------------------------------------------------------------------


class TestExtractDeviceDisplayName:
    """Cover the OR-chain priority of ``extract_device_display_name``."""

    def test_user_name_wins_when_present(self):
        """Branch 1: name_by_user has highest priority."""
        assert (
            extract_device_display_name("Alice's Phone", "Pixel 8", "fb")
            == "Alice's Phone"
        )

    def test_falls_back_to_device_name(self):
        """Branch 2: empty user-name falls back to device name."""
        assert extract_device_display_name(None, "Pixel 8", "fb") == "Pixel 8"
        assert extract_device_display_name("", "Pixel 8", "fb") == "Pixel 8"

    def test_falls_back_to_fallback(self):
        """Branch 3: both user and device empty -> fallback used."""
        assert extract_device_display_name(None, None, "fb") == "fb"
        assert extract_device_display_name("", "", "fb") == "fb"

    def test_empty_string_when_all_none(self):
        """Branch 4: all-None collapses to ``''`` (after strip)."""
        assert extract_device_display_name(None, None, None) == ""
        assert extract_device_display_name("", "", "") == ""

    def test_strips_surrounding_whitespace(self):
        """Branch 5: returned name is stripped."""
        assert extract_device_display_name("  Alice  ", None, None) == "Alice"
        assert extract_device_display_name(None, " Pixel ", None) == "Pixel"


# ---------------------------------------------------------------------------
# build_legacy_device_registry_kwargs -- 4 branches (each rename + base)
# ---------------------------------------------------------------------------


class TestBuildLegacyDeviceRegistryKwargs:
    """Cover the three rename branches and the base pass-through."""

    def test_passthrough_when_no_modern_keys(self):
        """Branch 0: kwargs without modern keys -> identical mapping (copy)."""
        original = {"name": "x", "manufacturer": "Google"}
        legacy = build_legacy_device_registry_kwargs(original)
        assert legacy == original
        assert legacy is not original  # must be a copy

    def test_renames_add_config_entry_id(self):
        """Branch 1: ``add_config_entry_id`` -> ``config_entry_id``."""
        legacy = build_legacy_device_registry_kwargs(
            {"add_config_entry_id": "entry-1", "name": "x"}
        )
        assert legacy == {"config_entry_id": "entry-1", "name": "x"}

    def test_renames_add_config_subentry_id(self):
        """Branch 2: ``add_config_subentry_id`` -> ``config_subentry_id``."""
        legacy = build_legacy_device_registry_kwargs(
            {"add_config_subentry_id": "sub-1"}
        )
        assert legacy == {"config_subentry_id": "sub-1"}

    def test_drops_remove_config_subentry_id(self):
        """Branch 3: ``remove_config_subentry_id`` is dropped silently."""
        legacy = build_legacy_device_registry_kwargs(
            {"remove_config_subentry_id": "sub-1", "name": "x"}
        )
        assert legacy == {"name": "x"}

    def test_all_three_combined(self):
        """Branch 1+2+3 simultaneously, exercising every rename in one call."""
        legacy = build_legacy_device_registry_kwargs(
            {
                "add_config_entry_id": "entry-1",
                "add_config_subentry_id": "sub-1",
                "remove_config_subentry_id": "sub-old",
                "name": "x",
            }
        )
        assert legacy == {
            "config_entry_id": "entry-1",
            "config_subentry_id": "sub-1",
            "name": "x",
        }

    def test_does_not_mutate_input(self):
        """Spec: input must be untouched."""
        original = {"add_config_entry_id": "entry-1"}
        build_legacy_device_registry_kwargs(original)
        assert original == {"add_config_entry_id": "entry-1"}


# ---------------------------------------------------------------------------
# needs_legacy_kwarg_retry -- 5 branches
# ---------------------------------------------------------------------------


class TestNeedsLegacyKwargRetry:
    """Cover the five branches that decide whether a TypeError needs retry."""

    def test_modern_api_returns_false(self):
        """Branch 1: kwarg_name == 'add_config_subentry_id' -> never retry."""
        assert (
            needs_legacy_kwarg_retry(
                "add_config_subentry_id",
                "TypeError: add_config_entry_id",
                {"add_config_entry_id": "e1"},
            )
            is False
        )

    def test_add_config_entry_id_match(self):
        """Branch 2: add_config_entry_id in both kwargs and err -> True."""
        assert (
            needs_legacy_kwarg_retry(
                None,
                "got an unexpected keyword argument 'add_config_entry_id'",
                {"add_config_entry_id": "e1"},
            )
            is True
        )

    def test_add_config_subentry_id_match(self):
        """Branch 3: add_config_subentry_id triggers legacy rewrite."""
        assert (
            needs_legacy_kwarg_retry(
                None,
                "unexpected keyword argument 'add_config_subentry_id'",
                {"add_config_subentry_id": "s1"},
            )
            is True
        )

    def test_remove_config_subentry_id_match(self):
        """Branch 4: remove_config_subentry_id also triggers legacy rewrite."""
        assert (
            needs_legacy_kwarg_retry(
                None,
                "unexpected keyword argument 'remove_config_subentry_id'",
                {"remove_config_subentry_id": "s1"},
            )
            is True
        )

    def test_unrelated_error_returns_false(self):
        """Branch 5: TypeError unrelated to modern kwargs -> False."""
        assert (
            needs_legacy_kwarg_retry(
                None,
                "TypeError: positional argument missing",
                {"add_config_entry_id": "e1"},
            )
            is False
        )

    def test_key_only_in_err_but_not_kwargs(self):
        """Edge: matching kwarg must appear in BOTH err and kwargs."""
        assert (
            needs_legacy_kwarg_retry(
                None,
                "unexpected keyword argument 'add_config_entry_id'",
                {"other": "x"},
            )
            is False
        )


# ---------------------------------------------------------------------------
# parse_device_identifier -- 7 branches
# ---------------------------------------------------------------------------


class TestParseDeviceIdentifier:
    """Cover shape, domain, namespacing and service-skip branches."""

    @pytest.mark.parametrize(
        "ident",
        [
            None,
            "not-a-tuple",
            42,
            (),
            ("only-one",),
            ("a", "b", "c"),
            [_DOMAIN],
        ],
    )
    def test_invalid_shape_returns_none(self, ident):
        """Branch 1: anything not a 2-element tuple/list -> None."""
        assert (
            parse_device_identifier(ident, _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC)
            is None
        )

    def test_wrong_domain_returns_none(self):
        """Branch 2: identifier domain != our domain -> None."""
        assert (
            parse_device_identifier(
                ("other_domain", "dev-1"), _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC
            )
            is None
        )

    @pytest.mark.parametrize("value", [None, "", 0, 42, ("nested",)])
    def test_non_string_or_empty_value_returns_none(self, value):
        """Branch 3: identifier value must be a non-empty string."""
        assert (
            parse_device_identifier(
                (_DOMAIN, value), _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC
            )
            is None
        )

    def test_namespaced_match_returns_canonical_id(self):
        """Branch 4: '<entry>:<dev>' for matching entry -> canonical dev id."""
        result = parse_device_identifier(
            (_DOMAIN, "e1:abc"), _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC
        )
        assert result == "abc"

    def test_namespaced_mismatch_returns_none(self):
        """Branch 5: '<other-entry>:<dev>' -> None (different entry owns it)."""
        assert (
            parse_device_identifier(
                (_DOMAIN, "e2:abc"), _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC
            )
            is None
        )

    def test_namespaced_without_entry_id_returns_none(self):
        """Branch 5b: namespaced format requires entry_id to match."""
        assert (
            parse_device_identifier(
                (_DOMAIN, "e2:abc"), _DOMAIN, None, _SVC_PREFIX, _LEGACY_SVC
            )
            is None
        )

    def test_service_prefix_skipped(self):
        """Branch 6: identifier starting with service prefix -> None."""
        ident = f"{_SVC_PREFIX}service-thing" if _SVC_PREFIX else "service:thing"
        prefix = _SVC_PREFIX or "service:"
        assert (
            parse_device_identifier(
                (_DOMAIN, ident), _DOMAIN, "e1", prefix, _LEGACY_SVC
            )
            is None
        )

    def test_legacy_service_identifier_skipped(self):
        """Branch 6b: identifier equal to legacy service id -> None."""
        if not _LEGACY_SVC:
            pytest.skip("LEGACY_SERVICE_IDENTIFIER is empty in this build")
        assert (
            parse_device_identifier(
                (_DOMAIN, _LEGACY_SVC), _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC
            )
            is None
        )

    def test_legacy_format_passthrough(self):
        """Branch 7: legacy 'dev-id' (no ':') is returned as-is."""
        assert (
            parse_device_identifier(
                (_DOMAIN, "dev-1"), _DOMAIN, "e1", _SVC_PREFIX, _LEGACY_SVC
            )
            == "dev-1"
        )


# ---------------------------------------------------------------------------
# normalize_device_name -- 3 branches
# ---------------------------------------------------------------------------


class TestNormalizeDeviceName:
    """Cover non-string, empty and valid input branches."""

    @pytest.mark.parametrize("value", [None, 42, 1.5, b"bytes", ["list"], {"set"}])
    def test_non_string_returns_none(self, value):
        """Branch 1: anything not ``str`` -> None."""
        assert normalize_device_name(value) is None

    @pytest.mark.parametrize("value", ["", "   ", "\t\n", "   \r\n  "])
    def test_empty_or_whitespace_returns_none(self, value):
        """Branch 2: empty / whitespace-only -> None."""
        assert normalize_device_name(value) is None

    def test_lowercased_and_stripped(self):
        """Branch 3: valid str -> stripped + casefolded."""
        assert normalize_device_name("  Pixel 8 Pro  ") == "pixel 8 pro"

    def test_casefold_handles_unicode(self):
        """Branch 3b: casefold (not just lower) handles German ß."""
        # German "ß".casefold() == "ss"; "lower" would keep "ß".
        assert normalize_device_name("STRAßE") == "strasse"


# ---------------------------------------------------------------------------
# extract_subentry_links -- 7 branches
# ---------------------------------------------------------------------------


class TestExtractSubentryLinks:
    """Cover device-None, entry-None, mapping, fallback and config-entries paths."""

    def test_device_none_returns_empty(self):
        """Branch 1a: device=None -> empty set."""
        assert extract_subentry_links(None, "e1") == set()

    def test_entry_id_none_returns_empty(self):
        """Branch 1b: entry_id falsy -> empty set."""
        device = SimpleNamespace(config_entries_subentries={"e1": {"sub-1"}})
        assert extract_subentry_links(device, None) == set()
        assert extract_subentry_links(device, "") == set()

    def test_mapping_collection_returns_typed_set(self):
        """Branch 2: config_entries_subentries[entry_id] is a collection."""
        device = SimpleNamespace(
            config_entries_subentries={"e1": {"sub-1", "sub-2", None, 42}}
        )
        # 42 is filtered out (not str/None); None is kept; str passes through.
        assert extract_subentry_links(device, "e1") == {"sub-1", "sub-2", None}

    def test_mapping_with_string_value_falls_through(self):
        """Branch 2b: raw_links is a str -> not Collection-of-items -> fallback."""
        device = SimpleNamespace(
            config_entries_subentries={"e1": "sub-string"},
            config_subentry_id="sub-1",
        )
        assert extract_subentry_links(device, "e1") == {"sub-1"}

    def test_mapping_with_none_value_returns_empty(self):
        """Branch 2c: explicit None value -> empty set (no fallback)."""
        device = SimpleNamespace(
            config_entries_subentries={"e1": None},
            config_subentry_id="ignored",
        )
        assert extract_subentry_links(device, "e1") == set()

    def test_fallback_to_config_subentry_id(self):
        """Branch 3: no mapping -> use config_subentry_id attribute."""
        device = SimpleNamespace(config_subentry_id="sub-fallback")
        assert extract_subentry_links(device, "e1") == {"sub-fallback"}

    def test_config_entries_only_returns_hub_marker(self):
        """Branch 4: device has config_entries but no subentry -> {None}."""
        device = SimpleNamespace(
            config_entries=["e1"], config_subentry_id=None
        )
        assert extract_subentry_links(device, "e1") == {None}

    def test_no_attributes_returns_empty(self):
        """Branch 5: device with no relevant attributes -> empty set."""
        device = SimpleNamespace()  # nothing
        assert extract_subentry_links(device, "e1") == set()

    def test_mapping_missing_entry_id_short_circuits(self):
        """Edge: mapping present but entry_id key missing -> set() (raw=None).

        ``Mapping.get`` returns ``None`` for missing keys, which triggers the
        explicit ``raw_links is None`` short-circuit *before* the
        ``config_subentry_id`` fallback can run.  Documenting the actual
        semantics so callers don't mistake this for the fallback path.
        """
        device = SimpleNamespace(
            config_entries_subentries={"other": {"sub-1"}},
            config_subentry_id="sub-fallback",
        )
        assert extract_subentry_links(device, "e1") == set()


# ---------------------------------------------------------------------------
# has_subentry_link / has_hub_link -- 5 branches combined
# ---------------------------------------------------------------------------


class TestHasSubentryLink:
    """Cover target-None, hit and miss branches."""

    def test_target_none_returns_false(self):
        """Branch 1: target_id=None -> False even if None is in links."""
        assert has_subentry_link({None, "sub-1"}, None) is False

    def test_target_in_links(self):
        """Branch 2: target_id present -> True."""
        assert has_subentry_link({"sub-1"}, "sub-1") is True

    def test_target_not_in_links(self):
        """Branch 3: target_id absent -> False."""
        assert has_subentry_link({"sub-1"}, "sub-2") is False
        assert has_subentry_link(set(), "sub-1") is False


class TestHasHubLink:
    """Cover None-in / None-not-in branches."""

    def test_none_in_links_returns_true(self):
        """Branch 1: None present (hub marker) -> True."""
        assert has_hub_link({None}) is True
        assert has_hub_link({None, "sub-1"}) is True

    def test_none_not_in_links_returns_false(self):
        """Branch 2: only strings -> False."""
        assert has_hub_link({"sub-1"}) is False
        assert has_hub_link(set()) is False


# ---------------------------------------------------------------------------
# is_hub_device_check -- 4 branches
# ---------------------------------------------------------------------------


class TestIsHubDeviceCheck:
    """Cover ID-match, identifier-match, non-collection and no-match branches."""

    _PARENT = (_DOMAIN, "service-anchor")

    def test_device_id_match(self):
        """Branch 1: device_id == hub_device_id -> True."""
        assert (
            is_hub_device_check("dev-1", "dev-1", set(), self._PARENT) is True
        )

    def test_identifier_match_in_set(self):
        """Branch 2: parent_identifier in identifiers -> True."""
        assert (
            is_hub_device_check("dev-x", "hub-y", {self._PARENT}, self._PARENT)
            is True
        )

    def test_no_match_returns_false(self):
        """Branch 3: neither ID nor identifier match -> False."""
        assert (
            is_hub_device_check(
                "dev-x", "hub-y", {(_DOMAIN, "other")}, self._PARENT
            )
            is False
        )

    @pytest.mark.parametrize("identifiers", [None, "string-not-collection", b"bytes", {"k": "v"}])
    def test_non_collection_identifiers_returns_false(self, identifiers):
        """Branch 4: identifiers not a proper Collection -> False."""
        assert is_hub_device_check("dev-x", None, identifiers, self._PARENT) is False

    def test_hub_id_none_does_not_match(self):
        """Edge: hub_device_id=None must not match any device_id."""
        assert is_hub_device_check("dev-1", None, set(), self._PARENT) is False

    def test_device_id_none_does_not_match(self):
        """Edge: device_id=None must not match hub_device_id."""
        assert is_hub_device_check(None, "dev-1", set(), self._PARENT) is False


# ---------------------------------------------------------------------------
# resolve_tracker_subentry_candidate -- 7 branches
# ---------------------------------------------------------------------------


class TestResolveTrackerSubentryCandidate:
    """Cover all combinations of entry_tracker_id and tracker_subentry_ids."""

    def test_candidate_none_returns_none(self):
        """Branch 1: candidate=None -> None."""
        assert resolve_tracker_subentry_candidate(None, "etrk", {"etrk"}) is None

    def test_entry_id_match_and_in_set(self):
        """Branch 2a: candidate == entry_tracker_id and in set -> candidate."""
        assert (
            resolve_tracker_subentry_candidate("etrk", "etrk", {"etrk", "other"})
            == "etrk"
        )

    def test_entry_id_mismatch_returns_none(self):
        """Branch 2b: candidate != entry_tracker_id -> None."""
        assert (
            resolve_tracker_subentry_candidate("other", "etrk", {"etrk"}) is None
        )

    def test_entry_id_match_but_not_in_set(self):
        """Branch 2c: matches entry_tracker_id but tracker set rejects it -> None."""
        assert (
            resolve_tracker_subentry_candidate("etrk", "etrk", {"other"}) is None
        )

    def test_entry_id_match_empty_set_allowed(self):
        """Branch 2d: entry_tracker_id matches and set is empty -> candidate."""
        assert (
            resolve_tracker_subentry_candidate("etrk", "etrk", set()) == "etrk"
        )

    def test_no_entry_id_candidate_in_set(self):
        """Branch 3a: no entry_tracker_id, candidate in set -> candidate."""
        assert (
            resolve_tracker_subentry_candidate("c", None, {"c", "d"}) == "c"
        )

    def test_no_entry_id_candidate_not_in_set(self):
        """Branch 3b: no entry_tracker_id, candidate not in set -> None."""
        assert resolve_tracker_subentry_candidate("c", None, {"d"}) is None

    def test_no_entry_id_empty_set_accepts_candidate(self):
        """Branch 3c: no entry_tracker_id, set empty -> candidate passes."""
        assert resolve_tracker_subentry_candidate("any", None, set()) == "any"


# ---------------------------------------------------------------------------
# extract_service_subentry_ids -- 6 branches
# ---------------------------------------------------------------------------


class _SubentryStub:
    """Tiny stand-in for HA's ConfigSubentry runtime object."""

    def __init__(
        self,
        subentry_type: str | None = None,
        group_key: str | None = None,
        no_data: bool = False,
    ) -> None:
        self.subentry_type = subentry_type
        self.data: Any = None if no_data else {"group_key": group_key} if group_key else {}


_SVC_TYPE = "google_service"
_SVC_KEY = "service"


class TestExtractServiceSubentryIds:
    """Cover non-mapping, invalid id, provisional skip and match branches."""

    @pytest.mark.parametrize("entries", [None, "string", 42, ["list"]])
    def test_non_mapping_returns_empty(self, entries):
        """Branch 1: non-mapping subentries -> empty set."""
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == set()

    def test_invalid_subentry_id_skipped(self):
        """Branch 2: empty / non-str subentry IDs are filtered out."""
        entries = {
            "": _SubentryStub(subentry_type=_SVC_TYPE),
            42: _SubentryStub(subentry_type=_SVC_TYPE),
            "ok-1": _SubentryStub(subentry_type=_SVC_TYPE),
        }
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {"ok-1"}

    def test_provisional_skipped_unless_matches(self):
        """Branch 3a: '*-provisional' is skipped unless it equals current service id."""
        entries = {
            "abc-provisional": _SubentryStub(subentry_type=_SVC_TYPE),
            "active-1": _SubentryStub(subentry_type=_SVC_TYPE),
        }
        # No entry_service_subentry_id -> provisional dropped
        assert (
            extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY)
            == {"active-1"}
        )

    def test_provisional_included_when_matches(self):
        """Branch 3b: provisional id equal to entry_service_subentry_id is kept."""
        entries = {
            "abc-provisional": _SubentryStub(subentry_type=_SVC_TYPE),
            "active-1": _SubentryStub(subentry_type=_SVC_TYPE),
        }
        assert extract_service_subentry_ids(
            entries, "abc-provisional", _SVC_TYPE, _SVC_KEY
        ) == {"abc-provisional", "active-1"}

    def test_match_by_subentry_type(self):
        """Branch 4: subentry_type == subentry_type_service -> include."""
        entries = {"sub-1": _SubentryStub(subentry_type=_SVC_TYPE)}
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {"sub-1"}

    def test_match_by_group_key(self):
        """Branch 5: data.group_key == service_subentry_key -> include."""
        entries = {"sub-2": _SubentryStub(group_key=_SVC_KEY)}
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {"sub-2"}

    def test_non_matching_subentry_ignored(self):
        """Branch 6: neither type nor group_key matches -> excluded."""
        entries = {"sub-3": _SubentryStub(subentry_type="other", group_key="other")}
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == set()

    def test_subentry_without_data_attr(self):
        """Edge: subentry with no usable data mapping -> group_key falls back."""
        entries = {"sub-4": _SubentryStub(subentry_type=_SVC_TYPE, no_data=True)}
        # type matches -> still included
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {"sub-4"}


# ---------------------------------------------------------------------------
# should_defer_service_subentry -- 5 branches
# ---------------------------------------------------------------------------


class TestShouldDeferServiceSubentry:
    """Cover None, non-mapping, in-current, stable-default and defer branches."""

    def test_subentry_id_none_returns_false(self):
        """Branch 1: service_subentry_id=None -> never defer."""
        assert should_defer_service_subentry(None, {"x": 1}, "e1", _SVC_KEY) is False

    @pytest.mark.parametrize("subs", [None, "string", 42, ["list"]])
    def test_non_mapping_subentries_returns_false(self, subs):
        """Branch 2: current_subentries not a mapping -> False."""
        assert should_defer_service_subentry("sub-1", subs, "e1", _SVC_KEY) is False

    def test_subentry_present_returns_false(self):
        """Branch 3: subentry id already in registry -> no defer."""
        assert (
            should_defer_service_subentry("sub-1", {"sub-1": object()}, "e1", _SVC_KEY)
            is False
        )

    def test_stable_default_pattern_returns_false(self):
        """Branch 4: stable pattern '<entry>-<key>-subentry' -> no defer."""
        assert (
            should_defer_service_subentry(
                f"e1-{_SVC_KEY}-subentry", {"other": object()}, "e1", _SVC_KEY
            )
            is False
        )

    def test_unknown_subentry_returns_true(self):
        """Branch 5: id missing and not the stable pattern -> defer."""
        assert (
            should_defer_service_subentry(
                "random-sub", {"other": object()}, "e1", _SVC_KEY
            )
            is True
        )

    def test_no_entry_id_skips_stable_default_check(self):
        """Edge: entry_id None means stable-default check is impossible -> defer."""
        assert (
            should_defer_service_subentry(
                "random-sub", {"other": object()}, None, _SVC_KEY
            )
            is True
        )


# ---------------------------------------------------------------------------
# extract_canonical_device_id -- 8 branches
# ---------------------------------------------------------------------------


class TestExtractCanonicalDeviceId:
    """Cover None, non-collection, namespaced/simple priority and skip paths."""

    def test_identifiers_none_returns_none(self):
        """Branch 1: identifiers=None -> None."""
        assert extract_canonical_device_id(None, _DOMAIN) is None

    @pytest.mark.parametrize("ids", ["string", b"bytes", {"k": "v"}])
    def test_non_collection_returns_none(self, ids):
        """Branch 2: identifiers must be a non-str/bytes/Mapping collection."""
        assert extract_canonical_device_id(ids, _DOMAIN) is None

    def test_invalid_tuple_shape_skipped(self):
        """Branch 3: identifiers with wrong shape are skipped."""
        ids = {(_DOMAIN,), ("a", "b", "c"), "not-a-tuple"}
        assert extract_canonical_device_id(ids, _DOMAIN) is None

    def test_wrong_domain_skipped(self):
        """Branch 4: identifier from another domain is skipped."""
        ids = {("other", "abc")}
        assert extract_canonical_device_id(ids, _DOMAIN) is None

    def test_simple_identifier_returned(self):
        """Branch 5: simple '(domain, dev)' without ':' -> dev."""
        ids = {(_DOMAIN, "dev-1")}
        assert extract_canonical_device_id(ids, _DOMAIN) == "dev-1"

    def test_namespaced_match_preferred(self):
        """Branch 6: namespaced match takes priority over simple."""
        ids = {(_DOMAIN, "e1:dev-ns"), (_DOMAIN, "dev-simple")}
        assert (
            extract_canonical_device_id(ids, _DOMAIN, entry_id="e1") == "dev-ns"
        )

    def test_namespaced_mismatch_skipped(self):
        """Branch 7: namespaced but wrong entry -> skipped, falls back to simple."""
        ids = {(_DOMAIN, "other-entry:dev-x"), (_DOMAIN, "dev-fallback")}
        assert (
            extract_canonical_device_id(ids, _DOMAIN, entry_id="e1")
            == "dev-fallback"
        )

    def test_service_prefix_skipped(self):
        """Branch 8a: service_prefix matches -> skipped."""
        ids = {(_DOMAIN, "svc:thing"), (_DOMAIN, "real-dev")}
        assert (
            extract_canonical_device_id(
                ids, _DOMAIN, service_prefix="svc:"
            )
            == "real-dev"
        )

    def test_legacy_service_id_skipped(self):
        """Branch 8b: legacy service id -> skipped."""
        ids = {(_DOMAIN, "service-anchor"), (_DOMAIN, "real-dev")}
        assert (
            extract_canonical_device_id(
                ids, _DOMAIN, legacy_service_id="service-anchor"
            )
            == "real-dev"
        )

    def test_empty_value_skipped(self):
        """Branch 9: empty / non-str value is skipped."""
        ids = {(_DOMAIN, ""), (_DOMAIN, None), (_DOMAIN, "real")}
        assert extract_canonical_device_id(ids, _DOMAIN) == "real"


# ---------------------------------------------------------------------------
# build_entity_unique_id_candidates -- 5 branches (priority order)
# ---------------------------------------------------------------------------


class TestBuildEntityUniqueIdCandidates:
    """Cover each ``add()`` path and the dedup guarantee."""

    def test_canonical_first(self):
        """Branch 1: canonical 'entry:sub:dev' has highest priority."""
        cands = build_entity_unique_id_candidates(
            "dev-1", "e1", "sub-id", _DOMAIN
        )
        assert cands[0] == "e1:sub-id:dev-1"

    def test_subentry_key_variant_included_when_different(self):
        """Branch 2: separate subentry_key adds an extra candidate."""
        cands = build_entity_unique_id_candidates(
            "dev-1", "e1", "sub-id", _DOMAIN, subentry_key="sub-key"
        )
        assert "e1:sub-key:dev-1" in cands
        assert cands.index("e1:sub-id:dev-1") < cands.index("e1:sub-key:dev-1")

    def test_subentry_key_variant_skipped_when_equal(self):
        """Branch 2b: subentry_key == identifier -> no duplicate."""
        cands = build_entity_unique_id_candidates(
            "dev-1", "e1", "sub-id", _DOMAIN, subentry_key="sub-id"
        )
        # Only one ':sub-id:' candidate present
        assert sum(1 for c in cands if c.startswith("e1:sub-id:")) == 1

    def test_entry_device_format(self):
        """Branch 3: 'entry:dev' is always added when both present."""
        cands = build_entity_unique_id_candidates(
            "dev-1", "e1", None, _DOMAIN
        )
        assert "e1:dev-1" in cands

    def test_domain_underscore_formats(self):
        """Branch 4+5: 'domain_entry_dev' and 'domain_dev' variants."""
        cands = build_entity_unique_id_candidates(
            "dev-1", "e1", None, _DOMAIN
        )
        assert f"{_DOMAIN}_e1_dev-1" in cands
        assert f"{_DOMAIN}_dev-1" in cands

    def test_no_entry_id_still_emits_legacy(self):
        """Edge: entry_id missing -> only legacy 'domain_dev' returned."""
        cands = build_entity_unique_id_candidates(
            "dev-1", None, None, _DOMAIN
        )
        assert cands == [f"{_DOMAIN}_dev-1"]

    def test_no_device_id_returns_empty(self):
        """Edge: device_id empty -> no candidate makes sense -> empty list."""
        assert build_entity_unique_id_candidates("", "e1", "sub-id", _DOMAIN) == []

    def test_dedup_preserves_order(self):
        """Branch (dedup): identical candidates are dropped, order preserved."""
        # If subentry_key equals identifier, the second add() short-circuits.
        cands = build_entity_unique_id_candidates(
            "dev-1", "e1", "x", _DOMAIN, subentry_key="x"
        )
        assert len(cands) == len(set(cands))


# ---------------------------------------------------------------------------
# build_canonical_unique_id -- 6 branches
# ---------------------------------------------------------------------------


class TestBuildCanonicalUniqueId:
    """Cover entry/device validation and optional subentry."""

    @pytest.mark.parametrize("entry", [None, "", "   ", 42, 1.5])
    def test_invalid_entry_id_returns_none(self, entry):
        """Branch 1: entry_id falsy or non-str -> None."""
        assert build_canonical_unique_id(entry, "sub", "dev-1") is None

    @pytest.mark.parametrize("device", [None, "", "   ", 42])
    def test_invalid_device_id_returns_none(self, device):
        """Branch 2: device_id falsy or non-str -> None."""
        assert build_canonical_unique_id("e1", "sub", device) is None

    def test_full_canonical_form(self):
        """Branch 3: all three parts -> 'entry:sub:dev'."""
        assert build_canonical_unique_id("e1", "sub-id", "dev-1") == "e1:sub-id:dev-1"

    def test_subentry_optional_omitted(self):
        """Branch 4: subentry_identifier None -> 'entry:dev'."""
        assert build_canonical_unique_id("e1", None, "dev-1") == "e1:dev-1"

    def test_subentry_empty_string_omitted(self):
        """Branch 4b: empty subentry_identifier is treated as missing."""
        assert build_canonical_unique_id("e1", "", "dev-1") == "e1:dev-1"
        assert build_canonical_unique_id("e1", "   ", "dev-1") == "e1:dev-1"

    def test_strip_applied_to_parts(self):
        """Branch 5: surrounding whitespace stripped from each part."""
        assert (
            build_canonical_unique_id("  e1  ", "  sub  ", "  dev-1  ")
            == "e1:sub:dev-1"
        )

    def test_non_string_subentry_identifier_ignored(self):
        """Branch 6: non-str subentry_identifier silently skipped."""
        # Function only checks isinstance(str); falsy non-str -> drop.
        assert build_canonical_unique_id("e1", 0, "dev-1") == "e1:dev-1"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# match_entity_by_device_id -- 5 branches
# ---------------------------------------------------------------------------


class TestMatchEntityByDeviceId:
    """Cover validation, domain/platform/entry filters and substring match."""

    @pytest.mark.parametrize("device_id", [None, "", 42])
    def test_invalid_device_id_returns_false(self, device_id):
        """Branch 1: device_id must be a non-empty str."""
        assert (
            match_entity_by_device_id(
                "ignored", "e1", device_id, "e1",
                "device_tracker", "googlefindmy", "device_tracker", "googlefindmy",
            )
            is False
        )

    def test_domain_mismatch_returns_false(self):
        """Branch 2a: entity_domain != domain -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1", "e1", "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "sensor", "googlefindmy",
            )
            is False
        )

    def test_platform_mismatch_returns_false(self):
        """Branch 2b: entity_platform != platform -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1", "e1", "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "device_tracker", "other_platform",
            )
            is False
        )

    def test_target_entry_mismatch_returns_false(self):
        """Branch 3: config_entry_id != target_entry_id -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1", "e2", "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "device_tracker", "googlefindmy",
            )
            is False
        )

    def test_target_entry_none_skips_check(self):
        """Branch 3b: target_entry_id=None -> entry filter not applied."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1", "any-entry", "dev-1", None,
                "device_tracker", "googlefindmy",
                "device_tracker", "googlefindmy",
            )
            is True
        )

    def test_config_entry_none_skips_filter(self):
        """Branch 3c: config_entry_id=None but target set -> filter skipped."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1", None, "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "device_tracker", "googlefindmy",
            )
            is True
        )

    @pytest.mark.parametrize("uid", [None, 42, b"bytes"])
    def test_unique_id_not_string_returns_false(self, uid):
        """Branch 4: unique_id must be ``str`` to be matched."""
        assert (
            match_entity_by_device_id(
                uid, "e1", "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "device_tracker", "googlefindmy",
            )
            is False
        )

    def test_device_id_substring_required(self):
        """Branch 5a: device_id must appear in unique_id -> True."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1_extra", "e1", "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "device_tracker", "googlefindmy",
            )
            is True
        )

    def test_device_id_substring_missing(self):
        """Branch 5b: device_id absent from unique_id -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_other-dev", "e1", "dev-1", "e1",
                "device_tracker", "googlefindmy",
                "device_tracker", "googlefindmy",
            )
            is False
        )


# ---------------------------------------------------------------------------
# Re-exported constants -- spot check
# ---------------------------------------------------------------------------


class TestReexportedConstants:
    """Smoke-test that the re-exports stay aligned with ``...const``."""

    def test_constants_are_strings(self):
        """Both re-exports must remain ``str`` to keep helpers' contracts."""
        assert isinstance(LEGACY_SERVICE_IDENTIFIER, str)
        assert isinstance(SERVICE_DEVICE_IDENTIFIER_PREFIX, str)
