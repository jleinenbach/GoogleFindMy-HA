# tests/test_key_derivation.py
"""Tests for the owner key derivation helpers."""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.FMDNCrypto import key_derivation


def test_generate_keys_invokes_all_derivations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each derivation is requested with the correct inputs."""

    calls: list[tuple[bytes, int]] = []

    def fake_calculate(identity_key: bytes, constant: int) -> bytes:
        calls.append((identity_key, constant))
        return f"derived-{constant}".encode()

    monkeypatch.setattr(
        key_derivation,
        "calculate_truncated_sha256",
        fake_calculate,
        raising=True,
    )

    operations = key_derivation.FMDNOwnerOperations()
    identity_key = b"identity-key"

    operations.generate_keys(identity_key)

    assert calls == [
        (identity_key, 0x01),
        (identity_key, 0x02),
        (identity_key, 0x03),
    ]
    assert operations.recovery_key == b"derived-1"
    assert operations.ringing_key == b"derived-2"
    assert operations.tracking_key == b"derived-3"


def test_generate_keys_failure_resets_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Simulate a derivation failure and ensure keys reset and are logged."""

    attempts: list[int] = []

    def failing_calculate(identity_key: bytes, constant: int) -> bytes:
        attempts.append(constant)
        if constant == 0x02:
            raise RuntimeError("boom")
        return b"ok"

    monkeypatch.setattr(
        key_derivation,
        "calculate_truncated_sha256",
        failing_calculate,
        raising=True,
    )

    operations = key_derivation.FMDNOwnerOperations()
    operations.recovery_key = b"existing"
    operations.ringing_key = b"existing"
    operations.tracking_key = b"existing"

    with caplog.at_level("ERROR"):
        operations.generate_keys(b"identity")

    assert attempts == [0x01, 0x02]
    assert operations.recovery_key is None
    assert operations.ringing_key is None
    assert operations.tracking_key is None
    assert any(
        "Failed to derive owner operation keys" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("invalid", [None, "string", 123, [1, 2, 3]])
def test_generate_keys_requires_bytes(invalid: object) -> None:
    """Non-bytes inputs should raise a helpful TypeError."""

    operations = key_derivation.FMDNOwnerOperations()

    with pytest.raises(TypeError) as excinfo:
        operations.generate_keys(invalid)  # type: ignore[arg-type]

    assert (
        "Identity key must be a bytes-like object"
        in str(excinfo.value)
    )
