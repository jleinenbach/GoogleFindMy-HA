# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_status.py
"""Battery level and Wi-Fi name from the push that answers phone action 4.

The phone record of that push carries, besides the encrypted location (field 36),
the battery level (field 32.1, percent) and the name of the Wi-Fi network the
phone is connected to (field 30.1, in double quotes). Both were measured
against the phone's own display. They are not encrypted and do not depend on the
location: a push whose location is missing or does not decrypt still carries them.

Rules applied here, and nowhere else:

* A battery level outside 0 to 100 is dropped; a record without the field
  carries no level. proto3 does not put a zero on the wire, so 0 % cannot be
  told apart from a missing level and reads as none.
* The Wi-Fi name loses one pair of enclosing double quotes. Android's
  placeholder ``<unknown ssid>``, an empty name, a name longer than the 32 octets
  an SSID can have, a name that is not UTF-8 and a name with control characters
  count as no name.
* The SIM record (field 31) is not read.

The Wi-Fi name is personal data: it is never logged, and :class:`PhoneStatus`
keeps it out of its ``repr``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from google.protobuf.message import DecodeError

PHONE_BATTERY_MIN_PCT = 0
PHONE_BATTERY_MAX_PCT = 100
# IEEE 802.11: an SSID has at most 32 octets.
PHONE_WIFI_SSID_MAX_OCTETS = 32
# What Android reports as the name while the phone is not connected.
_ANDROID_UNKNOWN_SSID = "<unknown ssid>"


@dataclass(frozen=True)
class PhoneStatus:
    """What one phone push says about the phone besides its location.

    ``observed_at`` is the wall-clock time (``time.time()``) when the push was
    received. ``None`` for a value means the push did not carry a usable one;
    a status with neither value still replaces the stored one.
    """

    battery_pct: int | None
    wifi_ssid: str | None = field(repr=False)
    observed_at: float


def _pb2() -> Any:
    return import_module(
        "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2"
    )


def battery_from_field(raw: bytes) -> int | None:
    """Return the battery level of field 32 in percent, or ``None``.

    ``None`` when the field is empty, does not parse, has no level or a level
    outside 0 to 100.
    """
    if not raw:
        return None
    message = _pb2().PhoneBatteryInformation()
    try:
        message.ParseFromString(raw)
    except DecodeError:
        return None
    if not any(desc.name == "batteryLevel" for desc, _ in message.ListFields()):
        return None
    level = int(message.batteryLevel)
    if not PHONE_BATTERY_MIN_PCT <= level <= PHONE_BATTERY_MAX_PCT:
        return None
    return level


def _strip_enclosing_quotes(octets: bytes) -> bytes:
    """Remove one pair of enclosing double quotes, if both are there."""
    if octets.startswith(b'"') and octets.endswith(b'"') and len(octets) > 1:
        return octets[1:-1]
    return octets


def _usable_name(octets: bytes) -> str | None:
    """Return ``octets`` as a Wi-Fi name, or ``None`` when it is not one."""
    if not octets or len(octets) > PHONE_WIFI_SSID_MAX_OCTETS:
        return None
    try:
        name = octets.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if name == _ANDROID_UNKNOWN_SSID:
        return None
    if any(unicodedata.category(char) == "Cc" for char in name):
        return None
    return name


def ssid_from_field(raw: bytes) -> str | None:
    """Return the Wi-Fi name of field 30 without enclosing quotes, or ``None``."""
    if not raw:
        return None
    message = _pb2().PhoneWifiInformation()
    try:
        message.ParseFromString(raw)
    except DecodeError:
        return None
    return _usable_name(_strip_enclosing_quotes(bytes(message.ssid)))


def status_from_record(record: Any, *, observed_at: float) -> PhoneStatus:
    """Return the status carried by one decoded phone record (``PhoneInformation``)."""
    return PhoneStatus(
        battery_pct=battery_from_field(bytes(record.batteryInformation)),
        wifi_ssid=ssid_from_field(bytes(record.wifiInformation)),
        observed_at=observed_at,
    )
