# tests/test_time_anchor_e2e_pipeline.py
"""End-to-end anchor propagation from proto to resolver.

This test ensures pairDate/creationDate anchors survive the decrypt → decoder →
coordinator → resolver pipeline so relative EIDs can be generated.
"""
from __future__ import annotations

from typing import Any, cast

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import (
    DeviceIdentity,
    GoogleFindMyCoordinator,
)
from custom_components.googlefindmy.eid_resolver import (
    TimebaseLabel,
    _build_timebase_candidates,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as dl,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2


class _DummyCache:
    """Minimal cache stub for decrypt_locations/coordinator."""

    async def get_json(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        return None

    async def set_json(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_proto_anchors_reach_resolver_candidates(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair/secrets anchors must flow into REL candidates."""

    canonic_id = "device-anchor"
    pair_date = 1_749_216_525
    secrets_creation = 1_764_960_360

    device_update = DeviceUpdate_pb2.DeviceUpdate()
    device_update.deviceMetadata.userDefinedDeviceName = "Tracker"
    cid = device_update.deviceMetadata.identifierInformation.canonicIds.canonicId.add()
    cid.id = canonic_id

    devreg = device_update.deviceMetadata.information.deviceRegistration
    devreg.pairDate = pair_date
    secrets = devreg.encryptedUserSecrets
    secrets.ownerKeyVersion = 1
    secrets.encryptedIdentityKey = b"\x01" * 60
    secrets.creationDate.seconds = secrets_creation
    secrets.creationDate.nanos = 800_213_023

    async def _fake_unwrap(*_args: Any, **_kwargs: Any) -> bytes:
        return b"\xaa" * 32

    monkeypatch.setattr(dl, "_unwrap_encrypted_identity_key", _fake_unwrap)

    rows = await dl.async_decrypt_location_response_locations(
        device_update, cache=_DummyCache()
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.get("pair_date") == pair_date
    assert row.get("secrets_creation_date") == secrets_creation
    assert isinstance(row.get("identity_key"), (bytes, bytearray))
    assert len(row["identity_key"]) == 32

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True  # noqa: SLF001
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None  # noqa: SLF001
    coordinator.stats = {}
    coordinator._device_location_data = {}  # noqa: SLF001
    coordinator._device_update_history = {}  # noqa: SLF001
    coordinator._enabled_poll_device_ids = {canonic_id}  # noqa: SLF001
    coordinator._last_device_list = []  # noqa: SLF001
    coordinator.data = []
    coordinator.config_entry = type("_E", (), {"entry_id": "entry-id", "options": {}})()
    coordinator.hass = hass
    coordinator._extract_our_identifier = (  # noqa: SLF001
        lambda device: next(
            (identifier for domain, identifier in getattr(device, "identifiers", set()) if domain == DOMAIN),
            None,
        )
    )

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, canonic_id)},
        name="Tracker",
        manufacturer="Test",
        model="Anchor",
    )

    coordinator.update_device_cache(canonic_id, row)

    identities = coordinator.get_active_device_identities()
    identity = cast(DeviceIdentity, next(i for i in identities if i.canonical_id == canonic_id))

    assert identity.pair_date == pair_date
    assert identity.secrets_creation_date == secrets_creation
    assert identity.identity_key is not None
    assert len(bytes(identity.identity_key)) == 32

    candidates = _build_timebase_candidates(identity, now_unix=secrets_creation + 30)
    labels = {c.label for c in candidates}
    assert TimebaseLabel.REL_PAIR in labels
    assert TimebaseLabel.REL_SECRETS in labels
    assert TimebaseLabel.ABSOLUTE in labels
