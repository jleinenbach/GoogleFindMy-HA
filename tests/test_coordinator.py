# tests/test_coordinator.py

from __future__ import annotations

from typing import Any

import pytest

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
