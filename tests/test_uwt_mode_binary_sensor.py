# tests/test_uwt_mode_binary_sensor.py
"""Tests for the UWT-Mode binary sensor entity."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from custom_components.googlefindmy.binary_sensor import (
    UWT_MODE_DESC,
    GoogleFindMyUWTModeSensor,
)
from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.eid_resolver import BLEBatteryState
from tests.helpers.config_entries_stub import make_config_entry

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_ble_battery_sensor.py)
# ---------------------------------------------------------------------------


def _fake_hass(domain_data: dict[str, Any] | None = None) -> SimpleNamespace:
    """Return a lightweight hass stand-in."""
    data: dict[str, Any] = {}
    if domain_data is not None:
        data[DOMAIN] = domain_data
    return SimpleNamespace(data=data)


def _make_resolver_stub() -> SimpleNamespace:
    """Return a minimal resolver stub with a battery state dict."""
    return SimpleNamespace(
        _ble_battery_state={},
        get_ble_battery_state=lambda did: None,
    )


def _make_resolver_with_state(
    device_id: str,
    uwt_mode: bool = False,
    battery_level: int = 0,
    battery_pct: int | None = 100,
) -> SimpleNamespace:
    """Return a resolver stub with pre-loaded battery state."""
    state = BLEBatteryState(
        battery_level=battery_level,
        battery_pct=battery_pct,
        uwt_mode=uwt_mode,
        decoded_flags=0x00,
        observed_at_wall=1700000000.0,
    )
    states = {device_id: state}
    return SimpleNamespace(
        _ble_battery_state=states,
        get_ble_battery_state=states.get,
    )


def _fake_coordinator(
    device_id: str = "dev-1",
    present: bool = True,
    visible: bool = True,
    entry_id: str = "entry-1",
    snapshot: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """Create a minimal coordinator stub for sensor tests."""
    return SimpleNamespace(
        config_entry=make_config_entry(entry_id=entry_id),
        is_device_present=lambda did: present,
        is_device_visible_in_subentry=lambda key, did: visible,
        async_update_listeners=lambda: None,
        get_subentry_snapshot=lambda key: snapshot or [],
        last_update_success=True,
    )


def _build_uwt_sensor(
    device_id: str = "dev-1",
    device_name: str = "Test Tracker",
    coordinator: Any = None,
    hass: Any = None,
    resolver: Any = None,
) -> GoogleFindMyUWTModeSensor:
    """Create a UWT-Mode binary sensor with minimal stubs."""
    if coordinator is None:
        coordinator = _fake_coordinator(device_id=device_id)
    if hass is None:
        domain_data: dict[str, Any] = {}
        if resolver is not None:
            domain_data[DATA_EID_RESOLVER] = resolver
        hass = _fake_hass(domain_data)

    sensor = GoogleFindMyUWTModeSensor.__new__(GoogleFindMyUWTModeSensor)
    sensor._subentry_identifier = "tracker"
    sensor._subentry_key = "core_tracking"
    sensor.coordinator = coordinator
    sensor.hass = hass
    sensor._device_id = device_id
    sensor._device = {"id": device_id, "name": device_name}
    sensor.entity_description = UWT_MODE_DESC
    sensor._attr_has_entity_name = True
    sensor._attr_entity_category = None
    sensor._unrecorded_attributes = frozenset(
        {
            "last_ble_observation",
            "google_device_id",
        }
    )
    sensor._fallback_label = device_name
    sensor._attr_unique_id = (
        f"googlefindmy_{coordinator.config_entry.entry_id}_tracker_{device_id}_uwt_mode"
    )
    sensor.entity_id = f"binary_sensor.test_{device_id}_uwt_mode"

    return sensor


# ===========================================================================
# 1. UWT_MODE_DESC entity description
# ===========================================================================


class TestUWTModeDescription:
    """Tests for the UWT-Mode entity description constants."""

    def test_key(self) -> None:
        assert UWT_MODE_DESC.key == "uwt_mode"

    def test_translation_key(self) -> None:
        assert UWT_MODE_DESC.translation_key == "uwt_mode"

    def test_entity_category(self) -> None:
        from homeassistant.helpers.entity import EntityCategory

        assert UWT_MODE_DESC.entity_category == EntityCategory.DIAGNOSTIC


# ===========================================================================
# 2. GoogleFindMyUWTModeSensor — is_on property
# ===========================================================================


class TestUWTModeSensorIsOn:
    """Tests for the is_on property."""

    def test_is_on_true_when_uwt_active(self) -> None:
        """is_on returns True when UWT mode is active."""
        resolver = _make_resolver_with_state("dev-1", uwt_mode=True)
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.is_on is True

    def test_is_on_false_when_uwt_inactive(self) -> None:
        """is_on returns False when UWT mode is not active."""
        resolver = _make_resolver_with_state("dev-1", uwt_mode=False)
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.is_on is False

    def test_is_on_none_without_resolver(self) -> None:
        """is_on returns None when resolver is not available."""
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=None)
        assert sensor.is_on is None

    def test_is_on_none_without_battery_data(self) -> None:
        """is_on returns None when resolver has no data for the device."""
        resolver = _make_resolver_stub()
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.is_on is None

    def test_is_on_none_when_domain_not_dict(self) -> None:
        """is_on returns None when hass.data[DOMAIN] is not a dict."""
        hass = SimpleNamespace(data={DOMAIN: "not-a-dict"})
        sensor = _build_uwt_sensor(device_id="dev-1", hass=hass)
        assert sensor.is_on is None

    def test_is_on_reflects_state_changes(self) -> None:
        """is_on reflects changes when resolver state is updated."""
        state_false = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=False,
            decoded_flags=0x00,
            observed_at_wall=1000.0,
        )
        state_true = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=True,
            decoded_flags=0x80,
            observed_at_wall=2000.0,
        )
        states: dict[str, BLEBatteryState] = {"dev-1": state_false}
        resolver = SimpleNamespace(
            get_ble_battery_state=states.get,
        )
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)

        assert sensor.is_on is False
        states["dev-1"] = state_true
        assert sensor.is_on is True


# ===========================================================================
# 3. GoogleFindMyUWTModeSensor — icon property
# ===========================================================================


class TestUWTModeSensorIcon:
    """Tests for the dynamic icon."""

    def test_icon_alert_when_on(self) -> None:
        resolver = _make_resolver_with_state("dev-1", uwt_mode=True)
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.icon == "mdi:shield-alert"

    def test_icon_check_when_off(self) -> None:
        resolver = _make_resolver_with_state("dev-1", uwt_mode=False)
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.icon == "mdi:shield-check"

    def test_icon_check_when_none(self) -> None:
        """When is_on is None (falsy), icon should be shield-check."""
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=None)
        assert sensor.icon == "mdi:shield-check"


# ===========================================================================
# 4. GoogleFindMyUWTModeSensor — available property
# ===========================================================================


class TestUWTModeSensorAvailability:
    """Tests for the available property."""

    def test_available_when_present(self) -> None:
        resolver = _make_resolver_with_state("dev-1")
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        sensor = _build_uwt_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver
        )
        assert sensor.available is True

    def test_unavailable_when_not_present(self) -> None:
        resolver = _make_resolver_with_state("dev-1")
        coordinator = _fake_coordinator(device_id="dev-1", present=False)
        sensor = _build_uwt_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver
        )
        assert sensor.available is False

    def test_unavailable_when_hidden(self) -> None:
        resolver = _make_resolver_with_state("dev-1")
        coordinator = _fake_coordinator(device_id="dev-1", present=True, visible=False)
        sensor = _build_uwt_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver
        )
        assert sensor.available is False

    def test_available_fallback_on_exception(self) -> None:
        """When is_device_present raises, falls back to True."""
        resolver = _make_resolver_with_state("dev-1")

        def _raise(did: str) -> bool:
            raise RuntimeError("boom")

        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        coordinator.is_device_present = _raise
        sensor = _build_uwt_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver
        )
        # Exception path => falls to bottom return True
        assert sensor.available is True

    def test_available_without_is_device_present(self) -> None:
        """When coordinator lacks is_device_present, available returns True."""
        resolver = _make_resolver_with_state("dev-1")
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        del coordinator.is_device_present
        sensor = _build_uwt_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver
        )
        assert sensor.available is True


# ===========================================================================
# 5. GoogleFindMyUWTModeSensor — extra_state_attributes
# ===========================================================================


class TestUWTModeSensorExtraAttributes:
    """Tests for extra_state_attributes."""

    def test_attributes_with_data(self) -> None:
        resolver = _make_resolver_with_state("dev-1", uwt_mode=True)
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["google_device_id"] == "dev-1"
        assert "last_ble_observation" in attrs
        assert "T" in attrs["last_ble_observation"]
        # uwt_mode and battery_raw_level should NOT be in attrs
        # (they are separate entities / on the battery sensor)
        assert "uwt_mode" not in attrs
        assert "battery_raw_level" not in attrs

    def test_attributes_none_without_resolver(self) -> None:
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=None)
        assert sensor.extra_state_attributes is None

    def test_attributes_none_without_data(self) -> None:
        resolver = _make_resolver_stub()
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.extra_state_attributes is None


# ===========================================================================
# 6. GoogleFindMyUWTModeSensor — _handle_coordinator_update
# ===========================================================================


class TestUWTModeSensorCoordinatorUpdate:
    """Tests for _handle_coordinator_update."""

    def test_update_writes_state(self) -> None:
        resolver = _make_resolver_with_state("dev-1", uwt_mode=False)
        sensor = _build_uwt_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        sensor._handle_coordinator_update()

        sensor.async_write_ha_state.assert_called_once()


# ===========================================================================
# 7. GoogleFindMyUWTModeSensor — documentation guards (BSkando#210)
# ===========================================================================


#: Subjects the withdrawn claims were made about. A claim pattern only fires
#: when one of these stands in the same clause, so that a true sentence about a
#: different mechanism -- the "Skip ringing authentication" control flag, say --
#: does not trip a guard written for the mode semantics.
_UWT_SUBJECT = (
    r"(?:uwt|unwanted tracking protection mode|\bmode\b|this bit|the bit"
    r"|this sensor|the sensor|separated state|separation window)"
)

#: Gap between subject and claim. Bounded, and it may not cross a sentence or
#: clause boundary, so an unrelated neighbouring sentence cannot supply the
#: subject for a claim it has nothing to do with.
_CLAUSE_GAP = r"[^.;:]{0,60}"


def _scoped_to_uwt_subject(claim: str) -> tuple[str, str]:
    """Return *claim* anchored to a UWT subject, in both clause orders.

    Both orders are needed because English puts the subject on either side:
    "the mode is command-driven" and "a command-driven mode".
    """
    return (
        rf"{_UWT_SUBJECT}{_CLAUSE_GAP}{claim}",
        rf"{claim}{_CLAUSE_GAP}{_UWT_SUBJECT}",
    )


class TestUWTModeSensorDocumentationMatchesSpec:
    """Guard the docstrings against the semantics drift around BSkando#210.

    Two errors, in sequence, are what these guards exist for. The docstring
    first claimed the sensor turns on after a fixed 8-24 hour separation window.
    The correction then overshot and denied the window exists at all, and denied
    that the FMDN mode is the DULT separated state. Both denials are refuted by
    the specifications in their own words: the Find Hub Network Accessory
    Specification maps its unwanted tracking protection mode onto the DULT
    separated state, and ``T_(SEPARATED_UT_TIMEOUT)`` is a normative DULT value.

    A figure guard alone pinned the second error in place: it forbade the number
    as such, and the number is correct. So the guard moved from the *number* to
    the *statement*. Forbidden are the withdrawn claims; required is the DULT
    mapping, and the figure is allowed exactly when it is attributed to the
    motion detector rather than to this bit.

    Scope and blind spot, stated plainly: these are pattern and presence checks,
    not prose review. They cannot tell a well-written docstring from a bag of
    the right keywords. Widen them when a regression slips past, not before.
    """

    #: Matches the separation figure in its common spellings: hyphen, en-dash
    #: and em-dash variants, "8 to 24", "8 and 24", the "between ... and ..."
    #: frame, and the spelled-out forms of both. The figure itself is no longer
    #: forbidden; see the attribution guard below.
    #:
    #: Widened on 2026-09-03 after a review found that
    #: "separated for between eight and twenty-four hours" matched neither this
    #: expression nor any withdrawn-claim pattern, so the original false
    #: duration could return in an ordinary rephrasing with every guard green.
    SEPARATION_FIGURE_RE = re.compile(
        r"8\s*(?:[-\u2010-\u2015]|to|and)\s*24\s*h"
        r"|between\s+8\s*(?:[-\u2010-\u2015]|to|and)\s*24"
        r"|eight\s+(?:to|and)\s+twenty[- ]four"
        r"|between\s+eight\s+and\s+twenty[- ]four",
        re.IGNORECASE,
    )

    #: Claims withdrawn on 2026-09-03 against the specification wording, as
    #: patterns rather than spellings (property 1 in ``tests/AGENTS.md``), each
    #: scoped to a UWT subject by :func:`_scoped_to_uwt_subject`. They run
    #: against the docstring normalised to lowercase with whitespace collapsed,
    #: so recasing or rewrapping the old paragraph does not walk past them, and
    #: they cover both withdrawn claims in both directions: that the mode is
    #: command-only, and that the sensor follows a separation window.
    #:
    #: Subjects are load-bearing twice over. "is not what this bit reports" is a
    #: *true* statement about the motion detector, so only the withdrawn claim
    #: about the chime is matched; and "only by command" or "no such window
    #: exists" can be true of a different mechanism, which is why none of these
    #: fires without a UWT subject in the same clause.
    #:
    #: Per property 2 the guarded docstrings do not restate either withdrawn
    #: claim, which is what lets these patterns stay broad without an exception
    #: for the paragraph that mentions the history.
    WITHDRAWN_CLAIM_PATTERNS = tuple(
        pattern
        for claim in (
            r"not by elapsed time",
            r"command[-\s]driven",
            r"is\s+entered\s+and\s+left\s+by\s+command",
            r"only\s+(?:entered|set|activated)\s+by\s+command",
            r"only\s+by\s+command",
            r"no\s+such\s+window\s+exists",
            r"turns\s+on\s+(?:after|once|when)",
            r"(?:becomes|goes)\s+active\s+(?:after|once|when)",
            r"(?:switches|flips)\s+(?:to\s+)?on\s+(?:after|once|when)",
            r"activates\s+(?:after|once|when)",
            r"(?:set|reported)\s+after\s+(?:a|the)?\s*separation",
            r"chime\s+is\s+a\s+dult\s+platform\s+behaviour",
            r"is\s+not\s+the\s+(?:dult\s+)?separated\s+state",
        )
        for pattern in _scoped_to_uwt_subject(claim)
    )

    @staticmethod
    def _normalise(doc: str) -> str:
        """Return *doc* lowercased with all whitespace runs collapsed."""
        return " ".join(doc.split()).lower()

    def test_docstring_does_not_reassert_the_withdrawn_command_only_claim(
        self,
    ) -> None:
        """The mode is not command-only, and the docstrings must not say it is.

        Case (a) in ``tests/AGENTS.md``, corrective: the withdrawn claim shipped
        and produced misdirected automations (BSkando#210).

        Retirement condition: the DULT mapping has survived a release cycle
        unchanged, or BSkando#210 has closed. Either makes this a historical
        note rather than a live correction, and the positive guard below is
        then the only one still earning its place.
        """
        docs = {
            "class": inspect.getdoc(GoogleFindMyUWTModeSensor) or "",
            "is_on": inspect.getdoc(GoogleFindMyUWTModeSensor.is_on.fget) or "",
            "icon": inspect.getdoc(GoogleFindMyUWTModeSensor.icon.fget) or "",
        }

        for name, doc in docs.items():
            normalised = self._normalise(doc)
            for pattern in self.WITHDRAWN_CLAIM_PATTERNS:
                match = re.search(pattern, normalised)
                assert match is None, (
                    f"{name} docstring reasserts a withdrawn claim "
                    f"({pattern!r} matched {match.group(0)!r}). The FMDN mode "
                    "maps onto the DULT separated state, which a device enters "
                    "autonomously after 30 minutes of separation, and the 8-24 "
                    "hour figure gates the motion detector rather than this bit."
                )

    def test_docstring_names_the_dult_mapping_and_its_timer(self) -> None:
        """The corrected semantics must stay stated, not merely un-denied.

        Deleting the withdrawn claim without putting the mapping in its place
        would satisfy the negative guard above and still leave the reader with
        no way to find out how the mode is actually entered.

        Retirement condition: this one outlives the negative guard. It becomes
        obsolete only when the mapping stops being load-bearing for readers of
        this sensor, that is when the entity itself is removed or the mapping is
        stated somewhere a docstring edit cannot silently drop.
        """
        class_doc = inspect.getdoc(GoogleFindMyUWTModeSensor) or ""

        for token in (
            "DULT",
            "separated state",
            "30 minutes",
            "3.4.4",
            "T_(SEPARATED_UT_TIMEOUT)",
        ):
            assert token in class_doc, (
                f"class docstring no longer names {token!r}; the DULT mapping "
                "that replaced the command-only claim is not discoverable."
            )

    def test_separation_figure_is_attributed_to_the_motion_detector(self) -> None:
        """If the 8-24 hour figure appears, it must gate the chime, not the bit.

        The figure is normative DULT and may be named. What must not return is
        the original error of tying it to this sensor turning on, so naming it
        obliges the docstring to name what it actually gates.

        The check is per PARAGRAPH, not per docstring. A presence check over the
        whole text would pass on a docstring that states the figure in one place
        and mentions the motion detector in an unrelated one, which is the shape
        the original error had.

        Retirement condition: the figure disappears from these docstrings
        altogether, at which point the guard is inert by construction, or
        BSkando#210 closes with the attribution unchallenged across a release.
        """
        docs = {
            "class": inspect.getdoc(GoogleFindMyUWTModeSensor) or "",
            "is_on": inspect.getdoc(GoogleFindMyUWTModeSensor.is_on.fget) or "",
            "icon": inspect.getdoc(GoogleFindMyUWTModeSensor.icon.fget) or "",
        }

        for name, doc in docs.items():
            for paragraph in re.split(r"\n\s*\n", doc):
                if self.SEPARATION_FIGURE_RE.search(paragraph) is None:
                    continue
                normalised = self._normalise(paragraph)
                assert "motion detector" in normalised, (
                    f"{name} docstring names the 8-24 hour figure in a paragraph "
                    "that does not name the motion detector it gates; that is "
                    "the original BSkando#210 error."
                )
                assert "t_(separated_ut_timeout)" in normalised, (
                    f"{name} docstring names the 8-24 hour figure without its "
                    "DULT identifier alongside it, so a reader cannot check the "
                    "attribution."
                )

    def test_docstring_names_spec_term_and_source(self) -> None:
        """The docstrings must name the specification term, source and mechanism."""
        class_doc = inspect.getdoc(GoogleFindMyUWTModeSensor) or ""
        is_on_doc = inspect.getdoc(GoogleFindMyUWTModeSensor.is_on.fget) or ""

        assert "unwanted tracking protection mode" in class_doc
        assert (
            "developers.google.com/nearby/fast-pair/specifications/extensions/fmdn"
            in class_doc
        )
        assert "unwanted tracking protection mode" in is_on_doc
        # Both Data IDs must stay named: they are the additional, seeker-driven
        # GATT path into and out of the mode, and the "Skip ringing
        # authentication" caveat below hangs off 0x07.
        assert "0x07" in class_doc
        assert "0x08" in class_doc

    def test_docstring_keeps_the_skip_ringing_authentication_caveat(self) -> None:
        """The unauthenticated-ring consequence of the mode must stay documented.

        Case (b) of the guard family in ``tests/AGENTS.md``, "load-bearing
        cross-reference": the class docstring carries the only in-repo statement
        that links this sensor to the spurious-ring reports, namely that while
        the mode is active and was activated with control flag ``0x01``, ring
        requests are not authenticated, so a chime with no cloud request behind
        it is specification-conformant. Deleting the prose deletes the link
        silently, and no behavioural test would notice.

        This guard pins that the mechanism stays documented. It deliberately
        does NOT pin any claim about what caused a given report: the docstring
        names the DULT non-owner path as the likelier explanation, and that
        attribution is the reporter's, not this repository's.

        The guard does not assert on ``0x01``: that literal is already required
        by the bit-mask caveat above and would pass even if the control flag
        went unmentioned.

        Retirement condition: BSkando#195 and BSkando#210 both closed, or the
        mechanism documented somewhere that a docstring edit cannot silently
        remove.
        """
        class_doc = inspect.getdoc(GoogleFindMyUWTModeSensor) or ""

        assert "Skip ringing authentication" in class_doc
        assert "BSkando#195" in class_doc

    def test_docstring_keeps_the_msb_first_bit_numbering_caveat(self) -> None:
        """Spec bit 7 is standard bit 0; the mask is 0x01, never 0x80.

        The resolver marks ``0x80`` as an anti-pattern. A docstring that says
        "bit 7" without the caveat invites exactly that "fix".
        """
        class_doc = inspect.getdoc(GoogleFindMyUWTModeSensor) or ""

        assert "spec bit 7" in class_doc
        assert "0x01" in class_doc
        assert "0x80" in class_doc

    def test_is_on_docstring_documents_absence_of_ttl(self) -> None:
        """The missing staleness TTL must be documented as a decision.

        Mirrors the rationale comment on the BLE battery sensor: the absence of
        ageing is deliberate, has a stated reason, and names what would make it
        revisitable.
        """
        is_on_doc = inspect.getdoc(GoogleFindMyUWTModeSensor.is_on.fget) or ""

        assert "No BLE staleness TTL" in is_on_doc
        assert "decision rather than an oversight" in is_on_doc
        assert "FMDN_FLAGS_PROBE" in is_on_doc
