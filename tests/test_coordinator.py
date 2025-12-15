# tests/test_coordinator.py

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import coordinator as coordinator_module
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


class _Recorder:
    """Callable stub that records kwargs and returns a static response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return "ok"


class _TypeErrorRaiser:
    """Callable stub that records kwargs and raises TypeError."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        raise TypeError("unexpected keyword argument 'config_subentry_id'")


class _AddConfigSubentryRecorder:
    """Callable stub requiring ``add_config_subentry_id`` keyword."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        add_config_subentry_id: str,
        **kwargs: Any,
    ) -> str:
        payload = {"add_config_subentry_id": add_config_subentry_id}
        if kwargs:
            payload.update(kwargs)
        self.calls.append(payload)
        return "ok"


class _AddConfigEntryFallbackRecorder:
    """Recorder that forces a retry when ``add_config_entry_id`` is provided."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if "add_config_entry_id" in kwargs:
            raise TypeError("unexpected keyword argument 'add_config_entry_id'")
        assert "config_entry_id" in kwargs
        return "ok"


class _AddConfigSubentryFallbackRecorder:
    """Recorder that forces a retry when ``add_config_subentry_id`` is provided."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if "add_config_subentry_id" in kwargs:
            raise TypeError("unexpected keyword argument 'add_config_subentry_id'")
        assert "config_subentry_id" in kwargs
        return "ok"


class _RemoveConfigSubentryFallbackRecorder:
    """Recorder that forces a retry when ``remove_config_subentry_id`` is provided."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if "remove_config_subentry_id" in kwargs:
            raise TypeError("unexpected keyword argument 'remove_config_subentry_id'")
        assert "remove_config_entry_id" in kwargs
        return "ok"


def test_device_registry_wrapper_passes_kwargs() -> None:
    """The wrapper should pass kwargs to the underlying callable unchanged."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    recorder = _Recorder()

    payload: dict[str, Any] = {
        "config_entry_id": "entry-1",
        "identifiers": {("domain", "identifier")},
        "config_subentry_id": "service-subentry",
        "translation_key": "service",
        "translation_placeholders": {},
    }

    result = coordinator._call_device_registry_api(  # type: ignore[attr-defined]
        recorder,
        base_kwargs=payload,
    )

    assert result == "ok"
    assert recorder.calls == [payload]


def test_device_registry_wrapper_does_not_swallow_typeerror() -> None:
    """The wrapper should propagate TypeError from the underlying callable."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    raiser = _TypeErrorRaiser()

    payload: dict[str, Any] = {
        "config_entry_id": "entry-2",
        "identifiers": {("domain", "identifier-2")},
        "config_subentry_id": "tracker-subentry",
    }

    with pytest.raises(TypeError):
        coordinator._call_device_registry_api(  # type: ignore[attr-defined]
            raiser,
            base_kwargs=payload,
        )

    assert raiser.calls == [payload]


def test_device_registry_wrapper_maps_config_subentry_kwarg() -> None:
    """The wrapper should translate ``config_subentry_id`` to the new keyword."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    recorder = _AddConfigSubentryRecorder()

    payload: dict[str, Any] = {
        "device_id": "device-id",
        "config_subentry_id": "service-subentry",
        "translation_key": "service",
    }

    result = coordinator._call_device_registry_api(  # type: ignore[attr-defined]
        recorder,
        base_kwargs=payload,
    )

    assert result == "ok"
    assert recorder.calls == [
        {
            "device_id": "device-id",
            "add_config_subentry_id": "service-subentry",
            "translation_key": "service",
        }
    ]


def test_device_registry_wrapper_retries_with_legacy_config_entry_kwarg() -> None:
    """The wrapper retries with ``config_entry_id`` when modern kwarg fails."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    recorder = _AddConfigEntryFallbackRecorder()

    payload: dict[str, Any] = {
        "device_id": "device-id",
        "config_subentry_id": "service-subentry",
        "add_config_entry_id": "entry-id",
    }

    result = coordinator._call_device_registry_api(  # type: ignore[attr-defined]
        recorder,
        base_kwargs=payload,
    )

    assert result == "ok"
    assert recorder.calls == [
        {
            "device_id": "device-id",
            "config_subentry_id": "service-subentry",
            "add_config_entry_id": "entry-id",
        },
        {
            "device_id": "device-id",
            "config_subentry_id": "service-subentry",
            "config_entry_id": "entry-id",
        },
    ]


def test_device_registry_wrapper_retries_with_legacy_config_subentry_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modern kwarg failures bubble when registry already prefers new keyword."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    recorder = _AddConfigSubentryFallbackRecorder()

    payload: dict[str, Any] = {
        "device_id": "device-id",
        "config_subentry_id": "service-subentry",
    }

    monkeypatch.setattr(
        coordinator,
        "_device_registry_config_subentry_kwarg_name",
        lambda call: "add_config_subentry_id",
    )

    with pytest.raises(TypeError):
        coordinator._call_device_registry_api(  # type: ignore[attr-defined]
            recorder,
            base_kwargs=payload,
        )

    assert recorder.calls == [
        {
            "device_id": "device-id",
            "add_config_subentry_id": "service-subentry",
        }
    ]


# fmt: off


def test_device_registry_wrapper_retries_with_legacy_remove_config_subentry_kwarg() -> None:
    """The wrapper retries without ``remove_config_subentry_id`` on legacy cores."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    recorder = _RemoveConfigSubentryFallbackRecorder()

    payload: dict[str, Any] = {
        "device_id": "device-id",
        "remove_config_entry_id": "entry-id",
        "remove_config_subentry_id": None,
    }

    result = coordinator._call_device_registry_api(  # type: ignore[attr-defined]
        recorder,
        base_kwargs=payload,
    )

    assert result == "ok"
    assert recorder.calls == [
        {
            "device_id": "device-id",
            "remove_config_entry_id": "entry-id",
            "remove_config_subentry_id": None,
        },
        {
            "device_id": "device-id",
            "remove_config_entry_id": "entry-id",
        },
    ]

# fmt: on


def test_coordinator_propagates_timestamps_to_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metadata-only updates should populate resolver identities with timestamps."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None
    coordinator.stats = {}
    coordinator._device_location_data = {}
    coordinator._device_update_history = {}
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator._last_device_list = []
    coordinator.data = []
    coordinator.config_entry = SimpleNamespace(entry_id="entry-id", options={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator._extract_our_identifier = lambda device: device.custom_fields.get(
        "canonical_id"
    )

    metadata_only_payload = {
        "identity_key": b"\x01\x02\x03\x04",
        "pair_date": 1_700_000_000,
        "metadata_only": True,
    }

    coordinator.update_device_cache("device-1", metadata_only_payload)

    registry = SimpleNamespace()
    registry_device = SimpleNamespace(
        id="registry-id",
        disabled_by=None,
        custom_fields={"canonical_id": "device-1"},
    )

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: [registry_device],
    )

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == metadata_only_payload["pair_date"]
    assert identity.identity_key == metadata_only_payload["identity_key"]


def test_coordinator_persists_camelCase_identity_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CamelCase identity metadata should persist and feed resolver identities."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None
    coordinator.stats = {}
    coordinator._device_location_data = {}
    coordinator._device_update_history = {}
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator._last_device_list = []
    coordinator.data = []
    coordinator.config_entry = SimpleNamespace(entry_id="entry-id", options={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator._extract_our_identifier = lambda device: device.custom_fields.get(
        "canonical_id"
    )

    camel_payload = {
        "identityKey": b"\x05\x06\x07\x08",
        "pairDate": 1_800_000_000,
        "metadata_only": True,
    }

    coordinator.update_device_cache("device-1", camel_payload)

    assert (
        coordinator._device_location_data["device-1"]["identity_key"]
        == (camel_payload["identityKey"])
    )
    assert (
        coordinator._device_location_data["device-1"]["pair_date"]
        == camel_payload["pairDate"]
    )

    registry = SimpleNamespace()
    registry_device = SimpleNamespace(
        id="registry-id",
        disabled_by=None,
        custom_fields={"canonical_id": "device-1"},
    )

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: [registry_device],
    )

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == camel_payload["pairDate"]
    assert identity.identity_key == camel_payload["identityKey"]


def test_coordinator_yields_encrypted_only_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow encrypted identity keys to flow to the resolver."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None
    coordinator.stats = {}
    coordinator._device_location_data = {}
    coordinator._device_update_history = {}
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator._last_device_list = []
    coordinator.data = []
    coordinator.config_entry = SimpleNamespace(entry_id="entry-id", options={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator._extract_our_identifier = lambda device: device.custom_fields.get(
        "canonical_id"
    )

    encrypted_payload = {
        "pair_date": 1_700_000_000,
        "encrypted_identity_key": b"\x01\x02\x03\x04",
    }

    coordinator.update_device_cache("device-1", encrypted_payload)

    registry = SimpleNamespace()
    registry_device = SimpleNamespace(
        id="registry-id",
        disabled_by=None,
        custom_fields={"canonical_id": "device-1"},
    )

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: [registry_device],
    )

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.identity_key is None
    assert identity.encrypted_identity_key == encrypted_payload["encrypted_identity_key"]
    assert identity.pair_date == encrypted_payload["pair_date"]


def test_coordinator_yields_identities_without_pair_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permit identities without pairing timestamps to reach the resolver."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None
    coordinator.stats = {}
    coordinator._device_location_data = {}
    coordinator._device_update_history = {}
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator._last_device_list = []
    coordinator.data = []
    coordinator.config_entry = SimpleNamespace(entry_id="entry-id", options={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator._extract_our_identifier = lambda device: device.custom_fields.get(
        "canonical_id"
    )

    identity_only_payload = {
        "identity_key": b"\x09\x0A\x0B\x0C",
    }

    coordinator.update_device_cache("device-1", identity_only_payload)

    registry = SimpleNamespace()
    registry_device = SimpleNamespace(
        id="registry-id",
        disabled_by=None,
        custom_fields={"canonical_id": "device-1"},
    )

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: [registry_device],
    )

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.identity_key == identity_only_payload["identity_key"]
    assert identity.pair_date is None


def test_active_device_identities_assigns_correct_timestamps_per_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each device should retain its own timestamps from the last device list."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None
    coordinator.stats = {}
    coordinator._device_location_data = {}
    coordinator._device_update_history = {}
    coordinator._enabled_poll_device_ids = {"device-1", "device-2"}
    coordinator._last_device_list = [
        {"id": "device-1", "identity_key": b"\x01", "pair_date": 100},
        {"id": "device-2", "identity_key": b"\x02", "pair_date": 200},
    ]
    coordinator.data = []
    coordinator.config_entry = SimpleNamespace(entry_id="entry-id", options={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator._extract_our_identifier = lambda device: device.custom_fields.get(
        "canonical_id"
    )

    registry = SimpleNamespace()
    registry_devices = [
        SimpleNamespace(
            id="registry-1", disabled_by=None, custom_fields={"canonical_id": "device-1"}
        ),
        SimpleNamespace(
            id="registry-2", disabled_by=None, custom_fields={"canonical_id": "device-2"}
        ),
    ]

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: registry_devices,
    )

    identities = coordinator.get_active_device_identities()

    pair_dates = {identity.canonical_id: identity.pair_date for identity in identities}
    assert pair_dates == {"device-1": 100, "device-2": 200}


def test_active_device_identities_handles_missing_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator should not crash when registry devices lack custom fields."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None
    coordinator.stats = {}
    coordinator._device_location_data = {}
    coordinator._device_update_history = {}
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator._last_device_list = [
        {"id": "device-1", "identity_key": b"\x01", "pair_date": 100}
    ]
    coordinator.data = []
    coordinator.config_entry = SimpleNamespace(entry_id="entry-id", options={})
    coordinator.hass = SimpleNamespace(data={})
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "canonical_id", None
    )

    registry = SimpleNamespace()
    registry_device = SimpleNamespace(
        id="registry-1",
        disabled_by=None,
        custom_fields=None,
        canonical_id="device-1",
    )

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        coordinator_module.dr,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: [registry_device],
    )

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.canonical_id == "device-1"
    assert identity.pair_date == 100
