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
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag

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
