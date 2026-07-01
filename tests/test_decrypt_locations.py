# tests/test_decrypt_locations.py
"""Regression tests for decrypting location payloads."""

from __future__ import annotations

import asyncio
import logging

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
)

pytestmark = pytest.mark.asyncio

ALTITUDE_METERS = 1337
ACCURACY_METERS = 5.0


async def test_async_decrypt_location_response_locations_allows_future_owner_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner reports within a realistic future drift window are preserved."""

    base_now = 1_700_000_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    location_proto = DeviceUpdate_pb2.Location()
    location_proto.latitude = int(52.0 * 1e7)
    location_proto.longitude = int(13.0 * 1e7)
    location_proto.altitude = 1337
    location_bytes = location_proto.SerializeToString()

    async def fake_identity_key(*_args, **_kwargs) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload(*_args, **_kwargs) -> bytes:
        return location_bytes

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    owner_timestamp = int(base_now + 2 * 3600)
    reports.recentLocationTimestamp.seconds = owner_timestamp

    recent = reports.recentLocation
    recent.status = Common_pb2.Status.LAST_KNOWN
    recent.geoLocation.accuracy = 5.0
    encrypted_report = recent.geoLocation.encryptedReport
    encrypted_report.publicKeyRandom = b""
    encrypted_report.encryptedLocation = b"ignored"
    encrypted_report.isOwnReport = True

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["last_seen"] == owner_timestamp
    assert entry["is_own_report"] is True
    assert entry["altitude"] == ALTITUDE_METERS
    assert entry["accuracy"] == ACCURACY_METERS


async def test_async_decrypt_location_response_locations_aligns_missing_network_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent timestamps stay aligned when historic timestamps are missing."""

    valid_location = DeviceUpdate_pb2.Location()
    valid_location.latitude = int(40.0 * 1e7)
    valid_location.longitude = int(-74.0 * 1e7)
    valid_location.altitude = ALTITUDE_METERS

    invalid_location = DeviceUpdate_pb2.Location()
    invalid_location.latitude = int(91.0 * 1e7)  # Out of bounds → dropped
    invalid_location.longitude = 0
    invalid_location.altitude = ALTITUDE_METERS

    def serialize_location(loc: DeviceUpdate_pb2.Location) -> bytes:
        return loc.SerializeToString()

    async def fake_identity_key(*_args, **_kwargs) -> list[bytes]:
        return [b"\x01" * 32]

    async def fake_offload_aes(
        _identity_key: bytes, encrypted_location: bytes
    ) -> bytes:
        if encrypted_location == b"recent":
            return serialize_location(valid_location)
        return serialize_location(invalid_location)

    async def fake_offload_foreign(
        _identity_key: bytes,
        encrypted_location: bytes,
        *_args: object,
        **_kwargs: object,
    ) -> bytes:
        return await fake_offload_aes(_identity_key, encrypted_location)

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)
    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_: False)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations

    network_location = reports.networkLocations.add()
    network_location.status = Common_pb2.Status.LAST_KNOWN
    network_location.geoLocation.accuracy = ACCURACY_METERS
    network_enc = network_location.geoLocation.encryptedReport
    network_enc.publicKeyRandom = b""
    network_enc.encryptedLocation = b"network"
    network_enc.isOwnReport = False

    recent_timestamp = 1_700_000_321
    reports.recentLocationTimestamp.seconds = recent_timestamp
    recent_location = reports.recentLocation
    recent_location.SetInParent()
    recent_location.status = Common_pb2.Status.LAST_KNOWN
    recent_location.geoLocation.accuracy = ACCURACY_METERS
    recent_enc = recent_location.geoLocation.encryptedReport
    recent_enc.publicKeyRandom = b""
    recent_enc.encryptedLocation = b"recent"
    recent_enc.isOwnReport = True

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["last_seen"] == recent_timestamp
    assert entry["latitude"] == pytest.approx(40.0)
    assert entry["longitude"] == pytest.approx(-74.0)
    assert entry["is_own_report"] is True


async def test_async_decrypt_location_response_locations_returns_metadata_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pairing and secrets anchors propagate even without reports."""

    base_now = 1_700_100_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x41" * 32]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = int(base_now - 120)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 60)
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 32

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry.get("metadata_only") is True
    assert entry["pair_date"] == int(base_now - 120)
    assert entry["secrets_creation_date"] == int(base_now - 60)
    registration_meta = entry.get("device_registration")
    assert isinstance(registration_meta, dict)
    assert registration_meta.get("pairDate") == int(base_now - 120)
    secrets_meta = entry.get("encrypted_user_secrets")
    assert isinstance(secrets_meta, dict)
    assert secrets_meta.get("creationDate") == int(base_now - 60)


async def test_async_decrypt_location_response_unwraps_60_byte_eik(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression Test: Unwrap 60-byte EIKs so HKDF/location decryption never sees "Key length 60"."""

    base_now = 1_700_100_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    caplog.set_level(logging.WARNING)

    encrypted_eik = b"\x99" * decrypt_locations.EIK_GCM_TOTAL_LEN
    # Moto Tag/Chipolo responses wrap the 32-byte identity key inside a 60-byte envelope.
    unwrapped_eik = b"\x42" * 32

    location_proto = DeviceUpdate_pb2.Location()
    location_proto.latitude = int(37.42 * 1e7)
    location_proto.longitude = int(-122.084 * 1e7)
    location_proto.altitude = ALTITUDE_METERS
    location_bytes = location_proto.SerializeToString()

    identity_key_calls = {"count": 0}

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        identity_key_calls["count"] += 1
        return [encrypted_eik]

    unwrap_calls: dict[str, object] = {}
    offload_calls: dict[str, bytes] = {}

    async def fake_get_owner_key(*, cache):  # type: ignore[no-untyped-def]
        unwrap_calls["cache"] = cache
        return OwnerKeyInfo(key=b"\xaa" * 32, version=3)

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    def fake_decrypt(owner_key: bytes, encrypted_identity_key: bytes) -> bytes:
        unwrap_calls["owner_key"] = owner_key
        unwrap_calls["encrypted"] = encrypted_identity_key
        return unwrapped_eik

    async def fake_offload_aes(identity_key: bytes, encrypted_location: bytes) -> bytes:
        offload_calls["identity_key"] = identity_key
        offload_calls["encrypted_location"] = encrypted_location
        return location_bytes

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "async_get_owner_key", fake_get_owner_key)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(decrypt_locations, "decrypt_eik", fake_decrypt)
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = int(base_now - 30)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 15)
    registration.encryptedUserSecrets.encryptedIdentityKey = encrypted_eik

    reports = update.deviceMetadata.information.locationInformation.reports
    report_group = reports.recentLocationAndNetworkLocations
    report_group.recentLocationTimestamp.seconds = int(base_now - 10)
    recent = report_group.recentLocation
    recent.status = Common_pb2.Status.LAST_KNOWN
    recent.geoLocation.accuracy = ACCURACY_METERS
    encrypted_report = recent.geoLocation.encryptedReport
    encrypted_report.publicKeyRandom = b""
    encrypted_report.encryptedLocation = b"ciphertext"
    encrypted_report.isOwnReport = True

    cache = object()
    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=cache
    )

    assert len(result) == 1
    entry = result[0]
    assert entry.get("metadata_only") is not True
    assert entry["identity_key"] == unwrapped_eik
    # Bug 6 fix: early-unwrapped key is prepended to retrieved candidates.
    # fake_identity_key returns [encrypted_eik], so candidates = [unwrapped_eik, encrypted_eik].
    assert entry["identity_key_candidates"] == [unwrapped_eik, encrypted_eik]
    assert entry["encrypted_identity_key"] == encrypted_eik
    assert entry["pair_date"] == int(base_now - 30)
    assert entry["secrets_creation_date"] == int(base_now - 15)
    assert entry["accuracy"] == ACCURACY_METERS
    assert entry["altitude"] == ALTITUDE_METERS
    assert offload_calls["identity_key"] == unwrapped_eik
    assert len(offload_calls["identity_key"]) == 32
    assert offload_calls["encrypted_location"] == b"ciphertext"
    assert unwrap_calls["cache"] is cache
    assert unwrap_calls["owner_key"] == b"\xaa" * 32
    assert unwrap_calls["encrypted"] == encrypted_eik
    # Bug 6 fix: async_retrieve_identity_key is now always called to produce
    # the full candidate set (MCU flip variants, shared key alternatives).
    assert identity_key_calls["count"] == 1
    assert "[DIAG-ALERT] Key length" not in caplog.text


async def test_async_decrypt_location_response_locations_normalizes_anchor_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anchor extraction normalizes millisecond-like inputs and preserves keys."""

    base_now = 1_700_200_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x51" * 32]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = int(base_now - 300)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 150)
    registration.encryptedUserSecrets.creationDate.nanos = 500_000_000
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x10" * 32

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry.get("metadata_only") is True
    assert entry["pair_date"] == int(base_now - 300)
    assert entry["secrets_creation_date"] == int(base_now - 150)
    assert entry["identity_key"] == b"\x51" * 32
    assert entry["identity_key_candidates"] == [b"\x51" * 32]
    assert entry["encrypted_identity_key"] == b"\x10" * 32
    assert decrypt_locations._parse_epoch_seconds(  # type: ignore[attr-defined]
        int((base_now - 75) * 1000), base_now
    ) == pytest.approx(base_now - 75)


async def test_async_decrypt_location_response_reads_registration_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration anchors propagate even when deviceTypeInformation is absent."""

    base_now = 1_700_400_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x61" * 32]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x22" * 32

    registration.pairDate = int(base_now - 500)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 250)

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    reports.recentLocationTimestamp.seconds = int(base_now - 100)
    semantic = reports.recentLocation
    semantic.status = Common_pb2.Status.SEMANTIC
    semantic.semanticLocation.locationName = "semantic-anchor"

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["pair_date"] == int(base_now - 500)
    assert entry["secrets_creation_date"] == int(base_now - 250)
    assert entry["identity_key"] == b"\x61" * 32
    type_meta = entry.get("device_type_information")
    assert not type_meta
    registration_meta = entry.get("device_registration")
    assert isinstance(registration_meta, dict)
    assert registration_meta.get("pairDate") == int(base_now - 500)


async def test_pair_date_microseconds_normalization_and_future_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PairDate with microseconds normalizes while future drift is discarded."""

    base_now = 1_700_300_000.0

    normalized_pair_date = decrypt_locations.normalize_pair_date_value(
        (base_now - 240) * 1_000_000,
        now_wall=base_now,
    )
    assert normalized_pair_date == int(base_now - 240)

    future_pair_date = decrypt_locations.normalize_pair_date_value(
        (base_now + decrypt_locations.MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S + 50)
        * 1_000_000,
        now_wall=base_now,
    )

    assert future_pair_date is None


# ---------------------------------------------------------------------------
# Diff-Review #1 (Variant C): own-report exhaustion escalates to DecryptionError,
# while foreign-only failures and report-less devices never escalate.
# ---------------------------------------------------------------------------


def _valid_location_bytes() -> bytes:
    """Serialize a small, in-bounds Location proto for a successful decrypt."""

    loc = DeviceUpdate_pb2.Location()
    loc.latitude = int(48.0 * 1e7)
    loc.longitude = int(11.0 * 1e7)
    loc.altitude = ALTITUDE_METERS
    return loc.SerializeToString()


def _add_report(
    update: DeviceUpdate_pb2.DeviceUpdate,
    *,
    public_key_random: bytes,
    encrypted_location: bytes,
    is_own_report: bool,
    base_now: float,
) -> None:
    """Append one encrypted report; empty public_key_random marks an own report."""

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    network_location = reports.networkLocations.add()
    network_location.status = Common_pb2.Status.LAST_KNOWN
    network_location.geoLocation.accuracy = ACCURACY_METERS
    enc = network_location.geoLocation.encryptedReport
    enc.publicKeyRandom = public_key_random
    enc.encryptedLocation = encrypted_location
    enc.isOwnReport = is_own_report
    # A matching timestamp is required, otherwise the report is dropped before
    # the decrypt attempt (zip_longest pads missing timestamps with None).
    reports.networkLocationTimestamps.add().seconds = int(base_now - 10)


async def test_all_own_reports_failing_auth_raises_decryption_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D1: every own report fails authentication → OwnReportIdentityMismatchError.

    Device-local signal: the cached identity key no longer matches THIS device's own
    server reports (e.g. a phone powered off for days), so the function must raise
    the dedicated subclass instead of silently returning empty (which the
    coordinator would never surface). The coordinator downgrades it to a warning
    only when a sibling proves the account keys healthy; on its own it still
    escalates. Asserting the concrete subclass locks in the contract the
    coordinator's sibling-success gate keys off.
    """

    base_now = 1_700_000_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_aes(*_args: object, **_kwargs: object) -> bytes:
        raise InvalidTag

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-ciphertext",
        is_own_report=True,
        base_now=base_now,
    )

    with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
        await decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )


async def test_stale_own_reports_with_foreign_success_preserve_network_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D1b: stale own reports + a successful foreign report → keep the network fix.

    Counterpoint to T-D1: an offline phone still seen by the FMDN network (a Google
    Home / Nest anchor) yields stale own reports (all fail authentication) alongside
    one foreign/crowdsourced report that decrypts. The own-report mismatch raise must
    be SUPPRESSED so the good network coordinate is returned instead of being
    discarded by the exception before the ``wrapped`` list is ever converted. A
    reauth cannot fix stale own reports of an offline device anyway, and the device
    is demonstrably locatable this cycle. Dropping the ``is_real_location_record``
    network-fix suppression makes this test raise ``OwnReportIdentityMismatchError``
    (mutation proof).
    """

    base_now = 1_700_000_750.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_a: False)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_aes(*_args: object, **_kwargs: object) -> bytes:
        raise InvalidTag

    async def fake_offload_foreign(*_args: object, **_kwargs: object) -> bytes:
        return _valid_location_bytes()

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)
    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-stale",
        is_own_report=True,
        base_now=base_now,
    )
    _add_report(
        update,
        public_key_random=b"\x20\x21\x22\x23",
        encrypted_location=b"foreign-ok",
        is_own_report=False,
        base_now=base_now,
    )

    # Must NOT raise: the successful network report is a valid fix to preserve.
    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    # The good crowdsourced position survived instead of being discarded by the raise.
    assert decrypt_locations.any_real_location_record(result)


async def test_stale_own_reports_with_undecodable_foreign_still_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D1c: stale own reports + a foreign report that decrypts to bytes but yields
    NO usable coordinate → escalation must STILL fire.

    Codex (PR #1153) counterpoint to T-D1b: a foreign/crowdsourced report can
    authenticate to raw bytes yet fail protobuf parsing or lat/lon bounds
    validation, so it never becomes a real coordinate. Suppressing the own-report
    mismatch on "decrypted to bytes" alone would hand the caller an empty result
    AND skip the stale-own-report handling even though no network fix was preserved.
    The suppression is therefore judged on the validated ``is_real_location_record``
    output; with the only foreign report out of bounds, the raise must fire. Basing
    suppression back on mere decrypt success makes this test stop raising (mutation
    proof).
    """

    base_now = 1_700_000_800.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_a: False)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_aes(*_args: object, **_kwargs: object) -> bytes:
        raise InvalidTag

    def _out_of_bounds_location_bytes() -> bytes:
        # Parses cleanly, but latitude 200° is rejected by _is_valid_latlon, so the
        # report is dropped by the fail-fast validation and never enters `structured`.
        loc = DeviceUpdate_pb2.Location()
        loc.latitude = int(200.0 * 1e7)
        loc.longitude = int(11.0 * 1e7)
        loc.altitude = ALTITUDE_METERS
        return loc.SerializeToString()

    async def fake_offload_foreign(*_args: object, **_kwargs: object) -> bytes:
        return _out_of_bounds_location_bytes()

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)
    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-stale",
        is_own_report=True,
        base_now=base_now,
    )
    _add_report(
        update,
        public_key_random=b"\x30\x31\x32\x33",
        encrypted_location=b"foreign-unusable",
        is_own_report=False,
        base_now=base_now,
    )

    # The foreign report decrypted but produced no valid coordinate, so the stale
    # own-report signal must still escalate (there is no network fix to preserve).
    with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
        await decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )


async def test_foreign_failures_with_own_success_do_not_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D2: own report succeeds, foreign reports fail → no escalation.

    Foreign/crowdsourced reports legitimately fail with another account's key.
    A single own-report success proves the key is healthy, so they must not
    raise an account-wide DecryptionError (regression guard for Befund #2/#4).
    """

    base_now = 1_700_000_500.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_a: False)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_aes(*_args: object, **_kwargs: object) -> bytes:
        return _valid_location_bytes()

    async def fake_offload_foreign(*_args: object, **_kwargs: object) -> bytes:
        raise ValueError("MAC check failed")

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)
    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-ok",
        is_own_report=True,
        base_now=base_now,
    )
    _add_report(
        update,
        public_key_random=b"\x10\x11\x12\x13",
        encrypted_location=b"foreign-bad",
        is_own_report=False,
        base_now=base_now,
    )

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    # Own report decoded; no exception despite the foreign auth failure.
    assert any(entry.get("metadata_only") is not True for entry in result)


async def test_foreign_only_failure_does_not_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D3: device with no own reports → never escalate, even if foreign fails.

    With ``_own_encrypted_report_count == 0`` the own-exhaustion gate is closed,
    so a failing foreign-only report returns gracefully instead of raising.
    """

    base_now = 1_700_001_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_a: False)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_foreign(*_args: object, **_kwargs: object) -> bytes:
        raise ValueError("MAC check failed")

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"\x20\x21\x22\x23",
        encrypted_location=b"foreign-bad",
        is_own_report=False,
        base_now=base_now,
    )

    # Must not raise: no own reports means no account-wide signal.
    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )
    assert isinstance(result, list)


async def test_partial_own_success_self_heals_without_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D4: one own report fails but another succeeds → no escalation.

    A single own-report success means the key still works, so a transient
    per-report failure must self-heal rather than escalate.
    """

    base_now = 1_700_001_500.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    calls = {"n": 0}

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_aes(*_args: object, **_kwargs: object) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise InvalidTag
        return _valid_location_bytes()

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-bad",
        is_own_report=True,
        base_now=base_now,
    )
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-ok",
        is_own_report=True,
        base_now=base_now,
    )

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    # The successful own report suppresses escalation entirely.
    assert any(entry.get("metadata_only") is not True for entry in result)


async def test_own_report_mac_valueerror_counts_as_own_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-D5: an own-report MAC ValueError counts as an own failure and escalates.

    Own reports use AES-GCM (which raises InvalidTag), but the MAC-ValueError
    handler is mirrored defensively for any future own-report path that surfaces
    a PyCryptodome-style ``ValueError("MAC check failed")``. This guards that the
    defensive symmetry still feeds the own-only escalation counter.
    """

    base_now = 1_700_002_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x42" * 32]

    async def fake_offload_aes(*_args: object, **_kwargs: object) -> bytes:
        raise ValueError("MAC check failed")

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()
    _add_report(
        update,
        public_key_random=b"",
        encrypted_location=b"own-mac-fail",
        is_own_report=True,
        base_now=base_now,
    )

    with pytest.raises(decrypt_locations.DecryptionError):
        await decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # Authenticated coordinate report -> positive proof of the shared key.
        ({"last_seen": 123.0, "latitude": 1.0, "longitude": 2.0}, True),
        # Coordinates win regardless of an explicit metadata_only=False flag.
        ({"latitude": 1.0, "longitude": 2.0, "metadata_only": False}, True),
        # 0.0 coordinates are valid (is-None check, not truthiness).
        ({"latitude": 0.0, "longitude": 0.0}, True),
        # SEMANTIC-shaped row: last_seen + semantic_name but no coordinates and no
        # metadata_only flag -> skipped the crypto path, so it proves nothing.
        (
            {
                "last_seen": 123.0,
                "semantic_name": "Home",
                "status": "semantic",
                "latitude": None,
                "longitude": None,
            },
            False,
        ),
        # Partial coordinates (only one axis) -> not an authenticated fix.
        ({"latitude": 1.0, "longitude": None}, False),
        # metadata_only sentinel -> secrets-bundle key material, no decrypt.
        ({"metadata_only": True, "owner_key_version": 7}, False),
        ({}, False),
        (None, False),
    ],
)
async def test_is_real_location_record(record: object, expected: bool) -> None:
    """Only an authenticated coordinate report proves the shared key.

    Decrypted coordinates are produced solely by the authenticated crypto path, so
    requiring a real ``latitude``/``longitude`` pair is an allowlist for genuine
    reports. Truthy-but-unauthenticated shapes -- ``metadata_only=True`` sentinels
    and SEMANTIC rows (no coordinates, crypto path skipped) -- must return False, or
    they would clear the shared decrypt-failure budget and mask a stale key.
    """
    assert decrypt_locations.is_real_location_record(record) is expected


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        # Empty / falsy inputs -> no proof.
        (None, False),
        ([], False),
        # Only report-less rows -> no record authenticates a coordinate report.
        (
            [
                {"metadata_only": True, "owner_key_version": 7},
                {"last_seen": 9.0, "semantic_name": "Home", "latitude": None},
            ],
            False,
        ),
        # A real coordinate report anywhere in the list proves the shared key,
        # even when a report-less SEMANTIC row would outrank it in the selector.
        (
            [
                {"last_seen": 5.0, "latitude": 1.0, "longitude": 2.0},
                {"last_seen": 9.0, "semantic_name": "Home", "latitude": None},
            ],
            True,
        ),
    ],
)
async def test_any_real_location_record(records: object, expected: bool) -> None:
    """The decrypt proof is a full-list property, not a single-record property.

    ``any_real_location_record`` must return True when ANY record authenticates a
    coordinate report, so a successfully decrypted fix hidden behind a newer
    report-less SEMANTIC/metadata row is never lost. Empty inputs and lists of only
    report-less rows must return False so they cannot clear the reauth budget.
    """
    assert decrypt_locations.any_real_location_record(records) is expected
