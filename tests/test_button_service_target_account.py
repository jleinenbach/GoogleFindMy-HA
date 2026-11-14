# tests/test_button_service_target_account.py
"""The device buttons address the account of their own entity.

A device shared into two Google accounts carries the same canonical id in both
config entries. When a button hands only that canonical id to the device
service, ``_resolve_runtime_for_device_id`` finds no registry device under that
name and falls back to scanning the coordinators, and the first coordinator
that knows the id wins. The button of the second account then rings, stops or
locates through the first account (issue 224, measured live on 2026-11-14:
the second account's "Locate now" button sent its request through the first
account's session and push channel).

The registry device of a button entity belongs to exactly one config entry, so
the buttons pass ``registry_entry.device_id`` when it is known and the bare
canonical id only when the entity has no registry entry yet.

The second half pins the resolver as it is: a registry device id resolves to
its owning entry, a bare canonical id shared by two entries resolves to the
first runtime. That fallback is unchanged and only documented.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from custom_components.googlefindmy import services
from tests.helpers.service_doubles import FakeServiceCall, FakeServiceRegistry

_BUTTONS = (
    "GoogleFindMyPlaySoundButton",
    "GoogleFindMyStopSoundButton",
    "GoogleFindMyLocateButton",
)

_CANONICAL_ID = "canonical-shared"


def _pressable(base: type, registry_entry: Any) -> type:
    """Return a subclass with the availability gates stubbed out.

    Subclass rather than patch: mutating the production class would leak the
    stubbed gates into every later test in the session.
    """

    class _Pressable(base):  # type: ignore[misc, valid-type]
        device_id = _CANONICAL_ID
        available = True

        def device_label(self) -> str:
            return "Shared device"

        def _update_last_pressed(self) -> None:
            return None

    _Pressable.registry_entry = registry_entry  # type: ignore[attr-defined]
    return _Pressable


async def _pressed_device_id(cls_name: str, registry_entry: Any) -> Any:
    """Press the button and return the ``device_id`` handed to the service."""

    button_module = importlib.import_module("custom_components.googlefindmy.button")
    recorded: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _RecordingServices:
        async def async_call(self, *args: Any, **kwargs: Any) -> None:
            recorded.append((args, kwargs))

    cls = _pressable(getattr(button_module, cls_name), registry_entry)
    button = cls.__new__(cls)
    button.hass = SimpleNamespace(services=_RecordingServices())  # type: ignore[attr-defined]
    button._context = None  # type: ignore[attr-defined]

    await button.async_press()

    assert len(recorded) == 1, cls_name
    args, _kwargs = recorded[0]
    return args[2]["device_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls_name", _BUTTONS)
async def test_t_t1_button_passes_the_registry_device_of_its_entity(
    cls_name: str,
) -> None:
    """T-1: the service receives the registry device id, not the canonical id."""

    registry_entry = SimpleNamespace(device_id="registry-device-account-b")

    assert await _pressed_device_id(cls_name, registry_entry) == (
        "registry-device-account-b"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cls_name", _BUTTONS)
async def test_t_t2_button_without_registry_entry_passes_the_canonical_id(
    cls_name: str,
) -> None:
    """T-2: without a registry entry the button behaves as before."""

    assert await _pressed_device_id(cls_name, None) == _CANONICAL_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("cls_name", _BUTTONS)
async def test_t_t2_registry_entry_without_device_passes_the_canonical_id(
    cls_name: str,
) -> None:
    """T-2: a registry entry not yet linked to a device falls back as well."""

    registry_entry = SimpleNamespace(device_id=None)

    assert await _pressed_device_id(cls_name, registry_entry) == _CANONICAL_ID


# ---------------------------------------------------------------------------
# Resolver characterisation (unchanged behaviour, documented in services.py)
# ---------------------------------------------------------------------------


def _two_accounts_sharing_one_device() -> tuple[SimpleNamespace, Any, Any]:
    """Two active entries whose coordinators both know ``_CANONICAL_ID``."""

    coordinators = []
    entries = []
    for entry_id in ("entry-a", "entry-b"):
        coord = SimpleNamespace(
            async_locate_device=mock.AsyncMock(),
            get_device_display_name=mock.Mock(return_value="Shared device"),
        )
        coordinators.append(coord)
        entries.append(
            SimpleNamespace(
                entry_id=entry_id,
                runtime_data=SimpleNamespace(coordinator=coord),
                title="Account",
            )
        )

    hass = SimpleNamespace(
        services=FakeServiceRegistry(),
        config_entries=SimpleNamespace(
            async_entries=lambda _domain: list(entries),
            async_get_entry=lambda entry_id: next(
                (entry for entry in entries if entry.entry_id == entry_id), None
            ),
        ),
        data={},
    )
    return hass, coordinators[0], coordinators[1]


def _ctx() -> dict[str, Any]:
    return {
        "domain": services.DOMAIN,
        "resolve_canonical": mock.Mock(return_value=(_CANONICAL_ID, "Shared device")),
        "is_active_entry": mock.Mock(return_value=True),
        "primary_active_entry": mock.Mock(return_value=None),
        "opt": mock.Mock(side_effect=lambda entry, key, default: default),
        "default_map_view_token_expiration": False,
        "opt_map_view_token_expiration_key": "map_view_token_expiration",
        "redact_url_token": mock.Mock(return_value="<redacted-url>"),
        "soft_migrate_entry": mock.AsyncMock(),
        "migrate_unique_ids": mock.AsyncMock(),
        "relink_button_devices": mock.AsyncMock(),
        "relink_subentry_entities": mock.AsyncMock(),
        "coalesce_account_entries": mock.AsyncMock(),
        "extract_normalized_email": mock.Mock(return_value=None),
    }


def _locate_handler(hass: SimpleNamespace) -> Any:
    registry = FakeServiceRegistry()
    hass.services = registry
    coro = services.async_register_services(hass, _ctx())
    try:
        coro.send(None)
    except StopIteration:
        pass
    else:  # pragma: no cover - registration must not await
        coro.close()
        raise RuntimeError("async_register_services unexpectedly awaited")
    return registry.handlers[(services.DOMAIN, services.SERVICE_LOCATE_DEVICE)]


@pytest.fixture
def _registry_with_device_of_account_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """Device registry knowing one device, owned by ``entry-b``."""

    devices = {
        "registry-device-account-b": SimpleNamespace(
            id="registry-device-account-b",
            config_entry_id="entry-b",
            config_entries={"entry-b"},
        )
    }
    monkeypatch.setattr(
        services.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=devices.get),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registry_with_device_of_account_b")
async def test_t_t1_resolver_maps_a_registry_device_to_its_owning_entry() -> None:
    """The id the buttons now pass resolves to the second account."""

    hass, coord_a, coord_b = _two_accounts_sharing_one_device()
    handler = _locate_handler(hass)

    await handler(FakeServiceCall({"device_id": "registry-device-account-b"}))

    coord_b.async_locate_device.assert_awaited_once_with(_CANONICAL_ID)
    coord_a.async_locate_device.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_registry_with_device_of_account_b")
async def test_t_t1_resolver_maps_a_shared_canonical_id_to_the_first_runtime() -> None:
    """Characterisation: a bare shared canonical id resolves to the first entry.

    This is the path the buttons took before; it stays as it is for callers
    that only know the Google id.
    """

    hass, coord_a, coord_b = _two_accounts_sharing_one_device()
    handler = _locate_handler(hass)

    await handler(FakeServiceCall({"device_id": _CANONICAL_ID}))

    coord_a.async_locate_device.assert_awaited_once_with(_CANONICAL_ID)
    coord_b.async_locate_device.assert_not_awaited()
