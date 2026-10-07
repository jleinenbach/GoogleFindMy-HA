# tests/test_foreign_tracker_cryptor_regression_223.py
"""Regression test for issue 223: P-256 foreign reports must decrypt.

Crowdsourced reports for P-256 trackers carry a 32-byte ``Sx``; the cryptor
used to reject them with ``Sx must be exactly 20 bytes (got 32)``. This file
imports only the public ``decrypt`` function (unchanged signature) and the
independent test oracle, so it can also be collected against a tree without
the fix, where it fails at the length check and not at an import.

Issue: https://github.com/BSkando/GoogleFindMy-HA/issues/223
"""

from __future__ import annotations

from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import decrypt
from tests.helpers.fmdn_report_oracle import build_p256_report

_EIK = bytes(range(32))
_COUNTER = 1_000_000
_PLAINTEXT = b"lat/lon payload for issue 223"


def test_regression_223_p256_sx32_decrypts() -> None:
    """A report built by the specification's reading decrypts through decrypt()."""
    report = build_p256_report(
        _EIK,
        _COUNTER,
        _PLAINTEXT,
        rule="mod_n",
        nonce_half_len=8,
        ephemeral_scalar=0x1234_5678_9ABC_DEF0,
    )
    assert len(report.sx) == 32

    assert decrypt(_EIK, report.encrypted_and_tag, report.sx, _COUNTER) == _PLAINTEXT
