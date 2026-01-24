"""Regression tests for canonical ID extraction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA


class _DummyId:
    """Simple container for canonical ID values."""

    def __init__(self, identifier: str) -> None:
        self.id = identifier


class _DummyIdentifierInformation:
    """Stub identifierInformation payload with both ID layouts present."""

    def __init__(
        self, type_value: int, direct_ids: list[str], phone_ids: list[str]
    ) -> None:
        self.type = type_value
        self.canonicIds = SimpleNamespace(
            canonicId=[_DummyId(identifier) for identifier in direct_ids]
        )
        self.phoneInformation = SimpleNamespace(
            canonicIds=SimpleNamespace(
                canonicId=[_DummyId(identifier) for identifier in phone_ids]
            )
        )


class _DummyDeviceUpdate:
    """Stub DeviceUpdate protobuf with deviceMetadata populated."""

    def __init__(self, info: _DummyIdentifierInformation) -> None:
        self.deviceMetadata = SimpleNamespace(identifierInformation=info)

    def HasField(self, name: str) -> bool:  # noqa: N802
        return name == "deviceMetadata"


def test_extract_canonic_id_android_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Android responses use phoneInformation.canonicIds when type == 1."""

    receiver = FcmReceiverHA()
    expected_id = "android-canonic-id"

    info = _DummyIdentifierInformation(
        type_value=1,
        direct_ids=["tracker-id"],
        phone_ids=[expected_id],
    )
    device_update = _DummyDeviceUpdate(info)

    monkeypatch.setattr(
        fcm_receiver_ha.decoder_module,
        "parse_device_update_protobuf",
        lambda _hex: device_update,
    )

    assert receiver._extract_canonic_id_from_response("00ff") == expected_id
