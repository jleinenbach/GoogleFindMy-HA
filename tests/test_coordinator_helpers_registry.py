# tests/test_coordinator_helpers_registry.py
"""Branch-Coverage tests for ``coordinator.helpers.registry``.

The 19 public helper functions in
:mod:`custom_components.googlefindmy.coordinator.helpers.registry` hold no
state: no I/O, no module-level globals beyond re-exported constants.  Eighteen
of them are pure; ``resolve_device_by_identifiers`` is the exception and takes a
live device registry, because the lookup it replaces is the one thing that must
not be rebuilt per call site.  These tests exercise every documented branch via
the
Aniche-style Specification -> Boundary -> Structural progression
(PLAN_GFM_TEST_EXPANSION_SPRINT.md AP-1.1a).

Scope note (Core 2026.8 device registry change): these tests pin the behaviour
of *this translator*, not the Core API. Its renaming branch
(``add_config_entry_id`` -> ``config_entry_id``) serves registry doubles rather
than any supported core: tag 2025.9.1, the declared minimum, already ships the
``add_*`` spelling. Its ownership branch, which emits the ``add_*``/``remove_*``
quadruple, is live on that minimum and is what keeps it alive.

Since Core 2026.8 a device belongs to exactly one config entry and subentry;
the four ownership kwargs are deprecated there and stop working in 2027.8.
Nothing here should be read as "this is how to talk to a current core" -- new
production code expresses an intent instead, see AGENTS.md, section "Device
registry ownership".

Coverage scope (notes-sidecar ``helpers/registry.py``): the **24 functions**
listed in :data:`__all__` (the four dataclasses/enums and the six data names
there are data, not branches, and are covered through the functions that read
them).  The count is measured, not remembered::

    python3 -c "import ast,pathlib; t=ast.parse(pathlib.Path(
    'custom_components/googlefindmy/coordinator/helpers/registry.py'
    ).read_text()); a=[ast.literal_eval(n.value) for n in ast.walk(t)
    if isinstance(n,(ast.Assign,ast.AnnAssign)) and getattr(
    getattr(n,'target',None) or n.targets[0],'id','')=='__all__'][0];
    f={n.name for n in t.body if isinstance(n,ast.FunctionDef)};
    print(len([x for x in a if x in f]))"

This used to read "~75 branches across the 19 functions". The function count was
stale (23 before this work package, 24 with ``iter_all_devices``), and the branch
figure was never re-derived after any of them, so the heading now names the scope
it can state and the command that measures it. A branch budget would need the
same treatment: a number nobody re-measures is not a budget. Note that the
command counts ``ast.FunctionDef`` only; an ``async def`` added to
:data:`__all__` would be undercounted.  Each test docstring names the function
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

from custom_components.googlefindmy.coordinator.helpers import (
    registry as registry_helpers,
)
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
        device = SimpleNamespace(config_entries=["e1"], config_subentry_id=None)
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
        assert is_hub_device_check("dev-1", "dev-1", set(), self._PARENT) is True

    def test_identifier_match_in_set(self):
        """Branch 2: parent_identifier in identifiers -> True."""
        assert (
            is_hub_device_check("dev-x", "hub-y", {self._PARENT}, self._PARENT) is True
        )

    def test_no_match_returns_false(self):
        """Branch 3: neither ID nor identifier match -> False."""
        assert (
            is_hub_device_check("dev-x", "hub-y", {(_DOMAIN, "other")}, self._PARENT)
            is False
        )

    @pytest.mark.parametrize(
        "identifiers", [None, "string-not-collection", b"bytes", {"k": "v"}]
    )
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
        assert resolve_tracker_subentry_candidate("other", "etrk", {"etrk"}) is None

    def test_entry_id_match_but_not_in_set(self):
        """Branch 2c: matches entry_tracker_id but tracker set rejects it -> None."""
        assert resolve_tracker_subentry_candidate("etrk", "etrk", {"other"}) is None

    def test_entry_id_match_empty_set_allowed(self):
        """Branch 2d: entry_tracker_id matches and set is empty -> candidate."""
        assert resolve_tracker_subentry_candidate("etrk", "etrk", set()) == "etrk"

    def test_no_entry_id_candidate_in_set(self):
        """Branch 3a: no entry_tracker_id, candidate in set -> candidate."""
        assert resolve_tracker_subentry_candidate("c", None, {"c", "d"}) == "c"

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
        self.data: Any = (
            None if no_data else {"group_key": group_key} if group_key else {}
        )


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
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {
            "ok-1"
        }

    def test_provisional_skipped_unless_matches(self):
        """Branch 3a: '*-provisional' is skipped unless it equals current service id."""
        entries = {
            "abc-provisional": _SubentryStub(subentry_type=_SVC_TYPE),
            "active-1": _SubentryStub(subentry_type=_SVC_TYPE),
        }
        # No entry_service_subentry_id -> provisional dropped
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {
            "active-1"
        }

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
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {
            "sub-1"
        }

    def test_match_by_group_key(self):
        """Branch 5: data.group_key == service_subentry_key -> include."""
        entries = {"sub-2": _SubentryStub(group_key=_SVC_KEY)}
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {
            "sub-2"
        }

    def test_non_matching_subentry_ignored(self):
        """Branch 6: neither type nor group_key matches -> excluded."""
        entries = {"sub-3": _SubentryStub(subentry_type="other", group_key="other")}
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == set()

    def test_subentry_without_data_attr(self):
        """Edge: subentry with no usable data mapping -> group_key falls back."""
        entries = {"sub-4": _SubentryStub(subentry_type=_SVC_TYPE, no_data=True)}
        # type matches -> still included
        assert extract_service_subentry_ids(entries, None, _SVC_TYPE, _SVC_KEY) == {
            "sub-4"
        }


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
        assert extract_canonical_device_id(ids, _DOMAIN, entry_id="e1") == "dev-ns"

    def test_namespaced_mismatch_skipped(self):
        """Branch 7: namespaced but wrong entry -> skipped, falls back to simple."""
        ids = {(_DOMAIN, "other-entry:dev-x"), (_DOMAIN, "dev-fallback")}
        assert (
            extract_canonical_device_id(ids, _DOMAIN, entry_id="e1") == "dev-fallback"
        )

    def test_service_prefix_skipped(self):
        """Branch 8a: service_prefix matches -> skipped."""
        ids = {(_DOMAIN, "svc:thing"), (_DOMAIN, "real-dev")}
        assert (
            extract_canonical_device_id(ids, _DOMAIN, service_prefix="svc:")
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
        cands = build_entity_unique_id_candidates("dev-1", "e1", "sub-id", _DOMAIN)
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
        cands = build_entity_unique_id_candidates("dev-1", "e1", None, _DOMAIN)
        assert "e1:dev-1" in cands

    def test_domain_underscore_formats(self):
        """Branch 4+5: 'domain_entry_dev' and 'domain_dev' variants."""
        cands = build_entity_unique_id_candidates("dev-1", "e1", None, _DOMAIN)
        assert f"{_DOMAIN}_e1_dev-1" in cands
        assert f"{_DOMAIN}_dev-1" in cands

    def test_no_entry_id_still_emits_legacy(self):
        """Edge: entry_id missing -> only legacy 'domain_dev' returned."""
        cands = build_entity_unique_id_candidates("dev-1", None, None, _DOMAIN)
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
                "ignored",
                "e1",
                device_id,
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
            )
            is False
        )

    def test_domain_mismatch_returns_false(self):
        """Branch 2a: entity_domain != domain -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1",
                "e1",
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "sensor",
                "googlefindmy",
            )
            is False
        )

    def test_platform_mismatch_returns_false(self):
        """Branch 2b: entity_platform != platform -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1",
                "e1",
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "other_platform",
            )
            is False
        )

    def test_target_entry_mismatch_returns_false(self):
        """Branch 3: config_entry_id != target_entry_id -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1",
                "e2",
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
            )
            is False
        )

    def test_target_entry_none_skips_check(self):
        """Branch 3b: target_entry_id=None -> entry filter not applied."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1",
                "any-entry",
                "dev-1",
                None,
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
            )
            is True
        )

    def test_config_entry_none_skips_filter(self):
        """Branch 3c: config_entry_id=None but target set -> filter skipped."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1",
                None,
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
            )
            is True
        )

    @pytest.mark.parametrize("uid", [None, 42, b"bytes"])
    def test_unique_id_not_string_returns_false(self, uid):
        """Branch 4: unique_id must be ``str`` to be matched."""
        assert (
            match_entity_by_device_id(
                uid,
                "e1",
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
            )
            is False
        )

    def test_device_id_substring_required(self):
        """Branch 5a: device_id must appear in unique_id -> True."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_dev-1_extra",
                "e1",
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
            )
            is True
        )

    def test_device_id_substring_missing(self):
        """Branch 5b: device_id absent from unique_id -> False."""
        assert (
            match_entity_by_device_id(
                "googlefindmy_e1_other-dev",
                "e1",
                "dev-1",
                "e1",
                "device_tracker",
                "googlefindmy",
                "device_tracker",
                "googlefindmy",
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


# ---------------------------------------------------------------------------
# Core 2026.8 single-owner migration (AP-09).
#
# These classes describe helpers that AP-10 introduces.  Until then the symbols
# do not exist, so the classes skip: every commit has to be green on its own,
# which rules out landing a red test and fixing it two commits later.  A plain
# ``pytest.importorskip`` is not enough here -- the module already exists, only
# the names inside it are missing -- so the guard checks the attributes.
# ---------------------------------------------------------------------------

_NEW_SYMBOLS = (
    "DeviceRegistryCapabilities",
    "detect_device_registry_capabilities",
    "OwnershipIntent",
    "DeviceOwnership",
    "plan_device_ownership",
)

_missing_symbols = [
    name for name in _NEW_SYMBOLS if not hasattr(registry_helpers, name)
]
_needs_ap10 = pytest.mark.skipif(
    bool(_missing_symbols),
    reason=f"AP-10 has not landed yet; missing: {', '.join(_missing_symbols)}",
)


def _profile(**parameters: object) -> object:
    """Build a callable whose signature carries exactly ``parameters``."""
    names = ", ".join(f"{name}=None" for name in parameters)
    namespace: dict[str, object] = {}
    exec(f"def _call(device_id, *, {names}):\n    return None\n", namespace)  # noqa: S102
    return namespace["_call"]


@_needs_ap10
class TestDetectDeviceRegistryCapabilities:
    """The capability profile is read from the signature, never from a version.

    Version strings lie: a fork, a backport or a patched core can carry any
    number.  The signature is the thing the call actually has to satisfy.
    """

    def test_core_2025_9_profile(self) -> None:
        """The declared minimum: add_* present, new_* absent."""
        caps = registry_helpers.detect_device_registry_capabilities(
            _profile(
                add_config_entry_id=None,
                add_config_subentry_id=None,
                remove_config_entry_id=None,
                remove_config_subentry_id=None,
            )
        )
        assert caps.single_owner_model is False
        assert caps.subentry_kwarg_for_update == "add_config_subentry_id"

    def test_core_2025_11_profile(self) -> None:
        """Same shape as 2025.9 for our purposes; kept as a separate pin.

        The repository long believed the rename happened in 2025.11.  It did
        not -- 2025.9.1 already ships ``add_config_subentry_id``.  This case
        exists so that belief cannot quietly return: both profiles must yield
        the same answer.
        """
        caps = registry_helpers.detect_device_registry_capabilities(
            _profile(add_config_entry_id=None, add_config_subentry_id=None)
        )
        assert caps.single_owner_model is False
        assert caps.subentry_kwarg_for_update == "add_config_subentry_id"

    def test_core_2026_8_profile(self) -> None:
        """From 2026.8 the new_* keywords exist and the model is single-owner."""
        caps = registry_helpers.detect_device_registry_capabilities(
            _profile(
                new_config_entry_id=None,
                new_config_subentry_id=None,
                add_config_entry_id=None,
                add_config_subentry_id=None,
                remove_config_entry_id=None,
                remove_config_subentry_id=None,
            )
        )
        assert caps.single_owner_model is True
        assert caps.subentry_kwarg_for_update == "new_config_subentry_id"

    def test_var_keyword_fallback(self) -> None:
        """A ``**kwargs`` signature accepts anything, so assume the legacy name.

        Test doubles are the realistic case here.  Guessing "single owner"
        instead would send ``new_config_subentry_id`` at a double that silently
        swallows it, and the test would pass while production broke.
        """
        namespace: dict[str, object] = {}
        exec("def _call(device_id, **kwargs):\n    return None\n", namespace)  # noqa: S102
        caps = registry_helpers.detect_device_registry_capabilities(namespace["_call"])
        assert caps.accepts_var_keyword is True
        assert caps.single_owner_model is False
        assert caps.subentry_kwarg_for_update == "config_subentry_id"
        assert caps.subentry_kwarg_for_shim == "config_subentry_id"

    def test_a_half_new_signature_is_not_single_owner(self) -> None:
        """Both new keywords or neither: a partial double is not a core.

        The two switches must agree.  If ``subentry_kwarg_for_update`` answered
        ``new_config_subentry_id`` here while ``single_owner_model`` said False,
        the legacy ENSURE branch would emit ``add_config_entry_id`` next to
        ``new_config_subentry_id`` -- the combination Core 2026.8+ rejects.
        """
        caps = registry_helpers.detect_device_registry_capabilities(
            _profile(
                new_config_subentry_id=None,
                add_config_entry_id=None,
                add_config_subentry_id=None,
            )
        )
        assert caps.single_owner_model is False
        assert caps.subentry_kwarg_for_update == "add_config_subentry_id"

    def test_a_signature_without_any_subentry_keyword_yields_none(self) -> None:
        """No spelling at all means no keyword, not a guessed one."""
        caps = registry_helpers.detect_device_registry_capabilities(
            _profile(add_config_entry_id=None, remove_config_entry_id=None)
        )
        assert caps.subentry_kwarg_for_update is None

    def test_unreadable_signature_degrades_to_empty_profile(self) -> None:
        """A callable without an introspectable signature must not crash."""
        caps = registry_helpers.detect_device_registry_capabilities(object())
        assert caps.single_owner_model is False


@_needs_ap10
class TestPlanDeviceOwnership:
    """Intent x capability profile, with the deletion risk pinned explicitly."""

    @staticmethod
    def _modern() -> object:
        return registry_helpers.detect_device_registry_capabilities(
            _profile(
                new_config_entry_id=None,
                new_config_subentry_id=None,
                add_config_entry_id=None,
                add_config_subentry_id=None,
                remove_config_entry_id=None,
                remove_config_subentry_id=None,
            )
        )

    @staticmethod
    def _legacy() -> object:
        return registry_helpers.detect_device_registry_capabilities(
            _profile(
                add_config_entry_id=None,
                add_config_subentry_id=None,
                remove_config_entry_id=None,
                remove_config_subentry_id=None,
            )
        )

    def test_ensure_on_correct_owner_is_an_empty_plan(self) -> None:
        """Already owned means no operation at all, not a harmless no-op call.

        An empty tuple is observably different from an update that changes
        nothing: it produces no deprecation report and no registry event.
        """
        plan = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.ENSURE,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
            current=registry_helpers.DeviceOwnership("entry-1", "sub-1"),
        )
        assert plan == ()

    def test_ensure_on_correct_owner_still_writes_extra_metadata(self) -> None:
        """Right ownership silences the ownership keywords, not the metadata.

        The call sites bundle metadata with the ownership intent: name,
        identifiers, manufacturer, ``via_device_id=None``. An empty plan would
        drop all of it whenever the device happened to sit correctly, and the
        caller cannot distinguish that from "nothing to do".
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.ENSURE,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
            current=registry_helpers.DeviceOwnership("entry-1", "sub-1"),
            extra={"name": "Tracker", "via_device_id": None},
        )
        assert operation.method == "async_update_device"
        assert operation.kwargs == {
            "device_id": "dev-1",
            "name": "Tracker",
            "via_device_id": None,
        }

    def test_ensure_on_modern_core_uses_new_keywords(self) -> None:
        """A device owned elsewhere is moved with the new keywords."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.ENSURE,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
            current=registry_helpers.DeviceOwnership("entry-other", None),
        )
        assert operation.method == "async_update_device"
        assert operation.kwargs["new_config_entry_id"] == "entry-1"
        assert operation.kwargs["new_config_subentry_id"] == "sub-1"

    def test_ensure_on_legacy_core_uses_add_keywords(self) -> None:
        """Below 2026.8 the same intent still speaks the add_* dialect."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.ENSURE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
        )
        assert operation.kwargs["add_config_entry_id"] == "entry-1"
        assert operation.kwargs["add_config_subentry_id"] == "sub-1"

    def test_ensure_without_target_on_a_known_foreign_owner_takes_the_root(
        self,
    ) -> None:
        """With a known current owner the entry root is a decision, not a guess.

        Core resets the subentry alongside the new entry, so this is what
        "we own it, no subentry named" means on a single-owner core.  It is only
        allowed because ``current`` is known; without it the planner refuses.
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.ENSURE,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            current=registry_helpers.DeviceOwnership("entry-other", "sub-x"),
        )
        assert operation.kwargs["new_config_entry_id"] == "entry-1"
        assert "new_config_subentry_id" not in operation.kwargs

    def test_ensure_without_target_on_a_legacy_core_only_adds_the_entry(
        self,
    ) -> None:
        """Below 2026.8 an add without a subentry is a plain hub link."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.ENSURE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
        )
        assert operation.kwargs["add_config_entry_id"] == "entry-1"
        assert "add_config_subentry_id" not in operation.kwargs

    def test_legacy_move_on_a_registry_without_subentries_omits_the_keyword(
        self,
    ) -> None:
        """A core that knows no subentry keyword gets none invented for it."""
        caps = registry_helpers.detect_device_registry_capabilities(
            _profile(add_config_entry_id=None, remove_config_entry_id=None)
        )
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=caps,
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-2",
            detach_subentry_id="sub-1",
        )
        assert set(operation.kwargs) == {
            "device_id",
            "remove_config_entry_id",
            "remove_config_subentry_id",
            "add_config_entry_id",
        }
        assert operation.kwargs["remove_config_subentry_id"] == "sub-1"

    def test_detach_on_matching_link_removes_the_device(self) -> None:
        """On a single-owner core, dropping the only link deletes the device.

        This is not a design choice, it is what core does; the plan just makes
        it visible at the call site instead of hiding it behind a kwarg.
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.DETACH,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            detach_subentry_id=None,
            current=registry_helpers.DeviceOwnership("entry-1", None),
        )
        assert operation.method == "async_remove_device"
        assert operation.kwargs == {"device_id": "dev-1"}

    def test_detach_on_mismatched_subentry_is_empty(self) -> None:
        """The device sits in a subentry, the caller drops the hub link: no-op.

        Measured against core in
        ``tests/test_device_registry_single_owner_contract.py``: core does
        nothing here.  Turning it into a deletion would destroy a device and all
        its entities -- the single most expensive mistake this migration can
        make, and the reason DETACH compares both levels.
        """
        plan = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.DETACH,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            detach_subentry_id=None,
            current=registry_helpers.DeviceOwnership("entry-1", "sub-1"),
        )
        assert plan == ()

    def test_detach_without_known_state_refuses(self) -> None:
        """Unknown ownership must raise, never fall back to deleting."""
        with pytest.raises(ValueError, match="current ownership"):
            registry_helpers.plan_device_ownership(
                registry_helpers.OwnershipIntent.DETACH,
                caps=self._modern(),
                device_id="dev-1",
                entry_id="entry-1",
                current=None,
            )

    def test_detach_on_legacy_core_uses_remove_keywords(self) -> None:
        """Below 2026.8 detaching stays a kwargs update and deletes nothing."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.DETACH,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            detach_subentry_id="sub-1",
        )
        assert operation.method == "async_update_device"
        assert operation.kwargs["remove_config_entry_id"] == "entry-1"
        assert operation.kwargs["remove_config_subentry_id"] == "sub-1"

    def test_move_on_modern_core_sends_both_new_keywords(self) -> None:
        """Both keywords together: the subentry is resolved against the target.

        Sending ``new_config_subentry_id`` alone makes core resolve it against
        the *old* owning entry and raise when the device still belongs there.
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._modern(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-2",
        )
        assert operation.kwargs["new_config_entry_id"] == "entry-1"
        assert operation.kwargs["new_config_subentry_id"] == "sub-2"

    def test_move_on_legacy_core_drops_the_named_link_only(self) -> None:
        """The legacy move removes the link it was told to, not the current one.

        ``_heal_tracker_device_subentry`` drops one specific surplus link per
        iteration; a plan that always removed ``current.subentry_id`` would make
        that healing loop spin without effect on the declared minimum.
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-2",
            detach_subentry_id="sub-surplus",
            current=registry_helpers.DeviceOwnership(None, "sub-1"),
        )
        assert operation.kwargs["remove_config_subentry_id"] == "sub-surplus"
        assert operation.kwargs["add_config_entry_id"] == "entry-1"
        assert operation.kwargs["add_config_subentry_id"] == "sub-2"


# ---------------------------------------------------------------------------
# resolve_device_by_identifiers -- the single device lookup point (AP-14)
# ---------------------------------------------------------------------------


class _LegacyOnlyRegistry:
    """A pre-2026.8 registry: identifier sets, no entry scoping.

    Mirrors the shape the integration met up to Core 2026.7 and still meets on
    the declared minimum ``2025.9.1``: one ``async_get_device`` taking a set,
    with no notion of an owning entry.
    """

    def __init__(self, devices: dict[tuple[str, str], Any]) -> None:
        self._devices = devices
        self.calls: list[set[tuple[str, str]]] = []

    def async_get_device(self, identifiers: set[tuple[str, str]]) -> Any | None:
        self.calls.append(set(identifiers))
        # Sorted, not set order: the real ``get_entry`` (tag ``2025.9.1``, lines
        # 684-686) returns the first identifier that matches in *set* iteration
        # order, which for the identifiers used below depends on the hash seed.
        # Sorting puts the unscoped ``dev-1`` before ``entry-1:dev-1`` every
        # time, so a lookup that hands over both identifiers at once meets the
        # adverse order deterministically instead of on one run in two.
        for identifier in sorted(identifiers):
            if (device := self._devices.get(identifier)) is not None:
                return device
        return None


class _BothApisRegistry(_LegacyOnlyRegistry):
    """Carries both lookup APIs at once, which no real core does.

    Deliberately unrealistic, and the only shape that can observe the "no
    fallback after a modern miss" rule: against a registry that has the modern
    API alone, dropping the guard is invisible because the legacy branch finds
    no callable to call.
    """

    def __init__(
        self, devices: dict[tuple[str, str], Any], *, owner: str = "entry-1"
    ) -> None:
        super().__init__(devices)
        self._owner = owner
        self.scoped_calls: list[tuple[tuple[str, str], str]] = []

    def async_get_device_by_identifier(
        self, identifier: tuple[str, str], config_entry_id: str
    ) -> Any | None:
        self.scoped_calls.append((identifier, config_entry_id))
        if config_entry_id != self._owner:
            return None
        return self._devices.get(identifier)


class TestDetachSubentryIdIsNotTheSameAsOmittingIt:
    """``None`` names the hub link; leaving the argument out names nothing.

    Both used to collapse into one value, and the collapse was not academic: on
    a pre-2026.8 core a device holds a *set* of links, so it can sit in the
    tracker subentry and on the entry root at the same time. That is precisely
    when the hub link has to go, and precisely when reading the current subentry
    instead of the named ``None`` cancels the removal out.
    """

    @staticmethod
    def _legacy() -> Any:
        return registry_helpers.detect_device_registry_capabilities(
            _profile(
                add_config_entry_id=None,
                add_config_subentry_id=None,
                remove_config_entry_id=None,
                remove_config_subentry_id=None,
            )
        )

    def test_explicit_none_removes_the_hub_link(self) -> None:
        """The device already sits in the target and must still lose the root."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
            detach_subentry_id=None,
            current=registry_helpers.DeviceOwnership("entry-1", "sub-1"),
        )
        assert operation.kwargs["remove_config_entry_id"] == "entry-1"
        assert operation.kwargs["remove_config_subentry_id"] is None
        assert operation.kwargs["add_config_subentry_id"] == "sub-1"

    def test_omitting_it_reads_the_current_subentry(self) -> None:
        """Without a named link the planner falls back to what it can see."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-2",
            current=registry_helpers.DeviceOwnership("entry-1", "sub-1"),
        )
        assert operation.kwargs["remove_config_subentry_id"] == "sub-1"

    def test_omitting_it_on_a_device_already_in_place_removes_nothing(self) -> None:
        """The fallback must not cancel the move it is supposed to complete."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
            current=registry_helpers.DeviceOwnership("entry-1", "sub-1"),
        )
        assert "remove_config_entry_id" not in operation.kwargs

    def test_named_hub_link_is_given_up_even_without_a_known_owner(self) -> None:
        """The combination the declared minimum core really produces.

        ``_remove_hub_link`` and ``_detach_service_hub_link`` name the link
        (``None``) and hand over a device entry that, at tag ``2025.9.1``,
        describes no ownership at all. ``current`` is therefore ``None`` while a
        link *is* named, and the removal must still go out. Every other case in
        this class either supplies ``current`` or names nothing, so a regression
        that folded the two conditions into one would slip past them.
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
            detach_subentry_id=None,
        )
        assert operation.kwargs["remove_config_entry_id"] == "entry-1"
        assert operation.kwargs["remove_config_subentry_id"] is None

    def test_unknown_ownership_and_no_named_link_removes_nothing(self) -> None:
        """The declared minimum core lands here, and it must not be guessed at.

        At tag ``2025.9.1`` a device entry has no ``config_entry_id`` and no
        ``config_subentry_id``, so ``current`` is ``None`` for every caller that
        does not name a link. Falling back to ``None`` would give up the hub
        link, which is a different link from the one the caller has in mind and,
        when it is the only one, the device itself. Only the add half goes out,
        which is what the call sites did before they spoke intents.
        """
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.MOVE,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
            target_subentry_id="sub-1",
        )
        assert "remove_config_entry_id" not in operation.kwargs
        assert "remove_config_subentry_id" not in operation.kwargs
        assert operation.kwargs["add_config_entry_id"] == "entry-1"
        assert operation.kwargs["add_config_subentry_id"] == "sub-1"

    def test_detach_still_treats_an_omitted_link_as_the_hub_link(self) -> None:
        """DETACH names a link by definition; omitting it keeps the old meaning."""
        (operation,) = registry_helpers.plan_device_ownership(
            registry_helpers.OwnershipIntent.DETACH,
            caps=self._legacy(),
            device_id="dev-1",
            entry_id="entry-1",
        )
        assert operation.kwargs["remove_config_subentry_id"] is None


class TestDeviceBelongsToEntry:
    """One question, two core generations, and an unknown state that says no."""

    def test_modern_entry_matches(self) -> None:
        """From 2026.8 the single owner is named in ``config_entry_id``."""
        assert registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id="entry-1"), "entry-1"
        )

    def test_modern_entry_mismatches(self) -> None:
        """A device owned by someone else is not ours."""
        assert not registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id="entry-other"), "entry-1"
        )

    def test_legacy_set_matches(self) -> None:
        """Below 2026.8 ownership is a set, and membership is the question."""
        assert registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id=None, config_entries={"entry-1", "x"}),
            "entry-1",
        )

    def test_legacy_set_mismatches(self) -> None:
        """A set that does not contain us answers no."""
        assert not registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id=None, config_entries={"x"}), "entry-1"
        )

    def test_unknown_state_is_not_ownership(self) -> None:
        """A device entry that describes nothing is not evidence of ownership."""
        assert not registry_helpers.device_belongs_to_entry(
            SimpleNamespace(), "entry-1"
        )

    def test_a_string_is_not_a_set_of_owners(self) -> None:
        """``config_entries`` holding a string must not match character-wise.

        A string is iterable, so without the explicit exclusion the membership
        test walks its characters. The entry id here is one character long,
        which is what makes the difference observable at all: with a longer id
        the character walk happens to find nothing and the bug hides.
        """
        assert not registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id=None, config_entries="x"), "x"
        )

    @pytest.mark.parametrize("entry_id", [None, ""])
    def test_no_entry_id_answers_no(self, entry_id: str | None) -> None:
        """Without an entry to test for there is nothing to confirm."""
        assert not registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id="entry-1"), entry_id
        )

    def test_none_device_answers_no(self) -> None:
        """A missing device owns nothing."""
        assert not registry_helpers.device_belongs_to_entry(None, "entry-1")

    def test_empty_modern_owner_falls_through_to_the_shim(self) -> None:
        """An empty ``config_entry_id`` names nobody and must not shadow the shim.

        The twin assertion to
        ``TestDeviceOwningEntryIds::test_empty_modern_owner_falls_through_to_the_shim``.
        The two accessors are documented as a pair, so they have to read the same
        shape the same way.
        """
        assert registry_helpers.device_belongs_to_entry(
            SimpleNamespace(config_entry_id="", config_entries={"entry-1"}), "entry-1"
        )


class TestDeviceOwningEntryIds:
    """The set-returning counterpart, same two core generations."""

    def test_modern_entry_is_the_single_owner(self) -> None:
        """From 2026.8 there is exactly one owner, so the tuple holds one id."""
        assert registry_helpers.device_owning_entry_ids(
            SimpleNamespace(config_entry_id="entry-1")
        ) == ("entry-1",)

    def test_modern_entry_wins_over_the_shim(self) -> None:
        """``config_entry_id`` is preferred even when the shim disagrees.

        The shim can name several entries on a device split from a
        pre-migration composite; the single owner is the answer we want.
        """
        assert registry_helpers.device_owning_entry_ids(
            SimpleNamespace(
                config_entry_id="entry-1", config_entries={"entry-1", "entry-2"}
            )
        ) == ("entry-1",)

    def test_legacy_set_yields_every_owner(self) -> None:
        """Below 2026.8 a device can belong to several entries at once."""
        assert set(
            registry_helpers.device_owning_entry_ids(
                SimpleNamespace(config_entry_id=None, config_entries={"a", "b"})
            )
        ) == {"a", "b"}

    def test_unknown_state_yields_nothing(self) -> None:
        """A device entry that describes no ownership answers with an empty tuple."""
        assert registry_helpers.device_owning_entry_ids(SimpleNamespace()) == ()

    def test_none_device_yields_nothing(self) -> None:
        """A missing device owns nothing."""
        assert registry_helpers.device_owning_entry_ids(None) == ()

    def test_a_string_is_not_a_set_of_owners(self) -> None:
        """``config_entries`` holding a string must not be walked character-wise.

        Without the explicit exclusion a two-character id would come back as two
        one-character "entries", which every caller would then treat as owners.
        """
        assert (
            registry_helpers.device_owning_entry_ids(
                SimpleNamespace(config_entry_id=None, config_entries="ab")
            )
            == ()
        )

    def test_non_string_members_are_dropped(self) -> None:
        """Only string ids are owners; anything else cannot name a config entry."""
        assert registry_helpers.device_owning_entry_ids(
            SimpleNamespace(config_entry_id=None, config_entries=["a", None, 7])
        ) == ("a",)

    def test_empty_modern_owner_falls_through_to_the_shim(self) -> None:
        """An empty ``config_entry_id`` names nobody and must not shadow the shim."""
        assert registry_helpers.device_owning_entry_ids(
            SimpleNamespace(config_entry_id="", config_entries={"entry-1"})
        ) == ("entry-1",)


class TestReadDeviceOwnership:
    """Unknown ownership and "owned by nobody" must stay distinguishable."""

    def test_no_device_is_unknown(self) -> None:
        """A caller that resolved nothing knows nothing."""
        assert registry_helpers.read_device_ownership(None) is None

    def test_an_entry_without_either_field_is_unknown(self) -> None:
        """The declared minimum core's shape: neither field, so no answer.

        Returning an empty ``DeviceOwnership`` here would read as "owned by
        nobody" and let a DETACH proceed on a guess.
        """
        assert registry_helpers.read_device_ownership(SimpleNamespace(id="d")) is None

    def test_a_modern_entry_answers_both_axes(self) -> None:
        """Core 2026.8 carries both fields."""
        ownership = registry_helpers.read_device_ownership(
            SimpleNamespace(config_entry_id="entry-1", config_subentry_id="sub-1")
        )
        assert ownership == registry_helpers.DeviceOwnership("entry-1", "sub-1")

    def test_a_modern_entry_on_the_hub_link_answers_none_for_the_subentry(
        self,
    ) -> None:
        """Sitting directly on the entry is a known state, not an unknown one."""
        ownership = registry_helpers.read_device_ownership(
            SimpleNamespace(config_entry_id="entry-1", config_subentry_id=None)
        )
        assert ownership == registry_helpers.DeviceOwnership("entry-1", None)

    def test_only_the_subentry_field_yields_an_owner_of_none(self) -> None:
        """First half-known shape: no owner, so no entry matches it."""
        assert registry_helpers.read_device_ownership(
            SimpleNamespace(config_subentry_id="sub-1")
        ) == registry_helpers.DeviceOwnership(None, "sub-1")

    def test_only_the_entry_field_yields_the_hub_link(self) -> None:
        """Second half-known shape, and it is not the mirror of the first.

        The absent subentry reads as ``None``, which *is* the hub link, so a
        DETACH planned from this state answers with the removal. That asymmetry
        is the reason this function documents both shapes instead of calling
        them equivalent.
        """
        assert registry_helpers.read_device_ownership(
            SimpleNamespace(config_entry_id="entry-1")
        ) == registry_helpers.DeviceOwnership("entry-1", None)


class _RecordingUpdateRegistry:
    """Modern registry double that records what it was asked to do."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.removed: list[str] = []

    def async_update_device(
        self,
        device_id: str,
        *,
        new_config_entry_id: Any = None,
        new_config_subentry_id: Any = None,
        name: Any = None,
    ) -> str:
        self.updates.append(
            {
                "device_id": device_id,
                "new_config_entry_id": new_config_entry_id,
                "new_config_subentry_id": new_config_subentry_id,
                "name": name,
            }
        )
        return f"updated-{device_id}"

    def async_remove_device(self, device_id: str) -> None:
        self.removed.append(device_id)


class _LegacyRejectingRegistry:
    """Pre-2026.8 registry that refuses ``remove_config_subentry_id``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def async_update_device(self, device_id: str, **changes: Any) -> str:
        self.calls.append({"device_id": device_id, **changes})
        if "remove_config_subentry_id" in changes:
            raise TypeError("unexpected keyword argument 'remove_config_subentry_id'")
        return f"updated-{device_id}"


class TestExecuteOwnershipPlan:
    """The executor for call sites without a coordinator.

    These tests assert keywords, which ``tests/AGENTS.md`` otherwise forbids
    ("assert the resulting ownership, never the keyword"). The rule is aimed at
    tests of *call sites*, where a keyword assertion pins a superseded API and
    keeps it alive. This function is not a call site: it chooses no keyword, it
    forwards what the planner already chose, and it owns no device whose
    resulting ownership could be asserted instead. What is under test here is the
    forwarding and its failure modes, and the keywords are the only observable
    it has. The ownership outcomes are asserted where they arise, against
    ``SingleOwnerDeviceRegistry`` in :class:`TestPlanDeviceOwnership` and in the
    service tests.
    """

    def test_update_operation_is_forwarded_verbatim(self) -> None:
        """Every keyword the planner chose reaches the registry unchanged."""
        registry = _RecordingUpdateRegistry()
        plan = (
            registry_helpers.DeviceRegistryOperation(
                "async_update_device",
                {
                    "device_id": "dev-1",
                    "new_config_entry_id": "entry-1",
                    "new_config_subentry_id": "sub-1",
                },
            ),
        )
        assert (
            registry_helpers.execute_ownership_plan(registry, plan) == "updated-dev-1"
        )
        assert registry.updates == [
            {
                "device_id": "dev-1",
                "new_config_entry_id": "entry-1",
                "new_config_subentry_id": "sub-1",
                "name": None,
            }
        ]

    def test_removal_operation_returns_none(self) -> None:
        """After a removal there is no device left to return."""
        registry = _RecordingUpdateRegistry()
        plan = (
            registry_helpers.DeviceRegistryOperation(
                "async_remove_device", {"device_id": "dev-1"}
            ),
        )
        assert registry_helpers.execute_ownership_plan(registry, plan) is None
        assert registry.removed == ["dev-1"]

    def test_empty_plan_touches_nothing(self) -> None:
        """An empty plan is "nothing to do", not "do something harmless"."""
        registry = _RecordingUpdateRegistry()
        assert registry_helpers.execute_ownership_plan(registry, ()) is None
        assert registry.updates == []
        assert registry.removed == []

    def test_legacy_signature_triggers_the_shared_retry(self) -> None:
        """A core that rejects the new keyword gets the translated call.

        This is the retry that used to be hand-written in ``services.py``. It
        serves no core at or above the declared minimum ``2025.9.1``, which
        already accepts ``remove_config_subentry_id``; the branch exists for
        signatures that do not, such as this double.
        """
        registry = _LegacyRejectingRegistry()
        plan = (
            registry_helpers.DeviceRegistryOperation(
                "async_update_device",
                {
                    "device_id": "dev-1",
                    "remove_config_entry_id": "entry-1",
                    "remove_config_subentry_id": None,
                },
            ),
        )
        assert (
            registry_helpers.execute_ownership_plan(registry, plan) == "updated-dev-1"
        )
        assert registry.calls == [
            {
                "device_id": "dev-1",
                "remove_config_entry_id": "entry-1",
                "remove_config_subentry_id": None,
            },
            {"device_id": "dev-1", "remove_config_entry_id": "entry-1"},
        ]

    def test_an_unrelated_type_error_is_not_swallowed(self) -> None:
        """Only the legacy-keyword TypeError is retried; the rest surfaces."""

        class _Broken:
            def async_update_device(self, device_id: str, **changes: Any) -> None:
                raise TypeError("something else entirely")

        plan = (
            registry_helpers.DeviceRegistryOperation(
                "async_update_device",
                {"device_id": "dev-1", "new_config_entry_id": "entry-1"},
            ),
        )
        with pytest.raises(TypeError, match="something else entirely"):
            registry_helpers.execute_ownership_plan(_Broken(), plan)

    def test_a_registry_without_the_update_call_raises(self) -> None:
        """A silent skip would look exactly like a successful ownership change."""
        plan = (
            registry_helpers.DeviceRegistryOperation(
                "async_update_device", {"device_id": "dev-1"}
            ),
        )
        with pytest.raises(AttributeError, match="async_update_device"):
            registry_helpers.execute_ownership_plan(SimpleNamespace(), plan)

    def test_a_registry_without_the_removal_call_raises(self) -> None:
        """Same reason, and here the unnoticed skip would leave a live device."""
        plan = (
            registry_helpers.DeviceRegistryOperation(
                "async_remove_device", {"device_id": "dev-1"}
            ),
        )
        with pytest.raises(AttributeError, match="async_remove_device"):
            registry_helpers.execute_ownership_plan(SimpleNamespace(), plan)


class _DriftedSignatureRegistry(_LegacyOnlyRegistry):
    """Has the new name but refuses the new signature."""

    def async_get_device_by_identifier(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("unexpected signature")


class TestResolveDeviceByIdentifiers:
    """All three core branches, the priority, and the cross-entry narrowing."""

    _ENTRY_ID = "entry-1"
    _SCOPED = (_DOMAIN, "entry-1:dev-1")
    _UNSCOPED = (_DOMAIN, "dev-1")

    def _modern(self, registry: Any, *, entry_id: str = _ENTRY_ID) -> Any | None:
        return registry_helpers.resolve_device_by_identifiers(
            registry, (self._SCOPED, self._UNSCOPED), entry_id=entry_id
        )

    def test_scoped_identifier_wins_over_the_legacy_one(
        self, single_owner_device_registry: Any
    ) -> None:
        """Both identifiers resolve; the caller's order decides, not the registry."""
        registry = single_owner_device_registry
        registry.add_config_entry("entry-1")
        scoped = registry.add_device(
            identifiers={self._SCOPED}, config_entry_id="entry-1"
        )
        legacy = registry.add_device(
            identifiers={self._UNSCOPED}, config_entry_id="entry-1"
        )

        assert self._modern(registry) is scoped
        assert legacy.id != scoped.id

    def test_legacy_identifier_is_used_when_the_scoped_one_is_absent(
        self, single_owner_device_registry: Any
    ) -> None:
        """A not-yet-migrated installation is still found."""
        registry = single_owner_device_registry
        registry.add_config_entry("entry-1")
        legacy = registry.add_device(
            identifiers={self._UNSCOPED}, config_entry_id="entry-1"
        )

        assert self._modern(registry) is legacy

    def test_a_miss_returns_none(self, single_owner_device_registry: Any) -> None:
        """Exhausting the candidates is an answer, not an exception."""
        registry = single_owner_device_registry
        registry.add_config_entry("entry-1")

        assert self._modern(registry) is None

    def test_a_modern_miss_does_not_fall_back_to_the_legacy_call(self) -> None:
        """Retrying unscoped after a scoped miss would undo the entry scoping.

        Measured against a registry carrying both APIs, because that is the
        only shape in which dropping the guard changes an observable result.
        """
        stranger = object()
        registry = _BothApisRegistry({self._UNSCOPED: stranger}, owner="entry-2")

        assert self._modern(registry) is None
        assert registry.calls == []
        assert registry.scoped_calls == [
            (self._SCOPED, "entry-1"),
            (self._UNSCOPED, "entry-1"),
        ]

    def test_a_device_of_another_entry_is_not_returned(
        self, single_owner_device_registry: Any
    ) -> None:
        """The deliberate narrowing: identifiers are unique per entry only.

        Two GoogleFindMy entries can both carry the legacy unscoped identifier.
        Before the entry scoping, which one a set lookup returned depended on
        iteration order.
        """
        registry = single_owner_device_registry
        registry.add_config_entry("entry-1")
        registry.add_config_entry("entry-2")
        stranger = registry.add_device(
            identifiers={self._UNSCOPED}, config_entry_id="entry-2"
        )

        assert self._modern(registry) is None
        assert (
            registry.async_get_device_by_identifier(self._UNSCOPED, "entry-2")
            is stranger
        )

    def test_legacy_core_is_asked_one_identifier_at_a_time(self) -> None:
        """On the declared minimum there is only the set-based call, used per
        identifier and in the caller's order.

        The device carries ``config_entries``, because a real ``DeviceEntry`` of
        that core does (tag ``2025.9.1``, line 327). A double without it would
        answer "ownership unknown" to the scoping check below, a state no real
        entry of that core is in.
        """
        ours = SimpleNamespace(id="ours", config_entries={self._ENTRY_ID})
        registry = _LegacyOnlyRegistry({self._UNSCOPED: ours})

        assert self._modern(registry) is ours
        assert registry.calls == [{self._SCOPED}, {self._UNSCOPED}]

    def test_legacy_core_honours_the_candidate_priority(self) -> None:
        """Two own devices, one per identifier: the caller's order decides.

        Handed over as one set, ``async_get_device`` would return whichever
        identifier its set iteration meets first; the double above makes that
        the unscoped one. The scoped identifier has to win regardless, and the
        lookup stops there.
        """
        scoped = SimpleNamespace(id="scoped", config_entries={self._ENTRY_ID})
        legacy = SimpleNamespace(id="legacy", config_entries={self._ENTRY_ID})
        registry = _LegacyOnlyRegistry({self._SCOPED: scoped, self._UNSCOPED: legacy})

        assert self._modern(registry) is scoped
        assert registry.calls == [{self._SCOPED}]

    def test_legacy_core_foreign_device_does_not_shadow_the_own_one(self) -> None:
        """A stranger on the low-priority identifier is skipped, not fatal.

        With a single set lookup the stranger came back first (adverse set
        order), failed the ownership check, and the function answered ``None``
        although this entry's own device sat behind the scoped identifier: a
        false miss on exactly the core where the legacy branch is the only one
        that runs.
        """
        ours = SimpleNamespace(id="ours", config_entries={self._ENTRY_ID})
        stranger = SimpleNamespace(id="stranger", config_entries={"entry-2"})
        registry = _LegacyOnlyRegistry({self._SCOPED: ours, self._UNSCOPED: stranger})

        assert self._modern(registry) is ours

    def test_legacy_core_skips_a_foreign_device_and_keeps_looking(self) -> None:
        """The stranger on the *high*-priority identifier is stepped over."""
        ours = SimpleNamespace(id="ours", config_entries={self._ENTRY_ID})
        stranger = SimpleNamespace(id="stranger", config_entries={"entry-2"})
        registry = _LegacyOnlyRegistry({self._SCOPED: stranger, self._UNSCOPED: ours})

        assert self._modern(registry) is ours
        assert registry.calls == [{self._SCOPED}, {self._UNSCOPED}]

    def test_legacy_core_does_not_hand_back_another_entrys_device(self) -> None:
        """The entry scoping holds on the branch the declared minimum runs.

        ``async_get_device`` searches by identifier alone and knows nothing about
        owning entries, so on Core 2025.9.1 it will happily return a device of a
        *different* GoogleFindMy entry that still carries the legacy unscoped
        identifier. Filtering that out is this function's job, not the caller's:
        every call site would otherwise rebuild the scoping by hand, which
        ``agents/runtime_patterns/AGENTS.md`` forbids.

        Until AP-16 the scoping was applied in the modern branch only, so the
        promise held on 2026.8+ and broke on the declared minimum -- the one core
        where this branch is the only one that runs.
        """
        stranger = SimpleNamespace(id="stranger", config_entries={"entry-2"})
        registry = _LegacyOnlyRegistry({self._UNSCOPED: stranger})

        assert self._modern(registry) is None
        assert registry.calls == [{self._SCOPED}, {self._UNSCOPED}]

    def test_legacy_core_device_without_ownership_is_a_miss(self) -> None:
        """A device entry that names no owner is not an answer either.

        "Owned by nobody" and "does not say" are indistinguishable here, and both
        are a miss: relinking an entity onto a device this entry does not own is
        the failure mode, and neither state rules it out.
        """
        ownerless = SimpleNamespace(id="ownerless")
        registry = _LegacyOnlyRegistry({self._UNSCOPED: ownerless})

        assert self._modern(registry) is None

    def test_signature_drift_propagates_instead_of_rescoping_silently(self) -> None:
        """A ``TypeError`` surfaces; it is never answered with a legacy retry.

        Retrying unscoped would undo the entry scoping and hand back a device of
        a different config entry, and the repository already rules that modern
        registries surface their ``TypeError`` rather than being rewritten into
        a legacy call.
        """
        sentinel = object()
        registry = _DriftedSignatureRegistry({self._SCOPED: sentinel})

        with pytest.raises(TypeError):
            self._modern(registry)
        assert registry.calls == []

    def test_a_registry_with_neither_api_yields_none(self) -> None:
        """No lookup surface at all is survivable, not an exception."""
        assert self._modern(SimpleNamespace()) is None

    @pytest.mark.parametrize("legacy_core", [False, True])
    def test_no_candidates_is_a_miss_on_either_core(self, legacy_core: bool) -> None:
        """An empty priority list must not degrade into an unscoped lookup.

        Both cores are exercised: on a legacy registry an unguarded empty tuple
        would reach ``async_get_device(identifiers=set())``, a deprecated call
        searching for nothing that the caller never asked for.
        """
        registry: Any = _LegacyOnlyRegistry({})
        if not legacy_core:
            registry = _BothApisRegistry({})

        assert (
            registry_helpers.resolve_device_by_identifiers(
                registry, (), entry_id="entry-1"
            )
            is None
        )
        assert registry.calls == []
        if not legacy_core:
            assert registry.scoped_calls == []


class TestIterAllDevices:
    """The whole-registry walk, and the two core generations it hides.

    The point of this helper is that ``dev_reg.devices`` means different things
    on the two supported cores: a plain mapping on the declared minimum
    ``2025.9.1``, whose iteration yields device **ids**, and a view on
    ``2026.9``, whose iteration yields the ``DeviceEntry`` objects. Both branches
    are exercised here because a test suite that only ever sees mapping doubles
    would leave the branch that runs on the *newer* core unmeasured.
    """

    def test_mapping_registry_yields_the_entries_not_the_ids(self) -> None:
        """Core 2025.9.1 shape: a mapping keyed by device id."""
        alpha = SimpleNamespace(id="a")
        beta = SimpleNamespace(id="b")
        registry = SimpleNamespace(devices={"a": alpha, "b": beta})

        assert registry_helpers.iter_all_devices(registry) == (alpha, beta)

    def test_view_registry_yields_the_entries(self) -> None:
        """Core 2026.9 shape: a view whose ``__iter__`` yields entries."""

        class _View:
            """Minimal stand-in for Core's ``DeviceRegistryItemsView``."""

            def __init__(self, entries: list[Any]) -> None:
                self._entries = entries

            def __iter__(self):  # noqa: ANN204 - test double
                return iter(self._entries)

        alpha = SimpleNamespace(id="a")
        registry = SimpleNamespace(devices=_View([alpha]))

        assert registry_helpers.iter_all_devices(registry) == (alpha,)

    def test_registry_without_devices_yields_nothing(self) -> None:
        """A registry double that exposes no ``devices`` is empty, not an error."""
        assert registry_helpers.iter_all_devices(SimpleNamespace()) == ()
        assert registry_helpers.iter_all_devices(SimpleNamespace(devices=None)) == ()

    def test_a_non_iterable_devices_attribute_yields_nothing(self) -> None:
        """Defensive: a ``devices`` that is neither mapping nor iterable.

        Reached by registry doubles, not by any supported core. It answers
        "empty" rather than raising, because this helper is called from
        one-time migration passes whose failure mode should be "did nothing",
        not "broke setup".
        """
        assert registry_helpers.iter_all_devices(SimpleNamespace(devices=42)) == ()
