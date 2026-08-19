# tests/test_map_view_controls_panel.py
"""Structure tests for the map-view control panel ergonomics.

Two defects are locked down here against regression:

1. **h3 spacing reset.** The device-name ``<h3>`` used to carry no own CSS rule
   and inherited the user-agent default ``margin: 1em 0`` (~18.7px), which made
   the gap above the name roughly 2.25x the gap below the point-count line. An
   explicit ``.controls h3`` rule normalizes it onto the panel's 10px rhythm.

2. **Collapsible filters (progressive disclosure).** On a phone the fixed panel
   hid a large part of the map. The three filter groups plus the apply button
   now live inside a native ``<details>``/``<summary>`` disclosure widget so the
   user can collapse them, while the trigger stays structurally visible (no
   real "close"). The device name (``<h3>``) and the point-count (``.stats``)
   stay OUTSIDE the widget so system state is readable while collapsed.

The panel defaults to ``<details open>`` (minimal variant): the current desktop
behavior is preserved and the user collapses on demand, which is the natural
order because the filter must be operated before the map underneath is wanted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.googlefindmy import map_view

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


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


def _render(language: str | None = "en") -> str:
    view = _make_view(language)
    return view._generate_map_html(
        "MyPhone",
        _sample_locations(),
        "device-1",
        _NOW,
        _NOW + timedelta(days=1),
        0,
    )


# --------------------------- Defect 1: h3 spacing ---------------------------


def test_h3_has_explicit_margin_reset() -> None:
    """The panel ships an own ``.controls h3`` rule (no UA-default margin).

    The exact declaration matters: ``margin: 0 0 10px`` normalizes the top gap
    onto the 15px panel padding (symmetric to the bottom) and the name-to-field
    gap onto the panel's 10px rhythm.
    """
    html = _render()
    assert ".controls h3 {" in html
    assert "margin: 0 0 10px" in html


def test_control_group_rhythm_unit_is_unchanged() -> None:
    """The 10px rhythm the h3 reset aligns to must stay intact."""
    html = _render()
    assert ".control-group { margin-bottom: 10px; }" in html


# ---------------------- Defect 2: collapsible filters -----------------------


def test_filters_are_wrapped_in_a_single_open_details_widget() -> None:
    """Exactly one native ``<details open>`` with one ``<summary>`` is rendered."""
    html = _render()
    assert html.count("<details open>") == 1
    assert html.count("</details>") == 1
    assert html.count("<summary>") == 1
    assert html.count("</summary>") == 1


def test_filter_inputs_and_button_live_inside_details() -> None:
    """The three filter inputs and the apply button are inside the widget."""
    html = _render()
    start = html.index("<details open>")
    end = html.index("</details>")
    for needle in ('id="start"', 'id="end"', 'id="accuracy"', "applyFilters()"):
        pos = html.find(needle)
        assert start < pos < end, f"{needle!r} must be inside <details>"


def test_device_name_stays_outside_and_above_details() -> None:
    """The device name (``h3``) stays visible above the collapsible widget."""
    html = _render()
    assert html.index("<h3>") < html.index("<details open>")


def test_point_count_stats_stay_outside_and_below_details() -> None:
    """The point-count (``.stats``) stays visible below the widget when collapsed."""
    html = _render()
    assert html.index("</details>") < html.index('<div class="stats">')


def test_summary_is_a_styled_pointer_tap_target() -> None:
    """The summary carries pointer + tap-target styling (WCAG 2.5.5 ~44px).

    ``12px`` vertical padding on a ``20px`` min-height content box yields a 44px
    tap target; ``cursor: pointer`` signals interactivity.
    """
    html = _render()
    assert ".controls details > summary {" in html
    assert "cursor: pointer;" in html
    assert "min-height: 20px;" in html
    assert "padding: 12px 0;" in html


def test_summary_has_a_visible_focus_ring() -> None:
    """Keyboard focus on the summary is visibly indicated."""
    html = _render()
    assert ".controls details > summary:focus-visible {" in html


def test_details_is_open_by_default_across_languages() -> None:
    """The widget defaults to open (minimal variant) regardless of language."""
    for language in ("en", "de", "he", None):
        html = _render(language)
        assert "<details open>" in html


def test_input_ids_are_unchanged_so_apply_filters_still_binds() -> None:
    """DOM was re-nested, not renamed: applyFilters() still finds its inputs.

    ``start``/``end`` are now resolved through the shared ``setDateParam`` helper
    (which reads ``getElementById(id)``); ``accuracy`` is still read directly.
    """
    html = _render()
    assert "setDateParam(url, 'start')" in html
    assert "setDateParam(url, 'end')" in html
    assert "getElementById(id)" in html
    assert "getElementById('accuracy')" in html


# ------------------- Defect 3: right-edge padding symmetry ------------------


def test_full_width_controls_use_border_box_sizing() -> None:
    """Full-width inputs/button must not overflow the panel's right padding.

    The date inputs, the range slider and the apply button all carry
    ``width: 100%`` together with their own ``padding`` and a ``1px`` border.
    Under the default ``content-box`` model that padding+border is added ON TOP
    of the 100% content width, so the elements grew ~12px past the panel's 15px
    right padding: the right gap collapsed to a few pixels while the left gap
    stayed at 15px (visible asymmetry reported on the rendered map). A scoped
    ``box-sizing: border-box`` reset folds padding+border back INTO the 100%,
    restoring the symmetric 15px gutter on both sides.

    Mutation check: dropping the reset re-introduces the overflow, so this
    assertion goes red — it pins the fix, not merely the presence of a string.
    """
    html = _render()
    assert ".controls input, .controls button {" in html
    assert "box-sizing: border-box;" in html


def test_border_box_reset_does_not_touch_the_summary_tap_target() -> None:
    """The border-box reset must stay off the summary (regression guard).

    The summary's 44px tap target is computed on ``content-box`` geometry
    (20px min-height + 2x12px padding). A broad ``.controls *`` descendant
    reset would silently re-measure that min-height on the border box and
    shrink the handle to ~41px, below the documented WCAG 2.5.5 target. The
    reset is therefore restricted to the full-width form controls and must not
    reach the summary via a universal descendant selector.

    Mutation check: widening the selector back to ``.controls *`` makes this
    assertion red — it pins the *scope* of the reset, not just its presence.
    """
    html = _render()
    assert ".controls *, .controls *::before" not in html
    assert "box-sizing: border-box;" in html  # reset still present, just scoped


def test_style_and_details_tags_are_balanced_and_ordered() -> None:
    """Guard against unclosed tags after the CSS/HTML rewrite.

    A substring assertion cannot see a dropped ``</style>``; this checks tag
    balance and document ordering explicitly (``<style>`` closes before
    ``</head>``; ``<details>`` closes before ``</body>``).
    """
    html = _render()
    # Two style blocks since Leaflet is embedded rather than pulled from a CDN:
    # the vendored stylesheet first, the page's own rules second.
    assert html.count("<style>") == html.count("</style>") == 2
    assert html.count("<details open>") == html.count("</details>") == 1
    assert html.rindex("</style>") < html.index("</head>")
    assert html.index("</details>") < html.index("</body>")
