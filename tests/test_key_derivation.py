# tests/test_key_derivation.py
"""Tests for the owner key derivation helpers."""

from __future__ import annotations

import hashlib

import pytest

from custom_components.googlefindmy.FMDNCrypto import key_derivation
from custom_components.googlefindmy.FMDNCrypto.key_derivation import FMDNOwnerOperations


def test_generate_keys_populates_owner_keys() -> None:
    """The derived keys should match the SHA-256 prefixes."""

    operations = FMDNOwnerOperations()
    identity_key = b"\x01" * 32

    operations.generate_keys(identity_key)

    expected_recovery = hashlib.sha256(identity_key + bytes([0x01])).digest()[:8]
    expected_ringing = hashlib.sha256(identity_key + bytes([0x02])).digest()[:8]
    expected_tracking = hashlib.sha256(identity_key + bytes([0x03])).digest()[:8]

    assert operations.recovery_key == expected_recovery
    assert operations.ringing_key == expected_ringing
    assert operations.tracking_key == expected_tracking


def test_generate_keys_rejects_non_bytes() -> None:
    """Non-bytes identity keys should raise the documented TypeError."""

    operations = FMDNOwnerOperations()

    with pytest.raises(TypeError) as excinfo:
        operations.generate_keys("not-bytes")  # type: ignore[arg-type]

    assert str(excinfo.value) == "Identity key must be a bytes-like object, got str"


def test_generate_keys_failure_resets_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures during derivation should clear state and log the error."""

    def raise_error(identity_key: bytes, operation: int) -> bytes:  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(
        key_derivation,
        "calculate_truncated_sha256",
        raise_error,
        raising=True,
    )

    operations = FMDNOwnerOperations()
    operations.recovery_key = b"existing"
    operations.ringing_key = b"existing"
    operations.tracking_key = b"existing"

    with caplog.at_level("ERROR"):
        operations.generate_keys(b"identity")

    assert operations.recovery_key is None
    assert operations.ringing_key is None
    assert operations.tracking_key is None
    assert any(
        "Failed to derive owner operation keys" in record.getMessage()
        for record in caplog.records
    )
