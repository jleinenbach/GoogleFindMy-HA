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
    SECP160R1,
    SECP256R1,
    ScalarRule,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_K,
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
    decrypt_aes_eax,
    decrypt_foreign_report,
    encrypt,
)
from tests.helpers.fmdn_report_oracle import (
    OracleReport,
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
    rule: ScalarRuleName = (
        "mod_n" if reading.scalar_rule is ScalarRule.MOD_N else "plus1"
    )
    return build_p256_report(
        eik,
        _COUNTER,
        _PLAINTEXT,
        rule=rule,
        nonce_half_len=reading.nonce_half_len,
        ephemeral_scalar=_EPHEMERAL,
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
                scalar_rule=ScalarRule.MOD_N,
                nonce_half_len=half,
                source="https://example.invalid",
                provisional=True,
            )


class TestP256Decryption:
    """Every reading decrypts a report built under it, and only that one wins."""

    @pytest.mark.parametrize("k", [1, 2, 3, 4])
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
        # P-256 reports were "malformed" before; no text bridge, so a P-256
        # total failure cannot drive the all-failed EIK cache invalidation.
        assert "mac" not in str(err).lower()


class TestAttemptCounts:
    """Work per report: PRF per key, ECDH per scalar rule, tag check per reading."""

    @pytest.mark.parametrize("k", [1, 2, 3, 4])
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

    def test_one_ecdh_per_scalar_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        derivations: list[int] = []
        _counting(monkeypatch, foreign_tracker_cryptor, "_shared_material", derivations)
        report = _report_for(4)
        decrypt_foreign_report([_EIK], report.encrypted_and_tag, report.sx, _COUNTER)
        assert len(derivations) == 2

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


class TestSecp160r1Unchanged:
    """SECP160R1 keeps one reading and today's mathematics."""

    def test_auth_failure_keeps_mac_text_bridge(self) -> None:
        eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
        encrypted, sx = encrypt(_PLAINTEXT, bytes(range(20)), eid)
        with pytest.raises(ForeignReportAuthError) as exc_info:
            decrypt_foreign_report([_OTHER_EIK], encrypted, sx, _COUNTER)
        # Bridge for callers that still classify by text until they migrate.
        assert str(exc_info.value).startswith("MAC check failed")
        assert exc_info.value.readings_tried == ("secp160r1/mod_n/nonce8",)

    def test_roundtrip_through_decrypt_foreign_report(self) -> None:
        eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
        encrypted, sx = encrypt(_PLAINTEXT, bytes(range(20)), eid)
        result = decrypt_foreign_report([_EIK], encrypted, sx, _COUNTER)
        assert result.plaintext == _PLAINTEXT
        assert result.reading.reading_id == "secp160r1/mod_n/nonce8"


class TestScalarInvariance:
    """Readings reproduce the stored EID variants; the rule is the only difference."""

    @staticmethod
    def _r_dash(eik: bytes, counter: int) -> int:
        block = build_table10_prf_input(counter, k=FHNA_K)
        return int.from_bytes(prf_aes_256_ecb(eik, block), "big")

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_secp160r1_reading_matches_legacy_variant(
        self, eik: bytes, counter: int
    ) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP160R1,
            self._r_dash(eik, counter),
            ScalarRule.MOD_N,
            SECP160R1.point_x(2),
        )
        assert material is not None
        legacy = generate_eid_variant(eik, counter, EidVariant.LEGACY_SECP160R1_X20_BE)
        assert material[0] == legacy
        assert SECP160R1.point_x(calculate_r(eik, counter)) == legacy

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_p256_plus1_matches_modern_variant(self, eik: bytes, counter: int) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP256R1, self._r_dash(eik, counter), ScalarRule.PLUS_ONE, p256_x(2)
        )
        assert material is not None
        assert material[0] == generate_eid_variant(
            eik, counter, EidVariant.MODERN_P256_X32_BE
        )

    @pytest.mark.parametrize(("eik", "counter"), _INVARIANCE_PAIRS)
    def test_p256_mod_n_matches_oracle(self, eik: bytes, counter: int) -> None:
        material = foreign_tracker_cryptor._shared_material(
            SECP256R1, self._r_dash(eik, counter), ScalarRule.MOD_N, p256_x(2)
        )
        assert material is not None
        assert material[0] == p256_x(owner_scalar(eik, counter, "mod_n"))
