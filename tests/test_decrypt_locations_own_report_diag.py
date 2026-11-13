# tests/test_decrypt_locations_own_report_diag.py
"""Own-report decryption in ``decrypt_locations`` with a single identity key.

T0 characterizes today's behavior before the own-report path changes: for a
device with exactly one identity key candidate it pins which keys reach
``_offload_decrypt_aes``, the ``identity_key`` and ``identity_key_candidates``
in the result, how often ``asyncio.to_thread`` runs, and the outcome (one
decrypted report, or ``OwnReportIdentityMismatchError`` when every own report
fails authentication).

Field 29 and field 33 are attached only as hand-built raw bytes through
``_build_update``; no message type from another project is used for them, and
the tests attach no meaning to their content.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import encrypt_aes_gcm
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2

pytestmark = pytest.mark.asyncio

_KEY = bytes(range(32))  # asymmetric, so a reordered key is detectable
_LATITUDE_E7 = 491_234_567
_LONGITUDE_E7 = 108_765_432


def _build_update(
    *,
    own_reports: int = 1,
    field29: bytes | None = None,
    field33: bytes | None = None,
) -> DeviceUpdate_pb2.DeviceUpdate:
    """Build a DeviceUpdate with ``own_reports`` own (server-side) reports.

    ``field29`` and ``field33`` are written as raw bytes into the
    ``encryptedUserSecrets`` registration fields, so later tests can shape them
    freely. A 32-byte ``encryptedIdentityKey`` keeps the 60-byte unwrap path
    out of the run.
    """

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x11" * 32
    if field29 is not None:
        registration.keyRotationInformation = field29
    if field33 is not None:
        registration.firstSecretsCreationDate = field33

    reports = update.deviceMetadata.information.locationInformation.reports
    recent_and_network = reports.recentLocationAndNetworkLocations
    for index in range(own_reports):
        if index == 0:
            recent_and_network.recentLocationTimestamp.seconds = 1_700_000_000
            location = recent_and_network.recentLocation
        else:
            recent_and_network.networkLocationTimestamps.add().seconds = (
                1_700_000_000 + index
            )
            location = recent_and_network.networkLocations.add()
        location.status = Common_pb2.Status.LAST_KNOWN
        location.geoLocation.accuracy = 5.0
        encrypted_report = location.geoLocation.encryptedReport
        encrypted_report.publicKeyRandom = b""  # empty -> own report
        encrypted_report.encryptedLocation = b"ciphertext-%d" % index
        encrypted_report.isOwnReport = True
    return update


def _plaintext_location() -> bytes:
    location = DeviceUpdate_pb2.Location()
    location.latitude = _LATITUDE_E7
    location.longitude = _LONGITUDE_E7
    return location.SerializeToString()


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: list[bytes],
    offload_result: Callable[[], bytes],
) -> tuple[list[bytes], list[Any]]:
    """Patch key retrieval, the AES offload and ``asyncio.to_thread``.

    Returns the list of keys handed to ``_offload_decrypt_aes`` and the list
    of callables handed to ``asyncio.to_thread``, both in call order.
    """

    offload_keys: list[bytes] = []
    to_thread_calls: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return list(candidates)

    async def fake_offload(identity_key: bytes, _encrypted: bytes) -> bytes:
        offload_keys.append(bytes(identity_key))
        return offload_result()

    def counting_to_thread(
        func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Awaitable[Any]:
        to_thread_calls.append(func)
        return real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload)
    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)
    return offload_keys, to_thread_calls


async def test_t0_single_candidate_success_is_characterized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T0, success: one candidate decrypts the single own report."""

    offload_keys, to_thread_calls = _patch(
        monkeypatch, candidates=[_KEY], offload_result=_plaintext_location
    )

    result = await decrypt_locations.async_decrypt_location_response_locations(
        _build_update(), cache=object()
    )

    assert offload_keys == [_KEY]
    assert len(to_thread_calls) == 0
    assert len(result) == 1
    record = result[0]
    assert record["identity_key"] == _KEY
    assert record["identity_key_candidates"] == [_KEY]
    assert record["latitude"] == pytest.approx(_LATITUDE_E7 / 1e7)
    assert record["longitude"] == pytest.approx(_LONGITUDE_E7 / 1e7)
    assert record["is_own_report"] is True
    assert record["is_network_report"] is False


async def test_t0_single_candidate_all_own_reports_fail_is_characterized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T0, failure: every own report fails, the mismatch error is raised."""

    def raise_invalid_tag() -> bytes:
        raise InvalidTag("authentication tag mismatch")

    offload_keys, to_thread_calls = _patch(
        monkeypatch, candidates=[_KEY], offload_result=raise_invalid_tag
    )

    with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
        await decrypt_locations.async_decrypt_location_response_locations(
            _build_update(own_reports=2), cache=object()
        )

    # Today each own report is tried with the first candidate only, once.
    assert offload_keys == [_KEY, _KEY]
    assert len(to_thread_calls) == 0


# --- AP1: [OWN-REPORT-DIAG] for an own report that fails authentication ---

_DIAG_TAG = "[OWN-REPORT-DIAG]"
# Content bytes of the hand-built field 29; none of them may reach the log.
_F29_CONTENT = bytes(range(0xA0, 0xB0))


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _len_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _time_bytes(seconds: int) -> bytes:
    return Common_pb2.Time(seconds=seconds).SerializeToString()


def _field29() -> bytes:
    """Two entries under number 2, each with content, a time and a flag."""

    entry = (
        _len_field(1, _F29_CONTENT)
        + _len_field(8, _time_bytes(1_600_000_000))
        + _varint_field(3, 1)
    )
    return _len_field(2, entry) + _len_field(2, entry + _varint_field(4, 7))


async def _run_failing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    update: DeviceUpdate_pb2.DeviceUpdate,
) -> list[str]:
    """Run one call whose own reports all fail; return the diagnostic lines."""

    def raise_invalid_tag() -> bytes:
        raise InvalidTag("authentication tag mismatch")

    _patch(monkeypatch, candidates=[_KEY], offload_result=raise_invalid_tag)
    caplog.set_level("DEBUG", logger=decrypt_locations._LOGGER.name)
    with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
        await decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )
    return [r.getMessage() for r in caplog.records if _DIAG_TAG in r.getMessage()]


async def test_t_a1_diag_line_names_report_time_and_registration_scalars(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-A1: one line per failed own report, with time and scalar fields."""

    update = _build_update(own_reports=2)
    registration = update.deviceMetadata.information.deviceRegistration
    registration.isShared = True
    registration.encryptedUserSecrets.ownerKeyVersion = 3

    lines = await _run_failing(monkeypatch, caplog, update)

    assert len(lines) == 2
    # One line per report; the report order is not part of the contract.
    times = sorted(line.split("report_time=", 1)[1].split(" ", 1)[0] for line in lines)
    assert times == ["2023-11-14T22:13:20+00:00", "2023-11-14T22:13:21+00:00"]
    for line in lines:
        assert "owner_key_version=3 " in line
        assert "f14=True " in line
        assert "keys_tried=1 " in line
        assert "f29=absent " in line
        assert "encryptedIdentityKey_len=32" in line
    # The existing per-report line stays, once per own report (EE-14).
    auth_lines = [
        r for r in caplog.records if "Decryption auth failed (InvalidTag)" in r.message
    ]
    assert len(auth_lines) == 2


async def test_t_a2_field29_shows_structure_one_level_deep_and_no_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-A2: field 29 as numbers, wire types and lengths, never a byte."""

    raw = _field29()
    lines = await _run_failing(
        monkeypatch, caplog, _build_update(own_reports=1, field29=raw)
    )

    assert len(lines) == 1
    # Depth pin: the time inside each entry shows its length only.
    expected = (
        f"f29=len={len(raw)}{{2[0]:2:28[1:2:16,8:2:6,3:v],"
        "2[1]:2:30[1:2:16,8:2:6,3:v,4:v]} "
    )
    assert expected in lines[0]
    text = caplog.text
    assert _F29_CONTENT.hex() not in text
    assert _F29_CONTENT[:8].hex() not in text
    assert base64.b64encode(_F29_CONTENT).decode() not in text
    assert repr(_F29_CONTENT)[2:12] not in text


async def test_t_a3_field33_is_read_as_time_only_when_it_parses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-A3: a parsable field 33 gives a time; garbage gives None."""

    good = await _run_failing(
        monkeypatch,
        caplog,
        _build_update(own_reports=1, field33=_time_bytes(1_600_000_000)),
    )
    assert len(good) == 1
    assert "f33=len=6{1:v} " in good[0]
    assert "f33_as_time=2020-09-13T12:26:40+00:00 (meaning unconfirmed)" in good[0]

    caplog.clear()
    bad = await _run_failing(
        monkeypatch, caplog, _build_update(own_reports=1, field33=b"\xff\xff\xff")
    )
    assert len(bad) == 1
    assert "f33=unparsable(len=3) " in bad[0]
    assert "f33_as_time=None (meaning unconfirmed)" in bad[0]


async def test_t_a4_secrets_container_shows_structure_without_key_bytes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-A4: the secrets container is one level of structure, no key byte."""

    update = _build_update(own_reports=1)
    secrets = update.deviceMetadata.information.deviceRegistration.encryptedUserSecrets
    secrets.ownerKeyVersion = 3
    # Key bytes that would parse as a message: only a container that is never
    # opened shows ``1:2:32`` without a bracket.
    key = b"\x08\x01" * 16
    secrets.encryptedIdentityKey = key

    lines = await _run_failing(monkeypatch, caplog, update)

    assert len(lines) == 1
    assert "secrets=len=36{1:2:32,3:v} " in lines[0]
    assert key.hex()[:16] not in caplog.text
    assert "\\x08\\x01" not in caplog.text


async def test_t_a5_helpers_cover_every_wire_type_and_never_raise() -> None:
    """T-A5: fixed64, fixed32, group, the entry cap, and broken input."""

    shape = decrypt_locations._wire_shape
    assert shape(b"\x09" + bytes(8)) == "len=9{1:1:8}"
    assert shape(b"\x15" + bytes(4)) == "len=5{2:5:4}"
    assert shape(b"\x1b\x1c") == "len=2{3:3}"
    # A nested entry that is not a message keeps its length, without brackets.
    assert shape(b"\x1a\x01\xff") == "len=3{3:2:1}"
    capped = shape(b"\x08\x01" * 33)
    assert capped.endswith(",1[31]:v,+1}")
    assert "1[32]" not in capped
    assert shape(b"\xff\xff\xff") == "unparsable(len=3)"
    assert shape(cast(bytes, _Broken())) == "unparsable(len=0)"

    reading = decrypt_locations._time_reading
    assert reading(b"") is None
    assert reading(Common_pb2.Time(nanos=5).SerializeToString()) is None
    assert reading(cast(bytes, _Broken())) is None

    structure = decrypt_locations._own_report_structure
    assert structure(object()) == (False, "absent", "absent", None, "absent")
    container_field = SimpleNamespace(number=19)
    registration = SimpleNamespace(ListFields=lambda: [(container_field, _Broken())])
    assert structure(registration)[4] == "absent"


class _Broken:
    """Input that fails every protobuf call, for the never-raise branches."""

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return True

    def SerializeToString(self) -> bytes:  # noqa: N802 - protobuf method name
        raise RuntimeError("broken")


# --- AP2: own reports try every identity key candidate, and promote ---

_STALE_KEY = b"\x51" * 32
_GOOD_KEY = bytes(range(32, 64))
_PROMOTION_TEXT = "Own report authenticated with alternate identity key candidate"


def _encrypt_own(key: bytes) -> bytes:
    """Encrypt the plaintext location the way an own report is sealed."""

    return encrypt_aes_gcm(hashlib.sha256(key).digest(), _plaintext_location())


def _patch_real_crypto(
    monkeypatch: pytest.MonkeyPatch, *, candidates: list[bytes]
) -> list[bytes]:
    """Patch key retrieval only; record the keys tried, decrypt for real."""

    offload_keys: list[bytes] = []
    real_offload = decrypt_locations._offload_decrypt_aes

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return list(candidates)

    async def spy_offload(identity_key: bytes, encrypted: bytes) -> bytes:
        offload_keys.append(bytes(identity_key))
        return await real_offload(identity_key, encrypted)

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", spy_offload)
    return offload_keys


def _sealed_update(
    key: bytes, *, own_reports: int = 1
) -> DeviceUpdate_pb2.DeviceUpdate:
    update = _build_update(own_reports=own_reports)
    reports = (
        update.deviceMetadata.information.locationInformation.reports
    ).recentLocationAndNetworkLocations
    reports.recentLocation.geoLocation.encryptedReport.encryptedLocation = _encrypt_own(
        key
    )
    for location in reports.networkLocations:
        location.geoLocation.encryptedReport.encryptedLocation = _encrypt_own(key)
    return update


def _promotions(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if _PROMOTION_TEXT in r.getMessage() and r.levelname == "INFO"
    ]


async def test_t_b1_own_report_under_second_candidate_is_decrypted_and_promoted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-B1: the second candidate opens the own report and becomes the key."""

    offload_keys = _patch_real_crypto(monkeypatch, candidates=[_STALE_KEY, _GOOD_KEY])
    caplog.set_level("INFO", logger=decrypt_locations._LOGGER.name)

    result = await decrypt_locations.async_decrypt_location_response_locations(
        _sealed_update(_GOOD_KEY), cache=object()
    )

    assert offload_keys == [_STALE_KEY, _GOOD_KEY]
    assert len(result) == 1
    assert result[0]["identity_key"] == _GOOD_KEY
    assert result[0]["is_own_report"] is True
    assert result[0]["latitude"] == pytest.approx(_LATITUDE_E7 / 1e7)
    assert _promotions(caplog) == [f"{_PROMOTION_TEXT} (index=1)"]


async def test_t_b2_every_candidate_fails_counts_one_failure_per_report(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-B2: both candidates fail; one report, one failure, keys_tried=2."""

    offload_keys = _patch_real_crypto(monkeypatch, candidates=[_STALE_KEY, _GOOD_KEY])
    caplog.set_level("DEBUG", logger=decrypt_locations._LOGGER.name)

    with pytest.raises(decrypt_locations.OwnReportIdentityMismatchError):
        await decrypt_locations.async_decrypt_location_response_locations(
            _sealed_update(b"\x77" * 32), cache=object()
        )

    assert offload_keys == [_STALE_KEY, _GOOD_KEY]
    diag = [r.getMessage() for r in caplog.records if _DIAG_TAG in r.getMessage()]
    assert len(diag) == 1
    assert "keys_tried=2 " in diag[0]
    assert _promotions(caplog) == []


async def test_t_b3_without_a_32_byte_candidate_the_active_key_is_tried_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-B3: no 32-byte candidate (60-byte wrapper); the unwrapped key alone.

    The candidate list then holds only the wrapped key, so the own path falls
    back to the unwrapped identity key and tries it exactly once.
    """

    wrapped = b"\x60" * decrypt_locations.EIK_GCM_TOTAL_LEN
    offload_keys = _patch_real_crypto(monkeypatch, candidates=[wrapped])

    async def fake_owner_key(*_args: object, **_kwargs: object) -> Any:
        return SimpleNamespace(key=b"\x01" * 32)

    monkeypatch.setattr(decrypt_locations, "async_get_owner_key", fake_owner_key)
    monkeypatch.setattr(decrypt_locations, "decrypt_eik", lambda *_a: _GOOD_KEY)
    caplog.set_level("INFO", logger=decrypt_locations._LOGGER.name)

    result = await decrypt_locations.async_decrypt_location_response_locations(
        _sealed_update(_GOOD_KEY), cache=object()
    )

    assert offload_keys == [_GOOD_KEY]
    assert len(result) == 1
    assert result[0]["identity_key"] == _GOOD_KEY
    assert _promotions(caplog) == []


async def test_t_b4_second_own_report_starts_with_the_promoted_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-B4: after the promotion the next own report tries the good key first."""

    offload_keys = _patch_real_crypto(monkeypatch, candidates=[_STALE_KEY, _GOOD_KEY])
    caplog.set_level("INFO", logger=decrypt_locations._LOGGER.name)

    result = await decrypt_locations.async_decrypt_location_response_locations(
        _sealed_update(_GOOD_KEY, own_reports=2), cache=object()
    )

    assert offload_keys == [_STALE_KEY, _GOOD_KEY, _GOOD_KEY]
    assert len(result) == 2
    assert all(record["identity_key"] == _GOOD_KEY for record in result)
    assert len(_promotions(caplog)) == 1
