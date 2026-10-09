# tests/test_foreign_tracker_cryptor_p256.py
"""Tests for multi-reading decryption of foreign reports (SECP160R1 and P-256).

P-256 reports come from ``tests.helpers.fmdn_report_oracle``, which rebuilds
the finder side from the specification without importing the integration.
Counting tests wrap production functions and count calls; every zero count is
paired with a positive case in which the same counter reaches at least one.
Readings are counted from 1 as in the plan: reading k is
``P256_FOREIGN_READINGS[k - 1]``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from Cryptodome.Cipher import AES
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.googlefindmy.FMDNCrypto import (
    eid_generator,
    foreign_tracker_cryptor,
)
from custom_components.googlefindmy.FMDNCrypto.curve_profile import (
    BE_MOD_N,
    BE_PLUS_ONE,
    LE_PLUS_ONE,
    SECP160R1,
    SECP256R1,
    ScalarRule,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_K,
    VARIANT_DERIVATIONS,
    EidVariant,
    build_table10_prf_input,
    generate_eid_variant,
    prf_aes_256_ecb,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_report_errors import (
    ForeignReportAuthError,
    ForeignReportStructureError,
    UnsupportedCurveError,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    FOREIGN_READING_FEEDBACK_URL,
    P256_FOREIGN_READINGS,
    READINGS_BY_CURVE,
    SECP160R1_FOREIGN_READINGS,
    ForeignReading,
    calculate_r,
    decrypt,
    decrypt_aes_eax,
    decrypt_foreign_report,
    encrypt,
    encrypt_aes_eax,
)
from tests.helpers.fmdn_report_oracle import (
    OracleReport,
    PrfByteOrderName,
    ScalarRuleName,
    build_p256_report,
    ephemeral_scalar_with_y_parity,
    owner_scalar,
    p256_x,
)

_EIK = bytes(range(32))
_OTHER_EIK = bytes(range(32, 64))
_COUNTER = 1_000_000
_PLAINTEXT = b"lat/lon payload"
_EPHEMERAL = 0x1234_5678_9ABC_DEF0

_EXPECTED_P256_IDS = (
    "p256/mod_n/nonce8",
    "p256/mod_n/nonce10",
    "p256/plus1/nonce8",
    "p256/plus1/nonce10",
    "p256/plus1_le/nonce8",
    "p256/plus1_le/nonce10",
)
_READING_NUMBERS = [1, 2, 3, 4, 5, 6]

# Variants whose EID is a truncated P-256 x-coordinate: a finder cannot run a
# P-256 ECDH from 20 of 32 bytes, so their reports are not decryptable
# (see test_truncated_p256_variant_reports_are_undecryptable).
_TRUNCATED_VARIANTS = frozenset(
    {
        EidVariant.MODERN_P256_X20_TRUNC_BE,
        EidVariant.MODERN_P256_X20_TRUNC_LE,
        EidVariant.SPEC_P256_X20_TRUNC_BE,
    }
)

# Sixteen fixed (EIK, counter) pairs, including unaligned and boundary counters.
_INVARIANCE_PAIRS = [
    (bytes([(7 * i + j) % 256 for j in range(32)]), counter)
    for i, counter in enumerate(
        (
            0,
            1,
            1023,
            1024,
            1025,
            65_535,
            1_000_000,
            123_456_789,
            0x7FFF_FFFF,
            0x8000_0000,
            0xDEAD_BEEF,
            0xFFFF_FC00,
            0xFFFF_FFFF,
            42,
            99_999,
            3_141_592_653,
        )
    )
]


def _report_for(k: int, eik: bytes = _EIK) -> OracleReport:
    reading = P256_FOREIGN_READINGS[k - 1]
    derivation = reading.derivation
    rule: ScalarRuleName = "mod_n" if derivation.rule is ScalarRule.MOD_N else "plus1"
    byteorder: PrfByteOrderName = (
        "little" if derivation.byteorder == "little" else "big"
    )
    return build_p256_report(
        eik,
        _COUNTER,
        _PLAINTEXT,
        rule=rule,
        nonce_half_len=reading.nonce_half_len,
        ephemeral_scalar=_EPHEMERAL,
        r_dash_byteorder=byteorder,
    )


def _counting(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, counter: list[int]
) -> None:
    original: Callable[..., Any] = getattr(owner, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        counter.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)


def _off_curve_p256_x() -> bytes:
    for x in range(1, 100):
        encoded = b"\x02" + x.to_bytes(32, "big")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), encoded)
        except ValueError:
            return x.to_bytes(32, "big")
    raise AssertionError("no off-curve x below 100")  # pragma: no cover


class TestReadingRegistry:
    """The reading set is closed, named, ordered and sourced."""

    def test_p256_ids_and_order(self) -> None:
        assert tuple(r.reading_id for r in P256_FOREIGN_READINGS) == _EXPECTED_P256_IDS

    def test_p256_readings_are_provisional_and_on_p256(self) -> None:
        assert all(r.provisional for r in P256_FOREIGN_READINGS)
        assert all(r.curve is SECP256R1 for r in P256_FOREIGN_READINGS)

    def test_sources_are_public_urls_or_commits(self) -> None:
        for readings in READINGS_BY_CURVE.values():
            for reading in readings:
                assert reading.source.startswith(("https://", "commit "))

    def test_specification_is_cited_only_for_its_literal_nonce(self) -> None:
        """The spec text says "lower 80 bits"; 8-byte halves cite the paper."""
        spec = "https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn"
        paper = "https://petsymposium.org/popets/2025/popets-2025-0147.pdf"
        for readings in READINGS_BY_CURVE.values():
            for reading in readings:
                if reading.source == spec:
                    assert reading.nonce_half_len == 10, reading.reading_id
        by_id = {
            r.reading_id: r.source for rs in READINGS_BY_CURVE.values() for r in rs
        }
        assert by_id["p256/mod_n/nonce8"] == paper
        assert by_id["secp160r1/mod_n/nonce8"] == paper
        assert by_id["p256/mod_n/nonce10"] == spec

    def test_secp160r1_has_exactly_one_reading(self) -> None:
        ids = tuple(r.reading_id for r in READINGS_BY_CURVE["secp160r1"])
        assert ids == ("secp160r1/mod_n/nonce8",)
        assert READINGS_BY_CURVE["secp160r1"] is SECP160R1_FOREIGN_READINGS
        assert not SECP160R1_FOREIGN_READINGS[0].provisional

    def test_registry_keyed_by_curve_name(self) -> None:
        assert set(READINGS_BY_CURVE) == {SECP160R1.name, SECP256R1.name}

    def test_feedback_url_is_issue_223(self) -> None:
        assert FOREIGN_READING_FEEDBACK_URL == (
            "https://github.com/BSkando/GoogleFindMy-HA/issues/223"
        )

    @pytest.mark.parametrize("half", [0, 33])
    def test_nonce_half_len_must_fit_coordinate(self, half: int) -> None:
        with pytest.raises(ValueError, match="nonce_half_len must lie in"):
            ForeignReading(
                curve=SECP256R1,
                derivation=BE_MOD_N,
                nonce_half_len=half,
                source="https://example.invalid",
                provisional=True,
            )


class TestP256Decryption:
    """Every reading decrypts a report built under it, and only that one wins."""

    @pytest.mark.parametrize("k", _READING_NUMBERS)
    def test_winner_is_reading_k(self, k: int) -> None:
        report = _report_for(k)
        result = decrypt_foreign_report(
            [_EIK], report.encrypted_and_tag, report.sx, _COUNTER
        )
        assert result.plaintext == _PLAINTEXT
        assert result.key_index == 0
        assert result.reading.reading_id == P256_FOREIGN_READINGS[k - 1].reading_id

    @pytest.mark.parametrize("odd_y", [False, True])
    def test_sy_parity_does_not_matter(self, odd_y: bool) -> None:
        report = build_p256_report(
            _EIK,
            _COUNTER,
            _PLAINTEXT,
            rule="mod_n",
            nonce_half_len=8,
            ephemeral_scalar=ephemeral_scalar_with_y_parity(odd_y=odd_y),
        )
        assert report.sy_prefix == (b"\x03" if odd_y else b"\x02")
        result = decrypt_foreign_report(
            [_EIK], report.encrypted_and_tag, report.sx, _COUNTER
        )
        assert result.plaintext == _PLAINTEXT

    def test_matching_key_index_is_reported(self) -> None:
        report = _report_for(1)
        result = decrypt_foreign_report(
            [_OTHER_EIK, bytes(32), _EIK], report.encrypted_and_tag, report.sx, _COUNTER
        )
        assert result.key_index == 2

    def test_wrong_key_raises_aggregated_auth_error(self) -> None:
        report = _report_for(1)
        with pytest.raises(ForeignReportAuthError) as exc_info:
            decrypt_foreign_report(
                [_OTHER_EIK, bytes(32)], report.encrypted_and_tag, report.sx, _COUNTER
            )
        err = exc_info.value
        assert err.curve_name == "secp256r1"
        assert err.readings_tried == _EXPECTED_P256_IDS
        assert err.keys_tried == 2
        assert isinstance(err, ValueError)
        # Callers classify by type (decrypt_locations counts P-256 failures
        # apart, Z11); the message carries no text a caller could match on.
        assert "mac" not in str(err).lower()


class TestAttemptCounts:
    """Work per report: PRF per key, ECDH per derivation, tag check per reading."""

    @pytest.mark.parametrize("k", _READING_NUMBERS)
    def test_first_report_needs_k_checks_then_one(
        self, monkeypatch: pytest.MonkeyPatch, k: int
    ) -> None:
        checks: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "_try_decrypt_aes_eax", checks)
        report = _report_for(k)

        first = decrypt_foreign_report(
            [_EIK], report.encrypted_and_tag, report.sx, _COUNTER
        )
        assert len(checks) == k

        checks.clear()
        second = decrypt_foreign_report(
            [_EIK],
            report.encrypted_and_tag,
            report.sx,
            _COUNTER,
            preferred_reading_id=first.reading.reading_id,
        )
        assert len(checks) == 1
        assert second.reading == first.reading

    def test_unknown_preferred_id_keeps_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checks: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "_try_decrypt_aes_eax", checks)
        report = _report_for(3)
        decrypt_foreign_report(
            [_EIK],
            report.encrypted_and_tag,
            report.sx,
            _COUNTER,
            preferred_reading_id="p256/unknown/nonce8",
        )
        assert len(checks) == 3

    def test_one_ecdh_per_derivation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        derivations: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "_shared_material", derivations)
        report = _report_for(6)
        decrypt_foreign_report([_EIK], report.encrypted_and_tag, report.sx, _COUNTER)
        # Six readings share three derivations: BE mod n, BE +1, LE +1.
        assert len(derivations) == 3

    def test_prf_once_per_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        prf_calls: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "prf_aes_256_ecb", prf_calls)
        report = _report_for(1)
        decrypt_foreign_report(
            [_OTHER_EIK, bytes(32), _EIK], report.encrypted_and_tag, report.sx, _COUNTER
        )
        assert len(prf_calls) == 3

    def test_zero_scalar_skips_the_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = foreign_tracker_cryptor.reduce_scalar

        def zero_for_mod_n(r_dash_int: int, order: int, rule: ScalarRule) -> int:
            if rule is ScalarRule.MOD_N:
                return 0
            return original(r_dash_int, order, rule)

        monkeypatch.setattr(foreign_tracker_cryptor, "reduce_scalar", zero_for_mod_n)
        checks: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "_try_decrypt_aes_eax", checks)
        report = _report_for(3)
        result = decrypt_foreign_report(
            [_EIK], report.encrypted_and_tag, report.sx, _COUNTER
        )
        assert result.reading.reading_id == "p256/plus1/nonce8"
        assert len(checks) == 1


class TestStructureBeforeKeys:
    """Structure errors are raised once, before any key candidate is used."""

    @staticmethod
    def _count_prf(monkeypatch: pytest.MonkeyPatch) -> list[int]:
        calls: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "prf_aes_256_ecb", calls)
        _counting(monkeypatch, eid_generator, "prf_aes_256_ecb", calls)
        return calls

    def test_unsupported_length_uses_no_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._count_prf(monkeypatch)
        keys = [_EIK, _OTHER_EIK, bytes(32), bytes([1]) * 32]
        with pytest.raises(UnsupportedCurveError) as exc_info:
            decrypt_foreign_report(keys, bytes(32), bytes(5), _COUNTER)
        assert exc_info.value.sx_len == 5
        assert calls == []

        # Same counter, positive case: a valid report does reach the PRF.
        report = _report_for(1)
        decrypt_foreign_report([_EIK], report.encrypted_and_tag, report.sx, _COUNTER)
        assert len(calls) >= 1

    def test_short_payload_uses_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._count_prf(monkeypatch)
        report = _report_for(1)
        with pytest.raises(ForeignReportStructureError, match="at least 16 bytes"):
            decrypt_foreign_report([_EIK], bytes(15), report.sx, _COUNTER)
        assert calls == []

        # Same counter, positive case: a valid report does reach the PRF.
        valid = _report_for(1)
        decrypt_foreign_report([_EIK], valid.encrypted_and_tag, valid.sx, _COUNTER)
        assert len(calls) >= 1

    def test_off_curve_sx_uses_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._count_prf(monkeypatch)
        with pytest.raises(ForeignReportStructureError, match="not on SECP256R1"):
            decrypt_foreign_report([_EIK], bytes(40), _off_curve_p256_x(), _COUNTER)
        assert calls == []

        # Same counter, positive case: a valid report does reach the PRF.
        valid = _report_for(1)
        decrypt_foreign_report([_EIK], valid.encrypted_and_tag, valid.sx, _COUNTER)
        assert len(calls) >= 1

    def test_empty_key_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one key"):
            decrypt_foreign_report([], bytes(40), bytes(32), _COUNTER)

    def test_short_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="identity_key must be exactly 32 bytes"):
            decrypt_foreign_report([_EIK, bytes(20)], bytes(40), bytes(32), _COUNTER)


class TestAesEaxWrapper:
    """decrypt_aes_eax accepts every reading's nonce length and signals tag failure."""

    def test_rejects_nonce_length_of_no_reading(self) -> None:
        with pytest.raises(ValueError, match=r"nonce must be one of \[16, 20\] bytes"):
            decrypt_aes_eax(b"", bytes(16), bytes(12), bytes(32))

    @pytest.mark.parametrize("nonce_len", [16, 20])
    def test_public_wrapper_roundtrip(self, nonce_len: int) -> None:
        key = bytes(range(32))
        nonce = bytes(range(nonce_len))
        cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(_PLAINTEXT)
        assert decrypt_aes_eax(ciphertext, tag, nonce, key) == _PLAINTEXT

    def test_public_wrapper_raises_on_wrong_tag(self) -> None:
        with pytest.raises(ValueError, match="MAC check failed"):
            decrypt_aes_eax(b"abc", bytes(16), bytes(20), bytes(32))

    def test_try_helper_returns_none_on_wrong_tag(self) -> None:
        try_decrypt = foreign_tracker_cryptor._try_decrypt_aes_eax
        assert try_decrypt(b"abc", bytes(16), bytes(20), bytes(32)) is None

    def test_try_helper_still_raises_on_bad_length(self) -> None:
        try_decrypt = foreign_tracker_cryptor._try_decrypt_aes_eax
        with pytest.raises(ValueError, match="key must be exactly 32 bytes"):
            try_decrypt(b"abc", bytes(16), bytes(20), bytes(16))


class TestEncrypt:
    """Finder side: curve from ``len(eid)``, nonce width from the reading.

    Expected bytes come from ``build_p256_report`` (the oracle), not from
    ``encrypt`` itself.
    """

    @pytest.mark.parametrize("k", _READING_NUMBERS)
    def test_matches_oracle_report(self, k: int) -> None:
        report = _report_for(k)
        encrypted, sx = encrypt(
            _PLAINTEXT,
            _EPHEMERAL.to_bytes(32, "big"),
            report.rx,
            reading=P256_FOREIGN_READINGS[k - 1],
        )
        assert (encrypted, sx) == (report.encrypted_and_tag, report.sx)

    @pytest.mark.parametrize("k", _READING_NUMBERS)
    def test_roundtrip_selects_the_encrypting_reading(self, k: int) -> None:
        report = _report_for(k)
        encrypted, sx = encrypt(
            _PLAINTEXT,
            bytes(range(1, 33)),
            report.rx,
            reading=P256_FOREIGN_READINGS[k - 1],
        )
        result = decrypt_foreign_report([_EIK], encrypted, sx, _COUNTER)
        assert result.plaintext == _PLAINTEXT
        assert result.reading.reading_id == _EXPECTED_P256_IDS[k - 1]

    def test_default_is_first_reading_of_the_curve(self) -> None:
        report = _report_for(1)
        random = _EPHEMERAL.to_bytes(32, "big")
        default = encrypt(_PLAINTEXT, random, report.rx)
        assert default == (report.encrypted_and_tag, report.sx)
        assert len(default[1]) == 32
        eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
        assert encrypt(_PLAINTEXT, random, eid) == encrypt(
            _PLAINTEXT, random, eid, reading=SECP160R1_FOREIGN_READINGS[0]
        )

    def test_rejects_reading_of_other_curve(self) -> None:
        eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
        with pytest.raises(ValueError, match="does not apply to a 20-byte EID"):
            encrypt(_PLAINTEXT, bytes(32), eid, reading=P256_FOREIGN_READINGS[0])

    def test_rejects_eid_length_of_no_curve(self) -> None:
        # The message speaks of the EID, not of a report's Sx; the decrypt-side
        # error stays reachable as the cause.
        with pytest.raises(
            ValueError, match=r"eid has 21 bytes; expected one of \[20, 32\]"
        ) as exc_info:
            encrypt(_PLAINTEXT, bytes(32), bytes(21))
        assert type(exc_info.value) is ValueError
        assert isinstance(exc_info.value.__cause__, UnsupportedCurveError)
        assert "Sx" not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("eid_factory", "curve_name"),
        [
            (_off_curve_p256_x, "secp256r1"),
            (lambda: ((1 << 160) - 1).to_bytes(20, "big"), "secp160r1"),
            (lambda: (3).to_bytes(20, "big"), "secp160r1"),
        ],
        ids=["p256_not_on_curve", "secp160r1_above_p", "secp160r1_not_on_curve"],
    )
    def test_rejects_off_curve_eid(
        self, eid_factory: Callable[[], bytes], curve_name: str
    ) -> None:
        with pytest.raises(
            ValueError, match=f"eid is not an x-coordinate on {curve_name}"
        ) as exc_info:
            encrypt(_PLAINTEXT, bytes(32), eid_factory())
        assert type(exc_info.value) is ValueError
        assert isinstance(exc_info.value.__cause__, ForeignReportStructureError)
        assert "Sx" not in str(exc_info.value)

    def test_zero_scalar_is_bumped_to_one(self) -> None:
        report = _report_for(1)
        _, sx = encrypt(_PLAINTEXT, SECP256R1.order.to_bytes(32, "big"), report.rx)
        assert sx == p256_x(1)

    @pytest.mark.parametrize("nonce_len", [12, 17])
    def test_aes_eax_rejects_nonce_length_of_no_reading(self, nonce_len: int) -> None:
        with pytest.raises(ValueError, match=r"nonce must be one of \[16, 20\] bytes"):
            encrypt_aes_eax(_PLAINTEXT, bytes(nonce_len), bytes(32))

    @pytest.mark.parametrize("nonce_len", [16, 20])
    def test_aes_eax_accepts_every_reading_nonce(self, nonce_len: int) -> None:
        key = bytes(range(32))
        nonce = bytes(range(nonce_len))
        ciphertext, tag = encrypt_aes_eax(_PLAINTEXT, nonce, key)
        assert decrypt_aes_eax(ciphertext, tag, nonce, key) == _PLAINTEXT


class TestSecp160r1Unchanged:
    """SECP160R1 keeps one reading and today's mathematics."""

    def test_auth_failure_message_carries_no_mac_text(self) -> None:
        eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
        encrypted, sx = encrypt(_PLAINTEXT, bytes(range(20)), eid)
        with pytest.raises(ForeignReportAuthError) as exc_info:
            decrypt_foreign_report([_OTHER_EIK], encrypted, sx, _COUNTER)
        # Callers classify by type; no text a caller could still match on.
        assert "mac" not in str(exc_info.value).lower()
        assert exc_info.value.readings_tried == ("secp160r1/mod_n/nonce8",)

    def test_roundtrip_through_decrypt_foreign_report(self) -> None:
        eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
        encrypted, sx = encrypt(_PLAINTEXT, bytes(range(20)), eid)
        result = decrypt_foreign_report([_EIK], encrypted, sx, _COUNTER)
        assert result.plaintext == _PLAINTEXT
        assert result.reading.reading_id == "secp160r1/mod_n/nonce8"


class TestScalarInvariance:
    """Readings reproduce the stored EID variants; the derivation is the only difference."""

    @staticmethod
    def _r_dash(eik: bytes, counter: int) -> bytes:
        block = build_table10_prf_input(counter, k=FHNA_K)
        return prf_aes_256_ecb(eik, block)

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_secp160r1_reading_matches_legacy_variant(
        self, eik: bytes, counter: int
    ) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP160R1,
            self._r_dash(eik, counter),
            BE_MOD_N,
            SECP160R1.point_x(2),
        )
        assert material is not None
        legacy = generate_eid_variant(eik, counter, EidVariant.LEGACY_SECP160R1_X20_BE)
        assert material[0] == legacy
        assert SECP160R1.point_x(calculate_r(eik, counter)) == legacy

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_p256_plus1_matches_modern_variant(self, eik: bytes, counter: int) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP256R1, self._r_dash(eik, counter), BE_PLUS_ONE, p256_x(2)
        )
        assert material is not None
        assert material[0] == generate_eid_variant(
            eik, counter, EidVariant.MODERN_P256_X32_BE
        )

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_p256_mod_n_matches_oracle(self, eik: bytes, counter: int) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP256R1, self._r_dash(eik, counter), BE_MOD_N, p256_x(2)
        )
        assert material is not None
        assert material[0] == p256_x(owner_scalar(eik, counter, "mod_n"))
        assert material[0] == generate_eid_variant(
            eik, counter, EidVariant.SPEC_P256_X32_BE
        )

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_p256_plus1_le_matches_le_variant(self, eik: bytes, counter: int) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP256R1, self._r_dash(eik, counter), LE_PLUS_ONE, p256_x(2)
        )
        assert material is not None
        assert material[0] == generate_eid_variant(
            eik, counter, EidVariant.MODERN_P256_X32_LE_SCALAR
        )
        assert material[0] == p256_x(
            owner_scalar(eik, counter, "plus1", r_dash_byteorder="little")
        )


class TestReadingsBoundToVariants:
    """Every scalar derivation an EID variant uses can decrypt its reports."""

    def test_every_full_variant_derivation_has_a_reading(self) -> None:
        """A tracker the resolver can lock to a variant must have a reading.

        Full variants (EID length equal to the curve's coordinate length) are
        checked; the truncated P-256 variants are the named exclusions.
        """
        checked: set[EidVariant] = set()
        for variant, (curve, derivation) in VARIANT_DERIVATIONS.items():
            eid_len = len(generate_eid_variant(_EIK, 0, variant))
            if eid_len != curve.coord_len:
                assert variant in _TRUNCATED_VARIANTS, variant
                continue
            reading_derivations = {
                reading.derivation for reading in READINGS_BY_CURVE[curve.name]
            }
            assert derivation in reading_derivations, (
                f"{variant.value}: no reading for {derivation}"
            )
            checked.add(variant)
        assert checked == set(VARIANT_DERIVATIONS) - _TRUNCATED_VARIANTS
        assert checked

    @pytest.mark.parametrize("variant", sorted(_TRUNCATED_VARIANTS))
    def test_truncated_p256_variant_reports_are_undecryptable(
        self, variant: EidVariant
    ) -> None:
        """A report built for a truncated P-256 EID never decrypts.

        The 20-byte EID is used as a SECP160R1 x-coordinate by the finder;
        about half of the counters give no curve point at all. Every report
        that can be built lands on SECP160R1 and fails authentication.
        """
        eik = bytes(range(32))
        built = 0
        for counter in range(0, 40 * 1024, 1024):
            eid = generate_eid_variant(eik, counter, variant)
            assert len(eid) == SECP160R1.coord_len
            try:
                encrypted_and_tag, sx = encrypt(b"x" * 20, bytes(range(20)), eid)
            except ValueError:
                continue  # truncated x is not a SECP160R1 point
            built += 1
            with pytest.raises(ForeignReportAuthError):
                decrypt_foreign_report([eik], encrypted_and_tag, sx, counter)
            with pytest.raises(ForeignReportAuthError):
                decrypt(eik, encrypted_and_tag, sx, counter)
        assert built >= 1
