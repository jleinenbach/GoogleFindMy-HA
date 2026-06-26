# tests/test_lskf_hasher.py
"""Coverage + parameter lock for ``KeyBackup/lskf_hasher.py``.

This module freezes the contract of the LSKF (Lock Screen Knowledge Factor)
Scrypt key-derivation surface used to bootstrap the Google Find My key-backup
chain. The single public derivation (``get_lskf_hash``) is exercised against the
**real** ``pyscrypt`` library; the SHA-256 wrapper (``hash_pin``) is mocked for
speed and determinism.

Highlights
----------
- ``L-1`` is a *parameter lock* (contract / interop lock), **not** a
  bug-discriminating regression lock: ``lskf_hasher.py`` has no historical
  crypto bugfix in its git history. The exact digest for
  ``(pin="1234", salt=0123…cdef, N=4096, r=8, p=1, dkLen=32)`` is frozen so that
  any silent change of the Scrypt parameters — which must match Google's
  server-side LSKF derivation — breaks the test. The expected value is produced
  by an **independent** oracle (``hashlib.scrypt`` / OpenSSL), never mirrored
  from ``pyscrypt`` itself, so the test cannot be circular (audit finding F-1).
- ``CT-1 … CT-8`` cover ``ascii_to_bytes``, ``get_lskf_hash`` and ``hash_pin``
  (length/type contract, input sensitivity, the SHA-256 envelope, the print
  side effect, the ``TypeError`` safety net, and the empty-salt example path).

Notes
-----
- ``CT-3``/``CT-4`` only assert *input sensitivity* (salt/PIN flow into the
  digest), so they replace the hardcoded N=4096 with a cheap N=2 via a
  delegating patch (audit finding F-2) instead of running two expensive 4 MiB
  ROMix passes each.
- The ``__main__`` brute-force harness is coverage-excluded (pyproject) and is
  intentionally not tested (negative protocol N-4).
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

import pyscrypt
from custom_components.googlefindmy.KeyBackup import lskf_hasher

# ---------------------------------------------------------------------------
# Known-Answer-Test constants (the interop lock) — kept as named symbols so the
# lock is self-documenting and AP-3 can grep them by string, not line number.
# ---------------------------------------------------------------------------
KAT_PIN = "1234"
KAT_SALT = bytes.fromhex("0123456789abcdef0123456789abcdef")
KAT_N = 4096
KAT_R = 8
KAT_P = 1
KAT_DKLEN = 32

# Independent oracle digest (hashlib.scrypt / OpenSSL), self-verified 2026-06-09.
# Re-derivable with:
#   hashlib.scrypt(b"1234", salt=KAT_SALT, n=4096, r=8, p=1, dklen=32).hex()
KAT_EXPECTED_DIGEST = "3336935b371bf5698d7ca9cd0d5b98adfce195dd9772e41d327bd093f6151284"

# Real ``pyscrypt.hash`` captured before any monkeypatch, so the delegating
# fast-hash in CT-3/CT-4 cannot recurse into its own patch.
_REAL_PYSCRYPT_HASH = pyscrypt.hash


# ---------------------------------------------------------------------------
# CT-1 — ascii_to_bytes
# ---------------------------------------------------------------------------
def test_ct1_ascii_to_bytes_roundtrip() -> None:
    """ASCII strings encode to their byte representation."""
    assert lskf_hasher.ascii_to_bytes("1234") == b"1234"
    assert lskf_hasher.ascii_to_bytes("") == b""


def test_ct1_ascii_to_bytes_rejects_non_ascii() -> None:
    """Non-ASCII input (incl. Unicode digits) raises — documented as-is."""
    with pytest.raises(UnicodeEncodeError):
        lskf_hasher.ascii_to_bytes("ä")
    with pytest.raises(UnicodeEncodeError):
        lskf_hasher.ascii_to_bytes("１")  # noqa: RUF001 — fullwidth digit, intentional


# ---------------------------------------------------------------------------
# L-1 — parameter lock (real pyscrypt, single call, independent oracle)
# ---------------------------------------------------------------------------
def test_l1_parameter_lock_kat() -> None:
    """Freeze the exact Scrypt digest against an independent hashlib oracle.

    A failure here means either the SUT's hardcoded parameters drifted
    (breaking interop with Google) or ``pyscrypt`` stopped being RFC-7914
    conformant. The expected value comes from ``hashlib.scrypt``, never from
    ``pyscrypt`` (no circularity, F-1).
    """
    digest = lskf_hasher.get_lskf_hash(KAT_PIN, KAT_SALT)

    assert digest.hex() == KAT_EXPECTED_DIGEST
    # Independent re-derivation in the same process, for defence in depth.
    oracle = hashlib.scrypt(
        KAT_PIN.encode("ascii"),
        salt=KAT_SALT,
        n=KAT_N,
        r=KAT_R,
        p=KAT_P,
        dklen=KAT_DKLEN,
    )
    assert digest == oracle


# ---------------------------------------------------------------------------
# CT-2 — get_lskf_hash length/type contract (shares the L-1 real call's params)
# ---------------------------------------------------------------------------
def test_ct2_get_lskf_hash_length_and_type() -> None:
    """The derived key is exactly 32 raw bytes."""
    digest = lskf_hasher.get_lskf_hash(KAT_PIN, KAT_SALT)
    assert isinstance(digest, bytes)
    assert len(digest) == KAT_DKLEN


# ---------------------------------------------------------------------------
# CT-3 / CT-4 — input sensitivity with cheap N=2 (delegating patch, F-2)
# ---------------------------------------------------------------------------
def _fast_hash(
    *, password: bytes, salt: bytes, N: int, r: int, p: int, dkLen: int
) -> bytes:
    """Delegate to the real pyscrypt but with cheap params (ignores N=4096/r=8).

    Used only to prove that ``get_lskf_hash`` forwards its inputs into the
    digest; interop fidelity is covered by L-1, not here.
    """
    return _REAL_PYSCRYPT_HASH(password=password, salt=salt, N=2, r=1, p=p, dkLen=dkLen)


def test_ct3_salt_changes_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different salts yield different derived keys (salt flows into the hash)."""
    monkeypatch.setattr(lskf_hasher.pyscrypt, "hash", _fast_hash)
    out_a = lskf_hasher.get_lskf_hash(KAT_PIN, b"\x00" * 16)
    out_b = lskf_hasher.get_lskf_hash(KAT_PIN, b"\xff" * 16)
    assert out_a != out_b


def test_ct4_pin_changes_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different PINs yield different derived keys (password flows into the hash)."""
    monkeypatch.setattr(lskf_hasher.pyscrypt, "hash", _fast_hash)
    out_a = lskf_hasher.get_lskf_hash("1234", KAT_SALT)
    out_b = lskf_hasher.get_lskf_hash("5678", KAT_SALT)
    assert out_a != out_b


# ---------------------------------------------------------------------------
# CT-5 / CT-6 / CT-7 / CT-8 — hash_pin (pyscrypt mocked for speed/determinism)
# ---------------------------------------------------------------------------
def test_ct5_hash_pin_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hash_pin`` returns ``(pin, sha256(lskf_hash).hexdigest())``."""
    fixed = b"\x01" * 32
    monkeypatch.setattr(lskf_hasher.pyscrypt, "hash", MagicMock(return_value=fixed))
    monkeypatch.setattr(
        lskf_hasher, "get_example_data", lambda key: "00112233445566778899aabbccddeeff"
    )

    pin, hash_hex = lskf_hasher.hash_pin("4242")

    assert pin == "4242"
    assert hash_hex == hashlib.sha256(fixed).hexdigest()


def test_ct6_hash_pin_prints_pin_and_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``hash_pin`` prints both the PIN and the hash (documents VA-LSKF-3)."""
    monkeypatch.setattr(
        lskf_hasher.pyscrypt, "hash", MagicMock(return_value=b"\x02" * 32)
    )
    monkeypatch.setattr(
        lskf_hasher, "get_example_data", lambda key: "00112233445566778899aabbccddeeff"
    )

    lskf_hasher.hash_pin("4242")

    captured = capsys.readouterr().out
    assert "PIN:" in captured
    assert "Hash:" in captured
    assert "4242" in captured


def test_ct7_hash_pin_type_safety_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-bytes return from the derivation trips the ``TypeError`` guard."""
    monkeypatch.setattr(
        lskf_hasher.pyscrypt, "hash", MagicMock(return_value="not-bytes")
    )
    monkeypatch.setattr(
        lskf_hasher, "get_example_data", lambda key: "00112233445566778899aabbccddeeff"
    )

    with pytest.raises(TypeError, match="get_lskf_hash must return bytes"):
        lskf_hasher.hash_pin("4242")


def test_ct8_hash_pin_empty_salt_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``sample_pin_salt`` yields an empty salt without crashing (VA-LSKF-4).

    ``get_example_data("sample_pin_salt")`` is not in the ``_EXAMPLES`` dict, so
    it returns ``""`` → ``unhexlify("") == b""`` → the derivation runs with an
    empty salt. This freezes that real (demo-only) behaviour.
    """
    fake_hash = MagicMock(return_value=b"\x03" * 32)
    monkeypatch.setattr(lskf_hasher.pyscrypt, "hash", fake_hash)
    # Use the REAL provider: 'sample_pin_salt' is genuinely absent -> "".
    from custom_components.googlefindmy import example_data_provider

    assert example_data_provider.get_example_data("sample_pin_salt") == ""

    pin, hash_hex = lskf_hasher.hash_pin("4242")

    assert pin == "4242"
    assert hash_hex == hashlib.sha256(b"\x03" * 32).hexdigest()
    # The derivation was invoked with an empty salt (proves the silent default).
    assert fake_hash.call_args.kwargs["salt"] == b""
