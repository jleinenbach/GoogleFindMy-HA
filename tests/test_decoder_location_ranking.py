# tests/test_decoder_location_ranking.py (snippet)
#
# The assertions are designed to be refactoring-robust and to prevent epoch-0
# contamination when EncryptedUserSecrets.creationDate is absent or default.

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    get_devices_with_location,
)

try:
    # Only needed for typing / consistency with other tests.
    from custom_components.googlefindmy.Auth.token_cache import (
        TokenCache,  # type: ignore
    )
except Exception:  # pragma: no cover
    TokenCache = Any  # type: ignore[misc,assignment]


_DECRYPT_LOCATIONS_TARGET = (
    "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations."
    "decrypt_location_response_locations"
)


def _build_single_device_with_seed_report(
    *,
    device_name: str,
    canonic_id: str,
) -> tuple[DeviceUpdate_pb2.DevicesList, Any]:
    """Return (DevicesList, deviceMetadata) with a semantic report so decryption runs."""

    devices_list = DeviceUpdate_pb2.DevicesList()
    device = devices_list.deviceMetadata.add()
    device.userDefinedDeviceName = device_name

    canonic = device.identifierInformation.canonicIds.canonicId.add()
    canonic.id = canonic_id

    # Ensure has_reports=True inside decoder so the decrypt hook is executed.
    reports = device.information.locationInformation.reports
    reports.recentLocationAndNetworkLocations.recentLocation.semanticLocation.locationName = "seed"

    return devices_list, device


def _prepare_registration_secrets(device: Any) -> Any:
    """Ensure deviceRegistration.encryptedUserSecrets is present for HasField checks."""

    registration = device.information.deviceRegistration
    secrets = (
        registration.encryptedUser_secrets
        if hasattr(registration, "encryptedUser_secrets")
        else registration.encryptedUserSecrets
    )

    # Make the submessage present in proto3 so registration.HasField('encryptedUserSecrets') is True.
    # A single non-default field is sufficient.
    secrets.encryptedIdentityKey = b"\x01"
    secrets.ownerKeyVersion = 1
    return registration


def test_device_list_preserves_anchor_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metadata from decrypted device list payloads must survive filtering."""

    devices_list, _device = _build_single_device_with_seed_report(
        device_name="Tracker",
        canonic_id="device-anchor",
    )

    # Provide minimal ranking fields so selection/merge doesn't discard the candidate.
    anchor_payload = {
        "status": "aggregated",
        "last_seen": 1_700_000_150,
        # Anchor + diagnostics payload:
        "pair_date": 1_700_000_100,
        "secrets_creation_date": 1_700_000_200,
        "identity_key": b"\xaa" * 32,
        "identity_key_candidates": [b"\xaa" * 32, b"\xbb" * 32],
        "encrypted_identity_key_candidates": [b"\x01\x02"],
        "device_registration": {"pairDate": 1_700_000_300},
        "encrypted_user_secrets": {"creationDate": 1_700_000_400},
        "time_anchors_debug": {"source": "device_list"},
        "metadata_only": True,
    }

    # Expected values after decoder normalizes bytes to hex strings
    expected_values = {
        "pair_date": 1_700_000_100,
        "secrets_creation_date": 1_700_000_200,
        # decoder.py converts bytes to hex strings for consistency
        "identity_key": "aa" * 32,
        # identity_key_candidates keeps bytes (list union preserves bytes)
        "identity_key_candidates": [b"\xaa" * 32, b"\xbb" * 32],
        "encrypted_identity_key_candidates": [b"\x01\x02"],
        "device_registration": {"pairDate": 1_700_000_300},
        "encrypted_user_secrets": {"creationDate": 1_700_000_400},
        "time_anchors_debug": {"source": "device_list"},
        "metadata_only": True,
    }

    with patch(_DECRYPT_LOCATIONS_TARGET, return_value=[anchor_payload]) as mocked:
        rows = get_devices_with_location(
            devices_list,
            cache=cast("TokenCache", object()),
        )

    assert mocked.call_count == 1
    assert len(rows) == 1
    row = rows[0]

    # Core identity invariants.
    assert row["device_id"] == "device-anchor"
    assert row["id"] == "device-anchor"
    assert row["name"] == "Tracker"

    # Anchor + diagnostics must survive the device-row filtering/shielding.
    for key in (
        "pair_date",
        "secrets_creation_date",
        "identity_key",
        "identity_key_candidates",
        "encrypted_identity_key_candidates",
        "device_registration",
        "encrypted_user_secrets",
        "time_anchors_debug",
        "metadata_only",
    ):
        assert row[key] == expected_values[key]


@pytest.mark.parametrize(
    ("set_creation_date", "creation_seconds", "creation_nanos", "expected"),
    [
        # 1) Absent creationDate: must not become 0.
        (False, None, None, None),
        # 2) Present but default/zero: must be ignored (no epoch contamination).
        (True, 0, 0, None),
        # 3) Real timestamp: should propagate.
        (True, 1_800_000_222, 0, 1_800_000_222),
    ],
)
def test_device_list_registration_anchor_fields_propagate(
    set_creation_date: bool,
    creation_seconds: int | None,
    creation_nanos: int | None,
    expected: int | None,
) -> None:
    """Anchors embedded in DeviceRegistration should populate device rows without epoch contamination."""

    devices_list, device = _build_single_device_with_seed_report(
        device_name="Tracker",
        canonic_id="device-registration",
    )

    registration = _prepare_registration_secrets(device)

    # pairDate is a scalar in proto3 (unset -> 0), so we only set a non-zero value here.
    registration.pairDate = 1_800_000_111

    secrets = (
        registration.encryptedUser_secrets
        if hasattr(registration, "encryptedUser_secrets")
        else registration.encryptedUserSecrets
    )
    if set_creation_date:
        # Any subfield assignment makes HasField('creationDate') true.
        secrets.creationDate.seconds = int(creation_seconds or 0)
        if creation_nanos is not None:
            secrets.creationDate.nanos = int(creation_nanos)

    with patch(_DECRYPT_LOCATIONS_TARGET, return_value=[]):
        rows = get_devices_with_location(
            devices_list,
            cache=cast("TokenCache", object()),
        )

    assert len(rows) == 1
    row = rows[0]

    assert row["device_id"] == "device-registration"
    assert row["pair_date"] == 1_800_000_111
    assert row["secrets_creation_date"] == expected
    assert row["secrets_creation_date"] != 0
