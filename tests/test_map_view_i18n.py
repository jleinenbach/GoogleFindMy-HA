# tests/test_map_view_i18n.py
"""Server-side render tests for map-view localization.

Exercises ``_generate_map_html`` end to end for a localized language (German),
the English fallback for an unknown language, and the RTL attributes for Hebrew.
The negative rest-string assertion guarantees that no English source label
leaks into a non-English render, catching any string AP2 forgot to route
through the catalog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.googlefindmy import map_view
from custom_components.googlefindmy.map_i18n import format_showing, resolve_map_labels

_NOW = datetime(2024, 1, 1, tzinfo=UTC)

# English source labels in the exact rendered form. The exact form (with the
# surrounding <b>.. tag, > label < markup, or attribute context) is required so
# the negative assertion does not false-fail on JS identifiers or CSS ids that
# merely contain the lower-cased word (for example ``loc.accuracy`` or
# ``id="accuracy"``). These must NOT appear in a non-English render.
_ENGLISH_EXACT_FORMS = (
    ">Start Time<",
    ">End Time<",
    "Min Accuracy (meters)",
    ">Apply Filters<",
    "<summary>Filters</summary>",
    "<b>Time:</b>",
    "<b>Accuracy:</b>",
    "<b>Source:</b>",
    "<b>Location:</b>",
    "<b>Coordinates:</b>",
    "Own Device",
    "Crowdsourced",
    "Copy to clipboard",
    "Copy Plus Code to clipboard",
    "Copy coordinates to clipboard",
    " - Location History",
    "Unknown Device",
)

# Deliberately source-language / non-translatable; must survive every render.
_ALWAYS_PRESENT = (
    "<b>Plus Code:</b>",  # Google brand name (proper noun)
    "© OpenStreetMap contributors",  # map attribution
)


def _make_view(language: str | None) -> map_view.GoogleFindMyMapView:
    if language is None:
        hass = SimpleNamespace()
    else:
        hass = SimpleNamespace(config=SimpleNamespace(language=language))
    return map_view.GoogleFindMyMapView(hass)


def _sample_locations() -> list[dict[str, object]]:
    return [
        {
            "lat": 52.5219,
            "lon": 13.4132,
            "accuracy": 8.0,
            "accuracy_estimated": False,
            "timestamp": _NOW.isoformat(),
            "last_seen": 1_735_000_000,
            "is_own_report": True,
            "semantic_location": "Home",
            "plus_code": "9F4MGCC7+Q7",
        }
    ]


def _render(language: str | None) -> str:
    view = _make_view(language)
    return view._generate_map_html(
        "MyPhone",
        _sample_locations(),
        "device-1",
        _NOW,
        _NOW + timedelta(days=1),
        0,
    )


def test_german_render_uses_german_panel_labels() -> None:
    """A ``de`` language renders German panel labels, not the English ones."""
    html = _render("de")
    assert ">Startzeit<" in html
    assert ">Filter anwenden<" in html
    assert "Mindestgenauigkeit (Meter)" in html
    assert "<b>Quelle:</b>" not in html  # sanity: label lives inside JS concat
    # German popup labels are injected via the JS label object.
    labels = resolve_map_labels("de")
    assert f'"source": "{labels["source"]}"' in html
    assert f'"coordinates": "{labels["coordinates"]}"' in html


def test_german_render_contains_no_english_source_label() -> None:
    """Negative rest-string guard: no English source label survives in ``de``.

    Iterates the full inventory in exact rendered form. A single hard-coded
    English string that AP2 forgot to route through the catalog would fail here.
    """
    html = _render("de")
    for english in _ENGLISH_EXACT_FORMS:
        assert english not in html, (
            f"English source label leaked into de render: {english!r}"
        )
    # The German plural stat line must be present instead of the English one.
    german = resolve_map_labels("de")
    assert format_showing(german, len(_sample_locations())) in html
    assert (
        format_showing(resolve_map_labels("en"), len(_sample_locations())) not in html
    )


def test_non_translatable_strings_always_present_in_german_render() -> None:
    """Proper nouns / attribution stay source-language even under localization."""
    html = _render("de")
    for keep in _ALWAYS_PRESENT:
        assert keep in html, f"non-translatable string missing from de render: {keep!r}"
    # The accuracy unit symbol stays untranslated.
    assert 'toFixed(1) + "m<br>"' in html
    # The device name passes through unchanged.
    assert "MyPhone" in html


def test_unknown_language_falls_back_to_english() -> None:
    """An unknown language renders the English catalog (fail-safe)."""
    html = _render("xx")
    assert ">Start Time<" in html
    assert ">Apply Filters<" in html
    assert " - Location History" in html
    assert '<html lang="xx">' in html  # lang attribute reflects the raw request


def test_stub_hass_without_config_renders_english() -> None:
    """A degraded hass without ``config`` never crashes; it renders English."""
    html = _render(None)
    assert ">Start Time<" in html
    assert '<html lang="en">' in html


def test_hebrew_sets_rtl_and_lang_attributes() -> None:
    """Hebrew (RTL) sets both ``dir="rtl"`` and ``lang="he"``."""
    html = _render("he")
    assert '<html lang="he" dir="rtl">' in html
    # Hebrew label present, English absent.
    assert resolve_map_labels("he")["start_time"] in html
    assert ">Start Time<" not in html


def test_english_render_has_no_rtl_attribute() -> None:
    """A left-to-right language must not carry ``dir="rtl"``."""
    html = _render("en")
    assert '<html lang="en">' in html
    assert 'dir="rtl"' not in html
