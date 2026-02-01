# custom_components/googlefindmy/SpotApi/UploadPrecomputedPublicKeyIds/upload_precomputed_public_key_ids.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
import time

from custom_components.googlefindmy.const import MICRO
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    ROTATION_PERIOD,
    EidVariant,
    generate_eid_variant,
)
from custom_components.googlefindmy.FMDNCrypto.mcu_utils import is_mcu_tracker
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    retrieve_identity_key,
)
from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
    DevicesList,
    PublicKeyIdList,
    UploadPrecomputedPublicKeyIdsRequest,
)
from custom_components.googlefindmy.SpotApi.CreateBleDevice.config import (
    max_truncated_eid_seconds_server,
)
from custom_components.googlefindmy.SpotApi.CreateBleDevice.util import hours_to_seconds
from custom_components.googlefindmy.SpotApi.spot_request import spot_request

# Google's server rejects UploadPrecomputedPublicKeyIds requests containing
# more than 40 devices with "Invalid GRPC payload".  Split into batches.
# See upstream: https://github.com/leonboe1/GoogleFindMyTools/issues/37
_MAX_DEVICES_PER_BATCH = 40


def refresh_custom_trackers(device_list: DevicesList) -> None:
    all_device_eids: list[UploadPrecomputedPublicKeyIdsRequest.DevicePublicKeyIds] = []

    for device in device_list.deviceMetadata:
        # This is a microcontroller
        if is_mcu_tracker(device.information.deviceRegistration):
            new_truncated_ids = (
                UploadPrecomputedPublicKeyIdsRequest.DevicePublicKeyIds()
            )
            new_truncated_ids.pairDate = device.information.deviceRegistration.pairDate
            new_truncated_ids.canonicId.id = (
                device.identifierInformation.canonicIds.canonicId[0].id
            )

            try:
                identity_keys = retrieve_identity_key(
                    device.information.deviceRegistration
                )
            except RuntimeError as exc:
                print(
                    "[UploadPrecomputedPublicKeyIds] Identity key refresh requires the async "
                    "API. "
                    f"{exc}"
                )
                return
            if not identity_keys:
                print(
                    "[UploadPrecomputedPublicKeyIds] No identity key candidates derived; "
                    "skipping upload."
                )
                continue
            start_timestamp = int(time.time() - hours_to_seconds(3))

            next_eids = get_next_eids(
                identity_keys[0],
                new_truncated_ids.pairDate,
                start_timestamp,
                duration_seconds=max_truncated_eid_seconds_server,
            )

            for next_eid in next_eids:
                new_truncated_ids.clientList.publicKeyIdInfo.append(next_eid)

            all_device_eids.append(new_truncated_ids)

    if not all_device_eids:
        return

    total = len(all_device_eids)
    batches = [
        all_device_eids[i : i + _MAX_DEVICES_PER_BATCH]
        for i in range(0, total, _MAX_DEVICES_PER_BATCH)
    ]
    num_batches = len(batches)

    print(
        "[UploadPrecomputedPublicKeyIds] Updating your registered "
        f"{MICRO}C devices ({total} device(s) in {num_batches} batch(es))..."
    )

    for batch_idx, batch in enumerate(batches, 1):
        request = UploadPrecomputedPublicKeyIdsRequest()
        for device_eids in batch:
            request.deviceEids.append(device_eids)
        try:
            bytes_data = request.SerializeToString()
            spot_request("UploadPrecomputedPublicKeyIds", bytes_data)
        except Exception as e:
            print(
                f"[UploadPrecomputedPublicKeyIds] Failed to refresh custom trackers "
                f"(batch {batch_idx}/{num_batches}). "
                f"Please file a bug report. Continuing... {str(e)}"
            )


def get_next_eids(
    eik: bytes, pair_date: int, start_date: int, duration_seconds: int
) -> list[PublicKeyIdList.PublicKeyIdInfo]:
    duration_seconds = int(duration_seconds)
    public_key_id_list: list[PublicKeyIdList.PublicKeyIdInfo] = []

    start_offset = start_date - pair_date
    current_time_offset = start_offset - (start_offset % ROTATION_PERIOD)

    static_eid = generate_eid_variant(eik, 0, EidVariant.LEGACY_SECP160R1_X20_BE)

    while current_time_offset <= start_offset + duration_seconds:
        timestamp = pair_date + current_time_offset

        info = PublicKeyIdList.PublicKeyIdInfo()
        info.timestamp.seconds = timestamp
        info.publicKeyId.truncatedEid = static_eid[:10]

        public_key_id_list.append(info)

        current_time_offset += 1024

    return public_key_id_list
