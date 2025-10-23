# tests/test_services_forward_compat.py
"""Forward compatibility tests for entity service registration helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy.util_services import register_entity_service


class _PlatformRecorder:
    """Capture calls to entity service registration methods for assertions."""

    def __init__(self, *, raises_in_new: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises_in_new = raises_in_new
        self.new_attempted = False

    def async_register_platform_entity_service(self, *args: Any) -> None:
        self.new_attempted = True
        if self.raises_in_new:
            raise TypeError("simulated signature mismatch")
        self.calls.append(("new", args))

    def async_register_entity_service(self, *args: Any) -> None:
        self.calls.append(("legacy", args))


def _integration_root() -> Path:
    return Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"


def test_register_entity_service_prefers_platform_specific() -> None:
    """The wrapper should call the new API when available."""

    platform = _PlatformRecorder()
    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    assert platform.new_attempted is True
    assert platform.calls == [("new", ("svc", {"field": "value"}, "handler"))]


def test_register_entity_service_falls_back_on_type_error() -> None:
    """If the new API raises a TypeError, fall back to the legacy method."""

    platform = _PlatformRecorder(raises_in_new=True)
    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    assert platform.new_attempted is True
    assert platform.calls == [("legacy", ("svc", {"field": "value"}, "handler"))]


def test_register_entity_service_legacy_only() -> None:
    """When only the legacy method exists, the wrapper uses it directly."""

    class LegacyOnlyPlatform:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def async_register_entity_service(self, *args: Any) -> None:
            self.calls.append(args)

    platform = LegacyOnlyPlatform()
    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    assert platform.calls == [("svc", {"field": "value"}, "handler")]


def test_register_entity_service_legacy_duplicate_registration() -> None:
    """Duplicate legacy registrations should be ignored to support multiple entries."""

    class LegacyDuplicatePlatform:
        def __init__(self) -> None:
            self.successful_calls: list[tuple[Any, ...]] = []
            self.duplicate_attempts = 0

        def async_register_entity_service(self, *args: Any) -> None:
            if self.successful_calls:
                self.duplicate_attempts += 1
                raise ValueError("already registered")
            self.successful_calls.append(args)

    platform = LegacyDuplicatePlatform()

    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    register_entity_service(platform, "svc", {"field": "value"}, "handler")

    assert platform.successful_calls == [("svc", {"field": "value"}, "handler")]
    assert platform.duplicate_attempts == 1


@pytest.mark.parametrize(
    "needle",
    ["async_register_entity_service(", "async_register_platform_entity_service("],
)
def test_platforms_use_wrapper_exclusively(needle: str) -> None:
    """Integration code should not call HA APIs directly anymore."""

    offenders: list[Path] = []
    for path in _integration_root().rglob("*.py"):
        if path.name == "util_services.py":
            continue
        text = path.read_text(encoding="utf-8")
        if needle in text:
            offenders.append(path.relative_to(_integration_root()))
    assert offenders == []


def test_wrapper_used_somewhere_in_platforms() -> None:
    """The wrapper must be exercised by at least one integration module."""

    wrapper_callers: list[Path] = []
    for path in _integration_root().rglob("*.py"):
        if path.name == "util_services.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "register_entity_service(" in text:
            wrapper_callers.append(path.relative_to(_integration_root()))
    assert wrapper_callers, (
        "Expected at least one module to invoke register_entity_service"
    )
