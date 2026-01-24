# tests/test_decoder_anchor_dates.py
from __future__ import annotations

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    get_devices_with_location,
)

try:
    # Optional: TokenCache only used for typing in other tests; we don't need a real instance here.
    from custom_components.googlefindmy.Auth.token_cache import (
        TokenCache,  # type: ignore
    )
except Exception:  # pragma: no cover
    TokenCache = object  # type: ignore[misc,assignment]


def _build_device_with_registration(
    *,
    canonic_id: str,
    pair_date: int | None,
    set_creation_date: bool,
    creation_seconds: int | None,
    creation_nanos: int | None = None,
) -> DeviceUpdate_pb2.DevicesList:
    devices_list = DeviceUpdate_pb2.DevicesList()
    device = devices_list.deviceMetadata.add()
    device.userDefinedDeviceName = "Tracker"

    canonic = device.identifierInformation.canonicIds.canonicId.add()
    canonic.id = canonic_id

    registration = device.information.deviceRegistration
    secrets = registration.encryptedUserSecrets

    # Ensure the secrets container exists / is considered meaningful.
    secrets.encryptedIdentityKey = b"\x01\x02\x03"
    secrets.ownerKeyVersion = 1

    # Proto3 scalar: absent looks like 0. We only set it if asked.
    if pair_date is not None:
        registration.pairDate = int(pair_date)

    # Message field: may appear as default Timestamp object even if unset.
    # This is the core regression we want to catch.
    if set_creation_date:
        # Setting any subfield should mark creationDate as present.
        secrets.creationDate.seconds = int(creation_seconds or 0)
        if creation_nanos is not None:
            secrets.creationDate.nanos = int(creation_nanos)

    return devices_list


def test_decoder_does_not_emit_epoch_secrets_creation_date_when_unset() -> None:
    """
    Regression guard:
    If encryptedUserSecrets.creationDate is absent, protobuf may still expose a default Timestamp
    object (seconds==0). decoder.py must NOT treat that as a real anchor and must keep
    secrets_creation_date unset/None in the row.
    """
    devices_list = _build_device_with_registration(
        canonic_id="device-unset-creationDate",
        pair_date=1_749_216_525,
        set_creation_date=False,
        creation_seconds=None,
    )

    # Pass cache=None so decryption path is skipped (we only validate registration extraction).
    rows = get_devices_with_location(devices_list, cache=None)

    assert len(rows) == 1
    row = rows[0]

    # Pair date is a real scalar we did set.
    assert row["pair_date"] == 1_749_216_525

    # The bug would set this to 0; we must see None.
    assert row["secrets_creation_date"] is None
    assert row["secrets_creation_date"] != 0


def test_decoder_ignores_default_zero_creation_date_even_if_present() -> None:
    """
    Some refactorings may accidentally set creationDate to default (seconds=0/nanos=0).
    Even if HasField('creationDate') becomes True, a pure default timestamp must still be ignored.
    """
    devices_list = _build_device_with_registration(
        canonic_id="device-default-creationDate",
        pair_date=1_749_216_525,
        set_creation_date=True,
        creation_seconds=0,
        creation_nanos=0,
    )

    rows = get_devices_with_location(devices_list, cache=None)

    assert len(rows) == 1
    row = rows[0]

    assert row["pair_date"] == 1_749_216_525
    assert row["secrets_creation_date"] is None
    assert row["secrets_creation_date"] != 0


def test_decoder_emits_secrets_creation_date_when_set_to_real_timestamp() -> None:
    """
    Positive control:
    When creationDate is present with a real epoch seconds value, decoder.py should pass it through.
    """
    devices_list = _build_device_with_registration(
        canonic_id="device-real-creationDate",
        pair_date=1_749_216_525,
        set_creation_date=True,
        creation_seconds=1_764_960_360,
        creation_nanos=800_213_023,
    )

    rows = get_devices_with_location(devices_list, cache=None)

    assert len(rows) == 1
    row = rows[0]

    assert row["pair_date"] == 1_749_216_525
    assert row["secrets_creation_date"] == 1_764_960_360
