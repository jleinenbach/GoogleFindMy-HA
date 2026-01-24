"""MCU tracker helpers shared across EIK decryptors.

`flip_bits` mirrors the MCU firmware quirk documented for Find My Device
microcontroller trackers: encrypted identity key blobs must be bitwise
inverted (XOR 0xFF) before AES-GCM decryption. This compensates for the MCU's
byte-level inversion step on upload so decryptors receive the ciphertext in the
expected polarity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.googlefindmy.SpotApi.CreateBleDevice.config import (
    mcu_fast_pair_model_id,
)

if TYPE_CHECKING:
    from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
        DeviceRegistration as DeviceRegistrationMessage,
    )
else:
    DeviceRegistrationMessage = Any

# SpotDeviceType.DEVICE_TYPE_BEACON is encoded as `1` in the proto enum and
# identifies the custom MCU trackers registered by the integration.
_MCU_DEVICE_TYPE: int = 1


def flip_bits(data: bytes, enabled: bool) -> bytes:
    """Invert every bit in the payload when MCU handling is enabled."""

    return bytes(b ^ 0xFF for b in data) if enabled else data


def is_mcu_tracker(
    device_registration: DeviceRegistrationMessage | None = None,
    *,
    device_type: int | None = None,
    fast_pair_model_id: str | None = None,
) -> bool:
    """Return True when the tracker requires MCU bit-flip handling.

    MCU trackers register as `DEVICE_TYPE_BEACON` and advertise the dedicated
    Fast Pair model ID used during provisioning. Either signal indicates the
    MCU inversion quirk applies.
    """

    if device_type == _MCU_DEVICE_TYPE:
        return True

    fast_pair_id = fast_pair_model_id
    if fast_pair_id is None and device_registration is not None:
        fast_pair_id = getattr(device_registration, "fastPairModelId", None)

    return fast_pair_id == mcu_fast_pair_model_id
