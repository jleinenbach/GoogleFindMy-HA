#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from custom_components.googlefindmy.ProtoDecoders import Common_pb2
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.SpotApi.spot_request import spot_request
import logging

_LOGGER = logging.getLogger(__name__)

def get_eid_info():
    get_eid_info_for_e2ee_devices_request = Common_pb2.GetEidInfoForE2eeDevicesRequest()
    get_eid_info_for_e2ee_devices_request.ownerKeyVersion = -1
    get_eid_info_for_e2ee_devices_request.hasOwnerKeyVersion = True

    serialized_request = get_eid_info_for_e2ee_devices_request.SerializeToString()
    response_bytes = spot_request("GetEidInfoForE2eeDevices", serialized_request)

    # Defensive checks + diagnostics for trailers-only / non-gRPC / empty payloads
    if not response_bytes or len(response_bytes) < 1:
        _LOGGER.warning(
            "GetEidInfoForE2eeDevices: empty/none response (len=%s, pre=%s)",
            0 if not response_bytes else len(response_bytes),
            b"".hex() if not response_bytes else response_bytes[:16].hex(),
        )
        raise RuntimeError("Empty gRPC body (possibly trailers-only) for GetEidInfoForE2eeDevices")

    eid_info = DeviceUpdate_pb2.GetEidInfoForE2eeDevicesResponse()
    try:
        eid_info.ParseFromString(response_bytes)
    except Exception as exc:
        _LOGGER.warning(
            "GetEidInfoForE2eeDevices: ParseFromString failed (len=%s, pre=%s)",
            len(response_bytes),
            response_bytes[:16].hex(),
        )
        raise

    return eid_info


if __name__ == '__main__':
    print(get_eid_info().encryptedOwnerKeyAndMetadata)
