"""Test that phone devices don't trigger 'Missing Key' warnings.

Phone devices (IDENTIFIER_ANDROID) legitimately don't have keys in the device
listing payload from nbe_list_devices. The keys are provided via the Locate
flow (FCM response) instead. This test verifies that:

1. Phone devices don't trigger "Missing Key" warnings
2. Tracker devices without keys still trigger warnings
3. The identity_key from Locate flow is properly propagated
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2


def _make_phone_device(name: str = "Galaxy S25 Ultra") -> SimpleNamespace:
    """Create a mock phone device (IDENTIFIER_ANDROID) with reduced fields."""
    return SimpleNamespace(
        identifierInformation=SimpleNamespace(
            type=DeviceUpdate_pb2.IDENTIFIER_ANDROID,
            phoneInformation=SimpleNamespace(
                canonicIds=SimpleNamespace(
                    canonicId=[SimpleNamespace(id="phone-uuid-123")]
                )
            ),
        ),
        userDefinedDeviceName=name,
        imageInformation=SimpleNamespace(url=""),
        # Notably, phone devices do NOT have the 'information' field
        HasField=lambda field_name: (
            field_name not in ("information", "deviceRegistration")
        ),
        ListFields=lambda: [
            (SimpleNamespace(name="identifierInformation"), None),
            (SimpleNamespace(name="userDefinedDeviceName"), None),
            (SimpleNamespace(name="imageInformation"), None),
        ],
    )


def _make_tracker_device_no_key(name: str = "Moto Tag") -> SimpleNamespace:
    """Create a mock tracker device with 'information' but missing key."""
    encrypted_user_secrets = SimpleNamespace(
        encryptedIdentityKey=b"",  # Empty key
        ownerKeyVersion=0,
        HasField=lambda field_name: False,
        ListFields=lambda: [],
    )
    device_registration = SimpleNamespace(
        encryptedUserSecrets=encrypted_user_secrets,
        HasField=lambda field_name: field_name == "encryptedUserSecrets",
        ListFields=lambda: [
            (SimpleNamespace(name="encryptedUserSecrets"), None),
        ],
    )
    information = SimpleNamespace(
        deviceRegistration=device_registration,
        HasField=lambda field_name: field_name == "deviceRegistration",
        ListFields=lambda: [
            (SimpleNamespace(name="deviceRegistration"), None),
        ],
    )
    return SimpleNamespace(
        identifierInformation=SimpleNamespace(
            type=2,  # Not IDENTIFIER_ANDROID
            canonicIds=SimpleNamespace(
                canonicId=[SimpleNamespace(id="tracker-uuid-456")]
            ),
        ),
        information=information,
        userDefinedDeviceName=name,
        imageInformation=SimpleNamespace(url=""),
        HasField=lambda field_name: field_name == "information",
        ListFields=lambda: [
            (SimpleNamespace(name="identifierInformation"), None),
            (SimpleNamespace(name="information"), None),
            (SimpleNamespace(name="userDefinedDeviceName"), None),
            (SimpleNamespace(name="imageInformation"), None),
        ],
    )


def _make_tracker_device_with_key(name: str = "Moto Tag") -> SimpleNamespace:
    """Create a mock tracker device with proper key material."""
    encrypted_user_secrets = SimpleNamespace(
        encryptedIdentityKey=b"0" * 60,  # 60-byte encrypted key
        ownerKeyVersion=1,
        HasField=lambda field_name: field_name == "encryptedIdentityKey",
        ListFields=lambda: [
            (SimpleNamespace(name="encryptedIdentityKey"), None),
            (SimpleNamespace(name="ownerKeyVersion"), None),
        ],
    )
    device_type_info = SimpleNamespace(deviceType=1)  # Tracker type
    device_registration = SimpleNamespace(
        encryptedUserSecrets=encrypted_user_secrets,
        deviceTypeInformation=device_type_info,
        HasField=lambda field_name: (
            field_name in ("encryptedUserSecrets", "deviceTypeInformation")
        ),
        ListFields=lambda: [
            (SimpleNamespace(name="encryptedUserSecrets"), None),
            (SimpleNamespace(name="deviceTypeInformation"), None),
        ],
    )
    information = SimpleNamespace(
        deviceRegistration=device_registration,
        HasField=lambda field_name: field_name == "deviceRegistration",
        ListFields=lambda: [
            (SimpleNamespace(name="deviceRegistration"), None),
        ],
    )
    return SimpleNamespace(
        identifierInformation=SimpleNamespace(
            type=2,  # Not IDENTIFIER_ANDROID
            canonicIds=SimpleNamespace(
                canonicId=[SimpleNamespace(id="tracker-uuid-789")]
            ),
        ),
        information=information,
        userDefinedDeviceName=name,
        imageInformation=SimpleNamespace(url=""),
        HasField=lambda field_name: field_name == "information",
        ListFields=lambda: [
            (SimpleNamespace(name="identifierInformation"), None),
            (SimpleNamespace(name="information"), None),
            (SimpleNamespace(name="userDefinedDeviceName"), None),
            (SimpleNamespace(name="imageInformation"), None),
        ],
    )


def test_phone_device_no_missing_key_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Phone devices should NOT trigger 'Missing Key' warnings.

    Phones get their keys from the Locate flow (FCM), not from list_devices.

    Models the main poll (emit_canonicless_diagnostics=True): the hidden-key diagnostic
    block (both its WARNING and its benign "Locate flow" DEBUG line) is gated to the main
    poll, so the benign DEBUG line this test asserts on only appears there. The probe pass
    silence is pinned separately in test_decoder_canonicless_device_warning.py (T5/T6).
    """
    from custom_components.googlefindmy.ProtoDecoders import decoder

    phone_device = _make_phone_device()
    device_list = SimpleNamespace(deviceMetadata=[phone_device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        # Use cache=None to skip decryption attempts
        results = decoder.get_devices_with_location(
            device_list, cache=None, emit_canonicless_diagnostics=True
        )

    # Should produce a result (device was parsed)
    assert len(results) == 1
    assert results[0]["name"] == "Galaxy S25 Ultra"

    # Should NOT have "Missing Key" warning (WARNING level)
    warning_messages = [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert not any("Missing Key" in msg for msg in warning_messages), (
        f"Phone device should not trigger 'Missing Key' warning. "
        f"Warnings: {warning_messages}"
    )

    # Should have debug message about keys coming from Locate flow
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "keys will be obtained via Locate flow" in msg for msg in debug_messages
    ), (
        f"Phone device should have debug message about Locate flow. "
        f"Debug messages: {debug_messages}"
    )


def test_tracker_device_with_key_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Tracker devices with keys should NOT trigger any key-related warnings."""
    from custom_components.googlefindmy.ProtoDecoders import decoder

    tracker_device = _make_tracker_device_with_key()
    device_list = SimpleNamespace(deviceMetadata=[tracker_device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert len(results) == 1
    assert results[0]["name"] == "Moto Tag"
    # Should have the encrypted identity key
    assert results[0]["encrypted_identity_key"] is not None

    # No "Missing Key" warning
    warning_messages = [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert not any("Missing Key" in msg for msg in warning_messages)


def test_tracker_device_missing_key_triggers_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tracker devices without keys SHOULD trigger 'Missing Key' warnings.

    This is the expected behavior for trackers that should have keys.

    Models the main poll (emit_canonicless_diagnostics=True): the hidden-key diagnostic is
    gated to the main poll so the capability probe stays diagnostically silent (CQS). The
    probe-pass silence is pinned separately in test_decoder_canonicless_device_warning.py.
    """
    from custom_components.googlefindmy.ProtoDecoders import decoder

    tracker_device = _make_tracker_device_no_key()
    device_list = SimpleNamespace(deviceMetadata=[tracker_device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(
            device_list, cache=None, emit_canonicless_diagnostics=True
        )

    assert len(results) == 1
    assert results[0]["name"] == "Moto Tag"
    # No encrypted identity key (it's missing)
    assert results[0]["encrypted_identity_key"] is None

    # SHOULD have "Missing Key" warning for trackers
    warning_messages = [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("Missing Key" in msg for msg in warning_messages), (
        f"Tracker device without key should trigger 'Missing Key' warning. "
        f"Warnings: {warning_messages}"
    )
