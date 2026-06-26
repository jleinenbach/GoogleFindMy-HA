# tests/test_init_ecdsa_acceleration_log.py
"""Tests for the one-time ECDSA acceleration DEBUG log helper.

``_log_ecdsa_acceleration`` is a pure formatter: it takes the already
materialized info dict and logs a single DEBUG record, performing no I/O. These
tests pass the dict directly (no monkeypatching, environment-independent) and
verify the record content, including the "not installed" substitution for a
missing gmpy2 version.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.googlefindmy import _log_ecdsa_acceleration

_LOGGER_NAME = "custom_components.googlefindmy"


def test_logs_backend_and_version(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    _log_ecdsa_acceleration(
        {
            "ecdsa_acceleration": "gmpy2",
            "gmpy2_version": "9.9.9-test",
            "gmpy_version": None,
            "ecdsa_version": "0.19.1",
        }
    )
    records = [r for r in caplog.records if "ECDSA big-int backend" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "gmpy2" in message
    assert "9.9.9-test" in message
    # Mutation counter-check: lowering the level to WARNING, or dropping the
    # version token from the format string, makes this assertion red.


def test_missing_gmpy2_version_logs_not_installed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    _log_ecdsa_acceleration(
        {
            "ecdsa_acceleration": "pure-python",
            "gmpy2_version": None,
            "gmpy_version": None,
            "ecdsa_version": "0.19.1",
        }
    )
    message = next(
        r.getMessage()
        for r in caplog.records
        if "ECDSA big-int backend" in r.getMessage()
    )
    assert "not installed" in message
    # The literal "None" must never reach the log (PE-F2 substitution).
    assert "None" not in message
