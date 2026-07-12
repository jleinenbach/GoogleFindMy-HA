# tests/test_map_i18n.py
"""Unit tests for the map-view localization catalog (``map_i18n``).

Covers the pure catalog invariants and resolution helpers without the HTTP
stack: completeness (same key set and non-empty values across every locale),
three-stage language resolution with English fallback, region normalization,
the minimal plural helper, and RTL detection.
"""

from __future__ import annotations

from custom_components.googlefindmy import map_i18n
from custom_components.googlefindmy.map_i18n import (
    MAP_LABELS,
    format_showing,
    is_rtl,
    resolve_map_labels,
)

_EXPECTED_LOCALES = {"de", "en", "es", "fr", "he", "it", "nl", "pl", "pt-BR", "pt"}


def test_all_ten_locales_present() -> None:
    """The catalog ships exactly the ten locales the repo maintains."""
    assert set(MAP_LABELS) == _EXPECTED_LOCALES


def test_every_locale_has_the_english_key_set_and_nonempty_values() -> None:
    """Completeness guard: no locale may drop a key or carry an empty value.

    This is the core of the "all languages covered" requirement: every locale
    must expose the exact ``en`` key set (no missing, no extra key) AND every
    value must be a non-empty string. The non-empty check closes the blind spot
    a pure key-set comparison would leave (an empty ``""`` label would pass a
    key-only guard but render a blank UI string).
    """
    english_keys = set(MAP_LABELS["en"])
    assert english_keys, "English reference catalog must not be empty"

    for locale, labels in MAP_LABELS.items():
        assert set(labels) == english_keys, (
            f"locale {locale!r} key set differs from en: "
            f"missing={english_keys - set(labels)}, extra={set(labels) - english_keys}"
        )
        for key, value in labels.items():
            assert isinstance(value, str) and value.strip() != "", (
                f"locale {locale!r} key {key!r} must be a non-empty string, got {value!r}"
            )


def test_no_catalog_value_contains_a_single_quote() -> None:
    """Copy tooltips are injected into single-quoted HTML attributes.

    The client JS builds ``title='...'``/``aria-label='...'`` attributes by
    concatenating catalog values into single-quoted attributes (mirroring the
    pre-existing English literals). A value containing ``'`` would break the
    attribute, so the catalog must stay single-quote free.
    """
    for locale, labels in MAP_LABELS.items():
        for key, value in labels.items():
            assert "'" not in value, (
                f"locale {locale!r} key {key!r} contains a single quote"
            )


def test_resolve_none_and_unknown_fall_back_to_english() -> None:
    """A missing or unknown language yields the English label set."""
    english = MAP_LABELS["en"]
    assert resolve_map_labels(None) == english
    assert resolve_map_labels("") == english
    assert resolve_map_labels("   ") == english
    assert resolve_map_labels("xx") == english
    assert resolve_map_labels("zz-ZZ") == english


def test_resolve_exact_code_is_case_insensitive() -> None:
    """Exact codes resolve regardless of case, including region-qualified ones."""
    assert resolve_map_labels("de")["source"] == MAP_LABELS["de"]["source"]
    assert resolve_map_labels("DE")["source"] == MAP_LABELS["de"]["source"]
    assert resolve_map_labels("pt-BR")["end_time"] == MAP_LABELS["pt-BR"]["end_time"]
    assert resolve_map_labels("pt-br")["end_time"] == MAP_LABELS["pt-BR"]["end_time"]


def test_pt_br_is_preferred_over_base_pt() -> None:
    """The exact ``pt-BR`` code wins over the base ``pt`` (distinct values)."""
    assert MAP_LABELS["pt-BR"]["end_time"] != MAP_LABELS["pt"]["end_time"]
    assert resolve_map_labels("pt-BR")["end_time"] == MAP_LABELS["pt-BR"]["end_time"]


def test_region_qualified_code_falls_back_to_base_language() -> None:
    """A region-qualified code with no exact match uses the base language."""
    # de-DE has no exact entry -> base 'de'.
    assert resolve_map_labels("de-DE")["source"] == MAP_LABELS["de"]["source"]
    # pt-PT has no exact entry -> base 'pt' (not pt-BR).
    assert resolve_map_labels("pt-PT")["end_time"] == MAP_LABELS["pt"]["end_time"]


def test_resolve_returns_a_defensive_copy() -> None:
    """Mutating the resolved dict must not corrupt the shared catalog."""
    labels = resolve_map_labels("de")
    labels["source"] = "TAMPERED"
    assert MAP_LABELS["de"]["source"] != "TAMPERED"


def test_format_showing_singular_vs_plural() -> None:
    """``format_showing`` selects singular only for exactly one point."""
    english = resolve_map_labels("en")
    assert format_showing(english, 1) == "Showing 1 point"
    assert format_showing(english, 0) == "Showing 0 points"
    assert format_showing(english, 2) == "Showing 2 points"
    assert format_showing(english, 42) == "Showing 42 points"


def test_format_showing_localizes_and_substitutes_count() -> None:
    """The plural helper works per locale and substitutes ``{n}``."""
    german = resolve_map_labels("de")
    assert format_showing(german, 1) == "1 Punkt angezeigt"
    assert format_showing(german, 3) == "3 Punkte angezeigt"
    # No literal placeholder must survive.
    assert "{n}" not in format_showing(german, 5)


def test_is_rtl_detection() -> None:
    """Only Hebrew (and its region variants) is right-to-left."""
    assert is_rtl("he") is True
    assert is_rtl("he-IL") is True
    assert is_rtl("HE") is True
    assert is_rtl("en") is False
    assert is_rtl("de") is False
    assert is_rtl(None) is False
    assert is_rtl("") is False


def test_rtl_languages_constant_is_a_frozenset() -> None:
    """RTL set is an immutable frozenset containing Hebrew."""
    assert isinstance(map_i18n.RTL_LANGUAGES, frozenset)
    assert "he" in map_i18n.RTL_LANGUAGES


def test_filters_summary_key_present_and_localized() -> None:
    """The collapsible-filters summary label exists and is localized.

    The generic completeness guard already forces the key into every locale
    (all must mirror ``en``); this pins the concrete reference values so a
    silent English-only or empty translation cannot slip through.
    """
    for locale in _EXPECTED_LOCALES:
        assert "filters_summary" in MAP_LABELS[locale], (
            f"locale {locale!r} is missing 'filters_summary'"
        )
    assert MAP_LABELS["en"]["filters_summary"] == "Filters"
    assert MAP_LABELS["de"]["filters_summary"] == "Filter"


def test_filters_summary_resolves_via_language_lookup() -> None:
    """The new key flows through the standard three-stage resolver."""
    assert resolve_map_labels("de")["filters_summary"] == "Filter"
    assert resolve_map_labels("de-DE")["filters_summary"] == "Filter"
    assert resolve_map_labels("xx")["filters_summary"] == "Filters"
