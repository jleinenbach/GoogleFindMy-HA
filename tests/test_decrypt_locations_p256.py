# tests/test_decrypt_locations_p256.py
"""P-256 crowdsourced reports through ``async_decrypt_location_response_locations``.

Covers the wiring between the multi-reading decryption and the per-device
foreign-reading tracker: the remembered reading is tried first on the next
call (Z5), the feedback lines appear once (Z6), P-256 misses never invalidate
the cached identity key (Z11), each call counts as one poll, and failures are
classified by type rather than by message text.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.FMDNCrypto import foreign_tracker_cryptor
from custom_components.googlefindmy.FMDNCrypto.curve_profile import ScalarRule
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    EidVariant,
    generate_eid_variant,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_report_errors import (
    ForeignReportAuthError,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    P256_FOREIGN_READINGS,
    ForeignDecryptResult,
    encrypt,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.foreign_reading_tracker import (
    FOREIGN_READING_TRACKER,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2
from tests.helpers.fmdn_report_oracle import build_p256_report

pytestmark = pytest.mark.asyncio

_EIK = bytes(range(32))
_OTHER_EIK = bytes(range(32, 64))
_COUNTER = 1_000_000
_EPHEMERAL = 0x1234_5678_9ABC_DEF0
_BASE_NOW = 1_700_400_000.0
_ENTRY_ID = "entry-p256"
_CANONIC_ID = "canonic-p256"
_DEVICE_KEY = (_ENTRY_ID, _CANONIC_ID)
_MARKER = "FMDN_FOREIGN_READING"
# The third reading: provisional, not first in the default order, so a
# remembered preference is observable as a shorter search.
_READING = P256_FOREIGN_READINGS[2]
_P256_FAIL = ForeignReportAuthError(
    "secp256r1", tuple(r.reading_id for r in P256_FOREIGN_READINGS), 1
)


def _location_bytes() -> bytes:
    loc = DeviceUpdate_pb2.Location()
    loc.latitude = int(48.0 * 1e7)
    loc.longitude = int(11.0 * 1e7)
    loc.altitude = 500
    return loc.SerializeToString()


def _p256_report(eik: bytes = _EIK) -> tuple[bytes, bytes]:
    derivation = _READING.derivation
    report = build_p256_report(
        eik,
        _COUNTER,
        _location_bytes(),
        rule="mod_n" if derivation.rule is ScalarRule.MOD_N else "plus1",
        nonce_half_len=_READING.nonce_half_len,
        ephemeral_scalar=_EPHEMERAL,
        r_dash_byteorder="little" if derivation.byteorder == "little" else "big",
    )
    return report.encrypted_and_tag, report.sx


def _update(
    reports: list[tuple[bytes, bytes]],
    *,
    canonic_id: str | None = _CANONIC_ID,
    encrypted_identity_key: bytes = b"",
) -> DeviceUpdate_pb2.DeviceUpdate:
    """Build a device update with one foreign report per ``(payload, sx)``."""
    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.SetInParent()
    if encrypted_identity_key:
        registration.encryptedUserSecrets.encryptedIdentityKey = encrypted_identity_key
    if canonic_id is not None:
        cid = update.deviceMetadata.identifierInformation.canonicIds.canonicId.add()
        cid.id = canonic_id
    recent = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    for payload, sx in reports:
        location = recent.networkLocations.add()
        location.status = Common_pb2.Status.LAST_KNOWN
        location.geoLocation.accuracy = 5.0
        location.geoLocation.deviceTimeOffset = _COUNTER
        enc = location.geoLocation.encryptedReport
        enc.publicKeyRandom = sx
        enc.encryptedLocation = payload
        enc.isOwnReport = False
        recent.networkLocationTimestamps.add().seconds = int(_BASE_NOW - 10)
    return update


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: _BASE_NOW)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_a: False)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [_EIK]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )


async def _decrypt(update: DeviceUpdate_pb2.DeviceUpdate) -> list[dict[str, object]]:
    return await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=SimpleNamespace(entry_id=_ENTRY_ID)
    )


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _fail_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def fake_offload_foreign(
        *_args: object, **_kwargs: object
    ) -> ForeignDecryptResult:
        raise exc

    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )


async def test_second_call_tries_remembered_reading_first(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Z5/Z6: the next call starts with the reading that worked; one INFO total."""
    tag_checks: list[int] = []
    real_try = foreign_tracker_cryptor._try_decrypt_aes_eax

    def counting_try(*args: object) -> bytes | None:
        tag_checks[-1] += 1
        return real_try(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(foreign_tracker_cryptor, "_try_decrypt_aes_eax", counting_try)
    caplog.set_level(logging.INFO)

    for _ in range(2):
        tag_checks.append(0)
        result = await _decrypt(_update([_p256_report()]))
        assert any(decrypt_locations.is_real_location_record(r) for r in result)

    # Default order reaches the third reading on the third tag check; with the
    # preference remembered, the second call needs exactly one.
    assert tag_checks == [3, 1]
    assert FOREIGN_READING_TRACKER.preferred(_DEVICE_KEY) == _READING.reading_id
    infos = [m for m in _messages(caplog, logging.INFO) if m.startswith(_MARKER)]
    assert len(infos) == 1
    assert _READING.reading_id in infos[0]


async def test_canonic_id_casing_keys_one_device(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CX-7: the server may change the hex casing of one canonical ID.

    The second response spells the ID in upper case; the remembered reading
    must still apply and the device must appear once in diagnostics.
    """
    tag_checks: list[int] = []
    real_try = foreign_tracker_cryptor._try_decrypt_aes_eax

    def counting_try(*args: object) -> bytes | None:
        tag_checks[-1] += 1
        return real_try(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(foreign_tracker_cryptor, "_try_decrypt_aes_eax", counting_try)
    caplog.set_level(logging.INFO)

    for canonic_id in (_CANONIC_ID, _CANONIC_ID.upper()):
        tag_checks.append(0)
        result = await _decrypt(_update([_p256_report()], canonic_id=canonic_id))
        assert any(decrypt_locations.is_real_location_record(r) for r in result)

    assert tag_checks == [3, 1]
    assert len(FOREIGN_READING_TRACKER.diagnostics_snapshot(_ENTRY_ID)["devices"]) == 1
    infos = [m for m in _messages(caplog, logging.INFO) if m.startswith(_MARKER)]
    assert len(infos) == 1


async def test_p256_auth_failure_is_debug_only_and_keeps_eik_cache(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Z11: a poll with only P-256 misses warns nowhere and invalidates nothing."""
    encrypted_key = b"\x77" * 60
    cache_key = decrypt_locations._get_eik_cache_key(encrypted_key, 0, False)
    monkeypatch.setitem(decrypt_locations._eik_cache, cache_key, _EIK)
    invalidations: list[object] = []
    real_invalidate = decrypt_locations.invalidate_eik_cache_for_key

    def spy_invalidate(*args: object) -> int:
        invalidations.append(args)
        return real_invalidate(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(
        decrypt_locations, "invalidate_eik_cache_for_key", spy_invalidate
    )
    caplog.set_level(logging.DEBUG)

    await _decrypt(
        _update([_p256_report(_OTHER_EIK)], encrypted_identity_key=encrypted_key)
    )

    assert _messages(caplog, logging.WARNING) == []
    assert invalidations == []
    assert decrypt_locations._eik_cache.get(cache_key) == _EIK
    assert any(
        "1 crowdsourced P-256 report(s) failed" in m
        for m in _messages(caplog, logging.DEBUG)
    )


async def test_secp160r1_auth_failure_still_invalidates_eik_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart to Z11: SECP160R1 misses keep driving the invalidation."""
    encrypted_key = b"\x78" * 60
    invalidations: list[object] = []

    def spy_invalidate(*args: object) -> int:
        invalidations.append(args)
        return 0

    monkeypatch.setattr(
        decrypt_locations, "invalidate_eik_cache_for_key", spy_invalidate
    )
    eid = generate_eid_variant(_OTHER_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
    payload, sx = encrypt(_location_bytes(), bytes(range(20)), eid)

    await _decrypt(_update([(payload, sx)], encrypted_identity_key=encrypted_key))

    assert len(invalidations) == 1


async def test_each_call_counts_as_one_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two calls (e.g. two FCM pushes) are two polls for the Z6 threshold."""
    _fail_with(monkeypatch, _P256_FAIL)
    caplog.set_level(logging.DEBUG)
    report = (b"\x00" * 32, b"\x01" * 32)

    await _decrypt(_update([report, report]))
    assert _messages(caplog, logging.WARNING) == []
    await _decrypt(_update([report]))

    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith(f"{_MARKER} all_failed")


async def test_reports_of_one_call_share_one_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Three failed reports in a single call stay below the two-poll threshold."""
    _fail_with(monkeypatch, _P256_FAIL)
    caplog.set_level(logging.DEBUG)
    report = (b"\x00" * 32, b"\x01" * 32)

    await _decrypt(_update([report, report, report]))

    assert _messages(caplog, logging.WARNING) == []


async def test_unsupported_sx_length_warns_once_per_length(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An Sx length without a curve: one tracker WARNING, DEBUG per report."""
    caplog.set_level(logging.DEBUG)
    report = (b"\x00" * 32, b"\x01" * 24)

    await _decrypt(_update([report, report]))

    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith(f"{_MARKER} unsupported_length")
    skipped = [
        m
        for m in _messages(caplog, logging.DEBUG)
        if "Skipping one foreign report" in m
    ]
    assert len(skipped) == 2


@pytest.mark.parametrize("sx_len", [20, 32])
async def test_structure_error_is_debug_only(
    caplog: pytest.LogCaptureFixture, sx_len: int
) -> None:
    """A payload shorter than the tag is malformed on both curves: DEBUG, no WARNING.

    Before the refactor a SECP160R1 structure error was a WARNING "malformed
    data" per report; it is DEBUG now because a structure error says nothing
    about keys or readings.
    """
    caplog.set_level(logging.DEBUG)

    await _decrypt(_update([(b"\x00" * 8, b"\x01" * sx_len)]))

    assert _messages(caplog, logging.WARNING) == []
    assert any(
        "Skipping one malformed foreign report" in m
        for m in _messages(caplog, logging.DEBUG)
    )


async def test_no_tracking_without_canonic_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """E7: without a canonical ID the report decrypts but nothing is tracked."""
    preferred_seen: list[object] = []
    real_offload = decrypt_locations._offload_decrypt_foreign

    async def spy_offload(*args: object) -> ForeignDecryptResult:
        preferred_seen.append(args[-1])
        return await real_offload(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_foreign", spy_offload)
    caplog.set_level(logging.INFO)

    for _ in range(2):
        result = await _decrypt(_update([_p256_report()], canonic_id=None))
        assert any(decrypt_locations.is_real_location_record(r) for r in result)

    assert preferred_seen == [None, None]
    assert not [m for m in _messages(caplog, logging.INFO) if m.startswith(_MARKER)]
    assert FOREIGN_READING_TRACKER.diagnostics_snapshot(_ENTRY_ID)["devices"] == []


def _secp160r1_miss() -> tuple[bytes, bytes]:
    """A SECP160R1 report keyed to another EIK: an authentication failure."""
    eid = generate_eid_variant(_OTHER_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
    return encrypt(_location_bytes(), bytes(range(20)), eid)


@pytest.mark.parametrize(
    "neutral_report",
    [
        pytest.param(lambda: _p256_report(_OTHER_EIK), id="p256_all_failed"),
        pytest.param(lambda: (b"\x00" * 32, b"\x01" * 24), id="unsupported_length"),
        pytest.param(lambda: (b"\x00" * 8, b"\x01" * 32), id="structure_error"),
    ],
)
async def test_key_neutral_reports_do_not_block_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    neutral_report: object,
) -> None:
    """CX-5: a key-neutral report beside an auth failure still invalidates.

    The neutral report says nothing about the cached identity key (Z11), so
    it leaves the denominator; the one report that could speak about the key
    failed authentication.
    """
    encrypted_key = b"\x79" * 60
    invalidations: list[object] = []

    def spy_invalidate(*args: object) -> int:
        invalidations.append(args)
        return 0

    monkeypatch.setattr(
        decrypt_locations, "invalidate_eik_cache_for_key", spy_invalidate
    )
    make_neutral = neutral_report
    assert callable(make_neutral)

    await _decrypt(
        _update(
            [_secp160r1_miss(), make_neutral()],
            encrypted_identity_key=encrypted_key,
        )
    )

    assert len(invalidations) == 1


async def test_decrypted_report_blocks_invalidation_beside_neutral_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CX-5 bound: a report that decrypted still vetoes the invalidation.

    Neutral reports shrink the denominator by exactly their own count; one
    success and one miss beside them is not "every key-relevant report failed".
    """
    invalidations: list[object] = []
    monkeypatch.setattr(
        decrypt_locations,
        "invalidate_eik_cache_for_key",
        lambda *args: invalidations.append(args) or 0,
    )
    eid = generate_eid_variant(_EIK, _COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE)
    decrypted = encrypt(_location_bytes(), bytes(range(20)), eid)

    await _decrypt(
        _update(
            [decrypted, _secp160r1_miss(), (b"\x00" * 32, b"\x01" * 24)],
            encrypted_identity_key=b"\x7b" * 60,
        )
    )

    assert invalidations == []


async def test_key_neutral_reports_alone_do_not_invalidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart to CX-5: without any auth failure nothing is invalidated."""
    invalidations: list[object] = []
    monkeypatch.setattr(
        decrypt_locations,
        "invalidate_eik_cache_for_key",
        lambda *args: invalidations.append(args) or 0,
    )

    await _decrypt(
        _update(
            [_p256_report(_OTHER_EIK), (b"\x00" * 32, b"\x01" * 24)],
            encrypted_identity_key=b"\x7a" * 60,
        )
    )

    assert invalidations == []


@pytest.mark.parametrize(
    ("report", "debug_text"),
    [
        pytest.param(
            (b"\x00" * 32, b"\x01" * 24),
            "Skipping one foreign report",
            id="unsupported_length",
        ),
        pytest.param(
            None,
            "No provisional reading authenticated one foreign report",
            id="p256_all_failed",
        ),
    ],
)
async def test_failures_without_canonic_id_stay_untracked(
    caplog: pytest.LogCaptureFixture,
    report: tuple[bytes, bytes] | None,
    debug_text: str,
) -> None:
    """CC-5 (E7): without a canonical ID a failure is DEBUG only, never tracked."""
    caplog.set_level(logging.DEBUG)

    for _ in range(3):
        await _decrypt(_update([report or _p256_report(_OTHER_EIK)], canonic_id=None))

    assert _messages(caplog, logging.WARNING) == []
    assert any(debug_text in m for m in _messages(caplog, logging.DEBUG))
    assert FOREIGN_READING_TRACKER.diagnostics_snapshot(_ENTRY_ID)["devices"] == []
    assert FOREIGN_READING_TRACKER.diagnostics_snapshot(None)["devices"] == []
