# tests/test_eid_resolver_flags_geometry.py
"""The matched EID candidate, not a re-derivation, locates the flags byte.

Every test here exists because a payload byte was being read as the hashed
flags byte on the strength of a coincidence: byte 7 (or byte 0) happening to
carry 0x40/0x41 while actually being EID material. That is a 1-in-256 event
per device and rotation window, and its two outcomes are a silent resolution
failure and a fabricated battery level plus UWT bit.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.eid_resolver import (
    EidCandidate,
    EIDMatch,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
)

_FMDN_FRAME_TYPE = resolver_module.FMDN_FRAME_TYPE  # 0x40
_MODERN_FRAME_TYPE = resolver_module.MODERN_FRAME_TYPE  # 0x41
_RAW_HEADER_LENGTH = resolver_module.RAW_HEADER_LENGTH  # 1
_SERVICE_DATA_OFFSET = resolver_module.SERVICE_DATA_OFFSET  # 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_hass() -> SimpleNamespace:
    """Return a lightweight hass stand-in that never schedules real work."""

    def _close(coro: object, name: str | None = None) -> None:
        if hasattr(coro, "close"):
            coro.close()

    return SimpleNamespace(
        async_create_task=_close,
        async_create_background_task=_close,
        data={DOMAIN: {}},
    )


def _make_resolver() -> GoogleFindMyEIDResolver:
    """Create a resolver instance suitable for direct method calls."""
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}

    async def _async_noop(payload: Any = None) -> None:
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._ensure_cache_defaults()
    return resolver


def _match(device_id: str) -> EIDMatch:
    """Create a test EIDMatch whose storage key equals *device_id*."""
    return EIDMatch(
        device_id=device_id,
        config_entry_id="entry-1",
        canonical_id=device_id,
        time_offset=0,
        is_reversed=False,
    )


def _register(
    resolver: GoogleFindMyEIDResolver,
    eid: bytes,
    device_id: str,
    *,
    xor_mask: int = 0x00,
) -> None:
    """Make *eid* resolvable to *device_id* with a known XOR mask."""
    resolver._lookup[eid] = [_match(device_id)]
    resolver._lookup_metadata[eid] = {"flags_xor_mask": xor_mask}


# ---------------------------------------------------------------------------
# The R7 case: a raw-header payload whose byte 7 collides with a frame type
# ---------------------------------------------------------------------------


class TestRawHeaderPayloadWithByte7Collision:
    """34-byte raw-header payload, payload[7] == 0x40 by coincidence."""

    @staticmethod
    def _payload(flags_byte: int, decoy: int = 0x00) -> tuple[bytes, bytes]:
        """Return (payload, eid) for [0x41][EID(32)][flags], payload[7]=0x40.

        payload[7] is EID byte 6, so the service-data probe reads 0x40 there
        and builds payload[8:28] as a candidate -- 20 bytes of EID material
        that resolve to nothing. The real candidate is payload[1:33].
        """
        eid = bytearray([0x11] * MODERN_EID_LENGTH)
        eid[6] = _FMDN_FRAME_TYPE  # -> payload[7] == 0x40
        eid[27] = decoy  # -> payload[28], the byte the old chain would read
        payload = bytes([_MODERN_FRAME_TYPE]) + bytes(eid) + bytes([flags_byte])
        return payload, bytes(eid)

    def test_raw_header_payload_resolves_when_byte7_collides_with_frame_type(
        self,
    ) -> None:
        """The colliding byte no longer suppresses the real candidate."""
        resolver = _make_resolver()
        payload, eid = self._payload(flags_byte=0x01)
        assert payload[7] == _FMDN_FRAME_TYPE  # the coincidence, made explicit
        _register(resolver, eid, "dev-collision")

        result = resolver.resolve_eid(payload)

        assert result is not None
        assert result.device_id == "dev-collision"
        state = resolver._ble_battery_state.get("dev-collision")
        assert state is not None
        assert state.decoded_flags == payload[33]

    def test_flags_offset_follows_the_matched_candidate(self) -> None:
        """A decoy at the old offset must not win over the real flags byte."""
        resolver = _make_resolver()
        # 0x01 -> UWT bit set, battery level 0. Decoy 0x02 -> battery level 1.
        payload, eid = self._payload(flags_byte=0x01, decoy=0x02)
        _register(resolver, eid, "dev-decoy")

        resolver.resolve_eid(payload)

        state = resolver._ble_battery_state.get("dev-decoy")
        assert state is not None
        # Positive: the byte after the matched EID.
        assert state.decoded_flags == payload[33] == 0x01
        assert state.uwt_mode is True
        # And specifically not the decoy the pre-geometry chain would read.
        assert state.decoded_flags != payload[28]
        assert state.battery_level == 0

    def test_overlapping_geometries_report_matched_frame_type(self) -> None:
        """``_resolve_eid_internal`` reports the *matched* candidate's frame.

        Both geometries now produce a candidate for this payload, so the
        reported frame type has to come from the one that matched (0x41),
        not from whichever branch ran last. This asserts the value returned
        by ``_resolve_eid_internal`` -- the compatibility wrapper
        ``_extract_candidates`` reports the payload-level frame type instead
        and the two deliberately differ here.
        """
        resolver = _make_resolver()
        payload, eid = self._payload(flags_byte=0x01)
        _register(resolver, eid, "dev-frame")

        _matches, matched, observed_frame = resolver._resolve_eid_internal(payload)

        assert matched == eid
        assert observed_frame == _MODERN_FRAME_TYPE


# ---------------------------------------------------------------------------
# The bare-EID case: a consumer already stripped frame and flags bytes
# ---------------------------------------------------------------------------


class TestBareEidCandidate:
    """Payloads that *are* the EID, e.g. the Bermuda fallback path.

    Bermuda feeds naked EID candidates into this resolver alongside raw
    payloads (``bermuda/fmdn/integration.py``: ``extract_raw_fmdn_payloads``
    plus ``extract_fmdn_eids``); its ``auto`` extraction mode adds the
    frame-and-flags-stripped base as a candidate in its own right. Those
    payloads provably carry no flags byte, so guessing one out of EID
    material is a fabrication with nothing to correct it.
    """

    @staticmethod
    def _bare_payload() -> bytes:
        """32-byte naked EID with payload[7] == 0x40 and payload[0] not a frame."""
        body = bytearray([0x22] * MODERN_EID_LENGTH)
        body[0] = 0x99  # not 0x40/0x41, so the short-circuit applies
        body[7] = _FMDN_FRAME_TYPE  # the coincidence
        body[28] = 0x06  # what the pre-geometry chain would read as flags
        return bytes(body)

    def test_bare_modern_eid_with_byte7_collision_decodes_no_flags(self) -> None:
        """Resolved *and* no battery state: there is no flags byte to decode."""
        resolver = _make_resolver()
        payload = self._bare_payload()
        _register(resolver, payload, "dev-bare")

        result = resolver.resolve_eid(payload)

        # Positive control: without it, "no battery state" would also pass
        # when the resolution failed for an unrelated reason.
        assert result is not None
        assert result.device_id == "dev-bare"
        assert resolver._ble_battery_state.get("dev-bare") is None

    def test_bare_eid_geometry_is_authoritative_not_guessed(self) -> None:
        """The *cause*: the candidate is 'bare', which is not 'window'.

        Both layouts carry ``frame_type=None``. Hanging the fallback on the
        frame type therefore cannot tell them apart, and this payload would
        fall back into the guess it must not make.
        """
        resolver = _make_resolver()
        payload = self._bare_payload()
        _register(resolver, payload, "dev-bare-geom")

        candidates, observed_frame = resolver._extract_eid_candidates(payload)

        assert len(candidates) == 1
        assert candidates[0].layout == "bare"
        assert candidates[0].offset == 0
        assert candidates[0].eid == payload
        assert observed_frame is None

        result = resolver.resolve_eid(payload)
        assert result is not None  # positive control
        assert resolver._ble_battery_state.get("dev-bare-geom") is None


# ---------------------------------------------------------------------------
# The discriminator itself
# ---------------------------------------------------------------------------


class TestFlagsGeometryDecisionTable:
    """All four states of the geometry discriminator, as behaviour."""

    @pytest.mark.parametrize(
        ("layout", "authoritative"),
        [
            ("framed", True),
            ("bare", True),
            ("window", False),
            (None, False),  # no geometry supplied at all
        ],
    )
    def test_flags_geometry_decision_table(
        self, layout: str | None, authoritative: bool
    ) -> None:
        """Only 'framed' and 'bare' may claim to know the flags position.

        The payload is a 22-byte raw-header frame: the pre-geometry chain
        reads ``raw[21]``, while the supplied geometry points at ``raw[5]``.
        The two answers differ, so which one comes out is observable.
        """
        resolver = _make_resolver()
        eid = bytes([0x33] * LEGACY_EID_LENGTH)
        payload = bytearray([_FMDN_FRAME_TYPE]) + bytearray(eid) + bytearray([0x01])
        payload[5] = 0x04  # what a geometry with offset 1 / len 4 would read
        raw = bytes(payload)
        matches = [_match("dev-table")]
        metadata = {"flags_xor_mask": 0x00}

        geometry = (
            None
            if layout is None
            else EidCandidate(eid=raw[1:5], offset=1, frame_type=None, layout=layout)
        )
        resolver._update_ble_battery(raw, None, metadata, matches, geometry=geometry)

        state = resolver._ble_battery_state.get("dev-table")
        assert state is not None
        if authoritative:
            assert state.decoded_flags == raw[5] == 0x04
        else:
            assert state.decoded_flags == raw[21] == 0x01

    def test_unknown_layout_falls_back_instead_of_claiming_geometry(self) -> None:
        """An unexpected layout value must fail *safe*, into guessing.

        Failing open into the authoritative branch would turn an arbitrary
        payload byte into the flags byte -- a new fabrication class at the
        exact spot the old one was removed.
        """
        resolver = _make_resolver()
        eid = bytes([0x33] * LEGACY_EID_LENGTH)
        payload = bytearray([_FMDN_FRAME_TYPE]) + bytearray(eid) + bytearray([0x01])
        payload[5] = 0x04
        raw = bytes(payload)

        geometry = EidCandidate(
            eid=raw[1:5],
            offset=1,
            frame_type=None,
            layout="windows",  # type: ignore[arg-type]  # deliberate typo
        )
        resolver._update_ble_battery(
            raw, None, {"flags_xor_mask": 0x00}, [_match("dev-typo")], geometry=geometry
        )

        state = resolver._ble_battery_state.get("dev-typo")
        assert state is not None
        assert state.decoded_flags == raw[21]

    def test_sliding_window_match_does_not_claim_a_flags_offset(self) -> None:
        """A window offset is a find position, not a parsed layout."""
        resolver = _make_resolver()
        # 26 bytes, neither raw[0] nor raw[7] a frame type: the match can only
        # come from the sliding window.
        body = bytearray([0x55] * 26)
        body[0] = 0x01
        body[7] = 0x02
        raw = bytes(body)
        eid = raw[3 : 3 + LEGACY_EID_LENGTH]
        _register(resolver, eid, "dev-window")

        candidates, observed_frame = resolver._extract_eid_candidates(raw)
        assert observed_frame is None
        assert all(candidate.layout == "window" for candidate in candidates)

        result = resolver.resolve_eid(raw)
        assert result is not None  # positive control
        # The pre-geometry chain finds no flags byte for this shape either, so
        # the behaviour is unchanged -- which is the point.
        assert resolver._ble_battery_state.get("dev-window") is None


# ---------------------------------------------------------------------------
# The candidate type itself
# ---------------------------------------------------------------------------


class TestCandidateTypeIsNotByteKeyCompatible:
    """The type change only pays off if a misuse is loud rather than silent."""

    def test_candidate_type_is_not_byte_key_compatible(self) -> None:
        """A candidate must be unusable as a dict key and as a set member.

        Both lookups it guards (``_lookup``, ``_lookup_metadata``) are keyed
        by ``bytes``. A hashable candidate would be accepted by both and
        simply never match, so the failure would be a permanent silent miss.
        ``frozen=True`` alone generates a working ``__hash__``; measured, not
        assumed.
        """
        candidate = EidCandidate(
            eid=b"\x01\x02", offset=0, frame_type=None, layout="bare"
        )

        with pytest.raises(TypeError):
            {b"x": 1}.get(candidate)  # type: ignore[call-overload]
        with pytest.raises(TypeError):
            {candidate}  # noqa: B018


# ---------------------------------------------------------------------------
# Monotonicity: nothing that resolved before stops resolving
# ---------------------------------------------------------------------------


class TestHappyPathsUnchanged:
    """Well-formed payloads keep resolving and keep decoding their flags."""

    def test_legacy_service_data_happy_path_unchanged(self) -> None:
        """[header(7)][0x40][EID(20)][flags]."""
        resolver = _make_resolver()
        eid = bytes([0x44] * LEGACY_EID_LENGTH)
        raw = b"\x00" * 7 + bytes([_FMDN_FRAME_TYPE]) + eid + bytes([0x03])
        _register(resolver, eid, "dev-legacy-sd")

        result = resolver.resolve_eid(raw)

        assert result is not None
        state = resolver._ble_battery_state.get("dev-legacy-sd")
        assert state is not None
        assert state.decoded_flags == 0x03

    def test_modern_raw_header_happy_path_unchanged(self) -> None:
        """[0x41][EID(32)][flags] without any byte-7 coincidence."""
        resolver = _make_resolver()
        eid = bytes([0x55] * MODERN_EID_LENGTH)
        raw = bytes([_MODERN_FRAME_TYPE]) + eid + bytes([0x05])
        _register(resolver, eid, "dev-modern-raw")

        result = resolver.resolve_eid(raw)

        assert result is not None
        state = resolver._ble_battery_state.get("dev-modern-raw")
        assert state is not None
        assert state.decoded_flags == 0x05

    def test_framed_payload_without_flags_byte_decodes_none(self) -> None:
        """Geometry known, no flags byte present: then there is none.

        The specification allows the byte to be omitted entirely when the
        beacon supports neither battery indication nor unwanted tracking
        protection mode, so this shape is normal, not truncated.
        """
        resolver = _make_resolver()
        eid = bytes([0x66] * MODERN_EID_LENGTH)
        raw = bytes([_MODERN_FRAME_TYPE]) + eid  # 33 bytes, no flags byte
        _register(resolver, eid, "dev-no-flags")

        result = resolver.resolve_eid(raw)

        assert result is not None  # positive control
        assert resolver._ble_battery_state.get("dev-no-flags") is None


class TestTruncationDiagnosticStaysHonest:
    """The truncation warning must describe a payload that actually failed.

    Removing the exclusive service-data gate means the raw-header branch now
    also runs for payloads the service-data probe already resolved. Without a
    guard, those would be reported as truncated while a candidate exists.
    """

    @staticmethod
    def _resolved_but_short_modern(fill: int) -> bytes:
        """29 bytes: service-data hit at byte 7, raw header claims 0x41."""
        body = bytearray([fill] * 29)
        body[0] = _MODERN_FRAME_TYPE  # raw-header branch sees a modern frame
        body[7] = _FMDN_FRAME_TYPE  # service-data branch builds a candidate
        return bytes(body)

    def test_no_truncation_warning_when_a_candidate_was_found(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A resolved payload is not a truncated one."""
        resolver = _make_resolver()
        raw = self._resolved_but_short_modern(0x77)

        with caplog.at_level("WARNING"):
            candidates, _frame = resolver._extract_eid_candidates(raw)

        assert candidates  # the service-data probe did produce one
        assert "Truncated or unexpected framed BLE payload" not in caplog.text

    def test_truncation_warning_still_fires_without_any_candidate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Positive control: the diagnostic is guarded, not disabled."""
        resolver = _make_resolver()
        body = bytearray([0x77] * 29)
        body[0] = _MODERN_FRAME_TYPE
        body[7] = 0x00  # no service-data candidate this time
        raw = bytes(body)

        with caplog.at_level("WARNING"):
            resolver._extract_eid_candidates(raw)

        assert "Truncated or unexpected framed BLE payload" in caplog.text


class TestLegacyWrapperSemantics:
    """The compatibility wrapper keeps its candidate list, not its frame type.

    Pinned because the change is silent otherwise: no existing test covers a
    payload where both geometries apply, so nothing would notice the second
    return value moving.
    """

    def test_wrapper_reports_the_last_frame_byte_seen(self) -> None:
        """Both branches run now, so the raw-header one has the last word."""
        resolver = _make_resolver()
        eid = bytearray([0x11] * MODERN_EID_LENGTH)
        eid[6] = _FMDN_FRAME_TYPE  # -> payload[7] == 0x40
        payload = bytes([_MODERN_FRAME_TYPE]) + bytes(eid) + bytes([0x01])

        candidates, observed_frame = resolver._extract_candidates(payload)

        # Candidate list: service-data slice first, raw-header slice second.
        assert candidates == [payload[8:28], payload[1:33]]
        # Second element is the raw-header frame, not the first candidate's.
        assert observed_frame == _MODERN_FRAME_TYPE

    def test_production_path_reports_the_matched_frame_instead(self) -> None:
        """Which is exactly why production does not use the wrapper."""
        resolver = _make_resolver()
        eid = bytearray([0x11] * MODERN_EID_LENGTH)
        eid[6] = _FMDN_FRAME_TYPE
        payload = bytes([_MODERN_FRAME_TYPE]) + bytes(eid) + bytes([0x01])
        _register(resolver, payload[8:28], "dev-sd-hit")

        _matches, matched, observed_frame = resolver._resolve_eid_internal(payload)

        # The service-data candidate is what matched, so its frame type is
        # what gets reported -- 0x40, the byte that is EID material here.
        assert matched == payload[8:28]
        assert observed_frame == _FMDN_FRAME_TYPE


class TestHeuristicConsumerSeesBytes:
    """The one consumer of the candidate list that has no tests of its own.

    ``_heuristic_resolve`` builds ``set(candidates)`` internally. Handing it
    ``EidCandidate`` objects instead of their bytes now raises ``TypeError``
    rather than silently producing a set disjoint from every generated EID --
    but only if something actually walks this path, and nothing did.
    """

    def test_unresolvable_payload_reaches_the_heuristic_path_with_bytes(
        self,
    ) -> None:
        """A cache miss must not blow up on the way to heuristic discovery.

        ``_cached_identities`` has to be non-empty: ``_heuristic_resolve``
        returns before building its candidate set otherwise, and the test
        would cover the call site without ever exercising it.
        """
        resolver = _make_resolver()
        # A non-empty lookup, so the "cache not primed" early return is skipped.
        _register(resolver, bytes([0x01] * LEGACY_EID_LENGTH), "dev-other")
        # Non-empty identities, so set(candidates) is actually reached; a
        # None identity_key then short-circuits the expensive hypothesis test.
        resolver._cached_identities = [SimpleNamespace(identity_key=None)]

        unrelated = bytes([_FMDN_FRAME_TYPE]) + bytes([0x99] * MODERN_EID_LENGTH)
        matches, matched, observed_frame = resolver._resolve_eid_internal(unrelated)

        assert matches == []
        assert matched is None
        assert observed_frame is None


# ---------------------------------------------------------------------------
# The frame byte does not carry the EID length
# ---------------------------------------------------------------------------


class TestFrameTypeDoesNotDictateEidLength:
    """Slicing by frame byte answers a question the frame byte cannot answer.

    The specification ties ``0x40``/``0x41`` to unwanted tracking protection
    mode, not to the curve. Both mixed shapes therefore occur, and each was
    mis-sliced in its own way: a ``0x41`` frame carrying a legacy EID produced
    no framed candidate at all (so the resolution fell through to the sliding
    window, whose ``frame_type=None`` is invisible to the disagreement
    channel), and a ``0x40`` frame carrying a modern EID matched on its
    20-byte prefix -- a precomputed ``MODERN_P256_X20_TRUNC_*`` entry -- and
    read the hashed-flags byte 12 octets too early.
    """

    _PREFIX = bytes([0x02, 0x01, 0x06, 0x0B, 0x16, 0xAA, 0xFE])

    def _modern_frame_legacy_eid(self, *, flags_byte: int) -> tuple[bytes, bytes]:
        """Build ``[prefix(7)][0x41][legacy EID(20)][flags(1)]``."""
        eid = bytes(range(0x80, 0x80 + LEGACY_EID_LENGTH))
        payload = self._PREFIX + bytes([_MODERN_FRAME_TYPE]) + eid + bytes([flags_byte])
        assert len(payload) == _SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH + 1
        return payload, eid

    def test_modern_frame_with_legacy_eid_decodes_flags_after_the_eid(
        self,
    ) -> None:
        """0x41 + 20-byte EID resolves and reads octet 28 as the flags byte.

        This is a characterisation, not a regression guard: the sliding
        window plus the pre-geometry derivation already produced this exact
        result. It is asserted so that the framed candidate introduced below
        has to reproduce it rather than quietly change it.
        """
        resolver = _make_resolver()
        payload, eid = self._modern_frame_legacy_eid(flags_byte=0x01)
        _register(resolver, eid, "dev-modern-frame-legacy-eid")

        result = resolver.resolve_eid(payload)

        assert result is not None
        assert result.device_id == "dev-modern-frame-legacy-eid"
        state = resolver._ble_battery_state.get("dev-modern-frame-legacy-eid")
        assert state is not None
        assert state.decoded_flags == payload[_SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH]
        assert state.uwt_mode is True

    def test_modern_frame_with_legacy_eid_is_visible_to_the_conflict_channel(
        self,
    ) -> None:
        """The match must carry the frame byte, not the window's ``None``.

        Resolution alone was never the problem here: the sliding window found
        this EID at offset 8 anyway. What it could not do is report which
        frame byte the payload carried, so ``FMDN_FLAGS_CONFLICT`` -- the one
        evidence channel that decides whether 0x41 really implies a 32-byte
        EID in the field -- stayed blind to exactly the population that would
        settle it.
        """
        resolver = _make_resolver()
        payload, eid = self._modern_frame_legacy_eid(flags_byte=0x00)
        _register(resolver, eid, "dev-conflict-visible")

        _matches, matched, observed_frame = resolver._resolve_eid_internal(payload)

        assert matched == eid
        assert observed_frame == _MODERN_FRAME_TYPE

    def test_legacy_frame_with_modern_eid_reads_flags_after_32_bytes(
        self,
    ) -> None:
        """0x40 + 32-byte EID must not match on its own truncated prefix.

        Both entries are registered here because both really are in the
        lookup table: the resolver precomputes ``MODERN_P256_X32_BE`` and its
        20-byte truncation for every device. Whichever candidate is offered
        first therefore decides where the flags byte is read.
        """
        resolver = _make_resolver()
        # Octet 28 (EID byte 20) is the decoy: it decodes to battery level 1
        # with the UWT bit clear, which the real flags byte does not.
        eid = bytes(range(0x40, 0x40 + LEGACY_EID_LENGTH)) + bytes(
            [0x02] + [0x55] * (MODERN_EID_LENGTH - LEGACY_EID_LENGTH - 1)
        )
        assert len(eid) == MODERN_EID_LENGTH
        payload = self._PREFIX + bytes([_FMDN_FRAME_TYPE]) + eid + bytes([0x01])
        _register(resolver, eid, "dev-legacy-frame-modern-eid")
        _register(resolver, eid[:LEGACY_EID_LENGTH], "dev-legacy-frame-modern-eid")

        resolver.resolve_eid(payload)

        state = resolver._ble_battery_state.get("dev-legacy-frame-modern-eid")
        assert state is not None
        assert state.decoded_flags == payload[_SERVICE_DATA_OFFSET + MODERN_EID_LENGTH]
        assert state.uwt_mode is True
        # And specifically not the EID byte the prefix match would have read.
        assert state.decoded_flags != payload[_SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH]

    def test_truncated_modern_frame_is_not_re_read_as_legacy(self) -> None:
        """A 0x41 payload between the two shapes gets no legacy candidate.

        Its octet 28 is EID material, so slicing it as legacy would trade a
        missing candidate for a fabricated flags byte. The sliding window
        still gets its chance.
        """
        resolver = _make_resolver()
        payload = (
            self._PREFIX + bytes([_MODERN_FRAME_TYPE]) + bytes(range(0x20, 0x20 + 26))
        )
        assert (
            _SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH + 1
            < len(payload)
            < _SERVICE_DATA_OFFSET + MODERN_EID_LENGTH
        )

        candidates, _observed = resolver._extract_eid_candidates(payload)

        assert [c for c in candidates if c.layout == "framed"] == []
        assert any(c.layout == "window" for c in candidates)

    def test_legacy_frame_keeps_its_unconditional_20_byte_reading(self) -> None:
        """0x40 must not lose the reading it has always had at any length."""
        resolver = _make_resolver()
        payload = (
            self._PREFIX + bytes([_FMDN_FRAME_TYPE]) + bytes(range(0x20, 0x20 + 26))
        )

        candidates, observed = resolver._extract_eid_candidates(payload)

        framed = [c for c in candidates if c.layout == "framed"]
        assert [(c.offset, len(c.eid)) for c in framed] == [
            (_SERVICE_DATA_OFFSET, LEGACY_EID_LENGTH)
        ]
        assert observed == _FMDN_FRAME_TYPE

    @pytest.mark.parametrize(
        ("frame_type", "payload_len", "expected"),
        [
            # Legacy geometry, exact fit: both frame types read 20 bytes.
            (_FMDN_FRAME_TYPE, 28, (LEGACY_EID_LENGTH,)),
            (_MODERN_FRAME_TYPE, 28, (LEGACY_EID_LENGTH,)),
            (_FMDN_FRAME_TYPE, 29, (LEGACY_EID_LENGTH,)),
            (_MODERN_FRAME_TYPE, 29, (LEGACY_EID_LENGTH,)),
            # Between the shapes: 0x40 keeps its reading, 0x41 gets none.
            (_FMDN_FRAME_TYPE, 30, (LEGACY_EID_LENGTH,)),
            (_MODERN_FRAME_TYPE, 30, ()),
            (_FMDN_FRAME_TYPE, 39, (LEGACY_EID_LENGTH,)),
            (_MODERN_FRAME_TYPE, 39, ()),
            # Modern geometry: longest reading first for both frame types.
            (_FMDN_FRAME_TYPE, 40, (MODERN_EID_LENGTH, LEGACY_EID_LENGTH)),
            (_MODERN_FRAME_TYPE, 40, (MODERN_EID_LENGTH,)),
            (_FMDN_FRAME_TYPE, 41, (MODERN_EID_LENGTH, LEGACY_EID_LENGTH)),
            (_MODERN_FRAME_TYPE, 41, (MODERN_EID_LENGTH,)),
        ],
    )
    def test_service_data_eid_length_table(
        self, frame_type: int, payload_len: int, expected: tuple[int, ...]
    ) -> None:
        """The full band table, so a later edit cannot move a boundary quietly."""
        assert (
            resolver_module._service_data_eid_lengths(frame_type, payload_len)
            == expected
        )
