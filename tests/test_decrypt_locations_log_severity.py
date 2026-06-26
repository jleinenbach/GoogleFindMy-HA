# tests/test_decrypt_locations_log_severity.py
"""Log severity of per-report decrypt-auth failures in ``decrypt_locations``.

``async_decrypt_location_response_locations`` is stateless per call: it sees a
single poll cycle (often just one report) and cannot judge whether an
authentication failure is persistent. A single own-report ``InvalidTag`` is
therefore detail diagnostics, logged at DEBUG, and must carry no
``re-authenticate`` advice -- the sibling-aware, cross-cycle verdict belongs to
the coordinator (``PollingOperations``) and the FCM callback, which stay the
user-facing WARNING/ERROR owners. The functional contract is unchanged: the
own-report mismatch is still surfaced via ``OwnReportIdentityMismatchError`` and
the stale EIK cache is still invalidated.
"""

from __future__ import annotations

import logging

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2

pytestmark = pytest.mark.asyncio

_DECRYPT_LOGGER = decrypt_locations.__name__


def _build_single_own_report_update() -> DeviceUpdate_pb2.DeviceUpdate:
    """Build a DeviceUpdate carrying exactly one own (server-side) report.

    A 32-byte ``encryptedIdentityKey`` is attached so the aggregate
    invalidation branch is reached without triggering the 60-byte unwrap path.
    """

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x11" * 32

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    reports.recentLocationTimestamp.seconds = 1_700_000_000
    recent = reports.recentLocation
    recent.status = Common_pb2.Status.LAST_KNOWN
    recent.geoLocation.accuracy = 5.0
    encrypted_report = recent.geoLocation.encryptedReport
    encrypted_report.publicKeyRandom = b""  # empty -> own report
    encrypted_report.encryptedLocation = b"ciphertext"
    encrypted_report.isOwnReport = True
    return update


async def test_single_own_report_invalidtag_logs_debug_without_reauth_advice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One own-report ``InvalidTag`` is DEBUG-only and gives no reauth advice.

    The aggregate "all reports failed authentication" notice must also be DEBUG
    and free of ``re-authenticate``/``persistent`` wording, while the
    ``OwnReportIdentityMismatchError`` escalation signal is still raised so the
    coordinator/callback keep ownership of the user-facing verdict.
    """

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload(*_args: object, **_kwargs: object) -> bytes:
        raise InvalidTag("authentication tag mismatch")

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload)

    update = _build_single_own_report_update()

    with caplog.at_level(logging.DEBUG, logger=_DECRYPT_LOGGER):
        with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
            await decrypt_locations.async_decrypt_location_response_locations(
                update, cache=object()
            )

    decrypt_records = [rec for rec in caplog.records if rec.name == _DECRYPT_LOGGER]

    # The per-report InvalidTag notice exists and proves the failure path ran...
    per_report = [
        rec
        for rec in decrypt_records
        if "Decryption auth failed (InvalidTag)" in rec.message
    ]
    assert len(per_report) == 1
    # ...at DEBUG, not WARNING (this is the behavior the change pins).
    assert per_report[0].levelno == logging.DEBUG

    # The aggregate "all reports failed authentication" notice is also DEBUG.
    aggregate = [
        rec
        for rec in decrypt_records
        if "encrypted location reports failed authentication" in rec.message
    ]
    assert len(aggregate) == 1
    assert aggregate[0].levelno == logging.DEBUG

    # No auth-failure record from this stateless layer may reach WARNING/ERROR;
    # the user-facing verdict is owned by the coordinator/callback.
    auth_records = per_report + aggregate
    assert all(rec.levelno < logging.WARNING for rec in auth_records)

    # The contradictory reauth advice is gone from every record of this layer.
    joined = "\n".join(rec.getMessage() for rec in decrypt_records).lower()
    assert "re-authenticat" not in joined
    assert "try re-authenticating" not in joined


async def test_aggregate_invalidation_notice_is_debug_and_not_persistent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale EIK is still invalidated, but the notices are DEBUG/neutral.

    With a primed EIK cache the ``removed > 0`` branch runs: the aggregate
    "all reports failed" notice and the "Invalidated N EIK cache entries" INFO
    must no longer claim the failure is ``persistent`` or advise reauth, while
    the cache entries are still dropped to force a fresh derivation next poll.
    """

    raw_eik = b"\x11" * 32
    owner_version = 0  # default ownerKeyVersion on the proto used below

    # Prime the EIK cache via the real key mechanic so invalidate removes >0.
    decrypt_locations._eik_cache.clear()
    for do_flip in (False, True):
        flipped = decrypt_locations.flip_bits(raw_eik, do_flip)
        cache_key = decrypt_locations._get_eik_cache_key(
            flipped, owner_version, do_flip
        )
        decrypt_locations._eik_cache[cache_key] = b"\x00" * 32

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload(*_args: object, **_kwargs: object) -> bytes:
        raise InvalidTag("authentication tag mismatch")

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload)

    update = _build_single_own_report_update()

    try:
        with caplog.at_level(logging.DEBUG, logger=_DECRYPT_LOGGER):
            with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
                await decrypt_locations.async_decrypt_location_response_locations(
                    update, cache=object()
                )
    finally:
        decrypt_locations._eik_cache.clear()

    decrypt_records = [rec for rec in caplog.records if rec.name == _DECRYPT_LOGGER]

    # The cache invalidation INFO ran (removed > 0 branch) with neutral wording.
    invalidation = [rec for rec in decrypt_records if "Invalidated" in rec.message]
    assert len(invalidation) == 1
    assert invalidation[0].levelno == logging.INFO
    assert "after authentication failures this cycle" in invalidation[0].getMessage()

    # The aggregate notice ran on the removed>0 branch, at DEBUG.
    aggregate = [
        rec
        for rec in decrypt_records
        if "encrypted location reports failed authentication" in rec.message
    ]
    assert len(aggregate) == 1
    assert aggregate[0].levelno == logging.DEBUG

    joined = "\n".join(rec.getMessage() for rec in decrypt_records).lower()
    assert "persistent" not in joined
    assert "re-authenticat" not in joined
