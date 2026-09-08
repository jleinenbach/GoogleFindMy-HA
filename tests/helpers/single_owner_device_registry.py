# tests/helpers/single_owner_device_registry.py
"""A faithful stand-in for the Core 2026.8+ single-owner device registry.

Home Assistant 2026.8 changed the device registry from "a device may belong to
several config entries" to "a device belongs to exactly one config entry and one
subentry".  The existing ``_StubDeviceRegistry`` in ``tests/conftest.py`` still
models the old, multi-owner world, which is fine for the tests that predate the
change but useless for asserting the new behaviour.

This double models the new behaviour, closely enough that
``tests/test_device_registry_single_owner_contract.py`` can run the same table of
operations against it *and* against real Core and compare the results.  That
differential test is the reason the double may be trusted; without it this file
would only encode an assumption.

Deliberately **not** modelled, because this integration does not use them and a
half-modelled feature is worse than an absent one: composite (pre-migration
split) devices, child devices, connections-based lookup, disabled/labels/areas
bookkeeping and the update event bus.

Of these, exactly one is actively rejected: a connections-based lookup raises
``NotImplementedError``.  The others are simply absent, because no caller in
this repository reaches for them.  The distinction is stated rather than
glossed over: "everything unsupported raises" would be a stronger promise than
this file keeps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

# Sentinel with the same role as homeassistant.helpers.typing.UNDEFINED.  Using
# an own sentinel keeps this module importable without Home Assistant, which the
# lightweight stub test runs rely on.
UNDEFINED: Any = object()


class SingleOwnerError(Exception):
    """Raised for the cases Core raises ``HomeAssistantError`` for.

    The differential test compares against Core's ``HomeAssistantError``; it maps
    this type onto that one so both sides can be asserted with one table.
    """


@dataclass(frozen=True)
class PendingMove:
    """A deferred move armed by ``add_config_entry_id``."""

    config_entry_id: str
    config_subentry_id: str | None
    origin_domain: str | None


@dataclass(frozen=True)
class SingleOwnerDeviceEntry:
    """Device entry with exactly one owning config entry and subentry."""

    id: str
    identifiers: frozenset[tuple[str, str]]
    config_entry_id: str
    config_subentry_id: str | None = None
    name: str | None = None
    composite_device_id: str | None = None
    pending_move: PendingMove | None = field(default=None, compare=False)
    # Plain descriptive fields a real ``DeviceEntry`` always carries. They play
    # no part in the ownership rules this double exists for, but production code
    # reads them on the way to those rules, and a double that raises
    # ``AttributeError`` there cannot be used to test them.
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None
    entry_type: Any = None
    configuration_url: str | None = None
    translation_key: str | None = None
    translation_placeholders: Mapping[str, str] | None = None
    name_by_user: str | None = None
    via_device_id: str | None = None

    @property
    def config_entries(self) -> set[str]:
        """Deprecated compatibility shim: always exactly one entry."""
        return {self.config_entry_id}

    @property
    def config_entries_subentries(self) -> dict[str, set[str | None]]:
        """Deprecated compatibility shim: always exactly one entry/subentry pair."""
        return {self.config_entry_id: {self.config_subentry_id}}


@dataclass
class _ConfigEntryStub:
    """Minimal config entry: the double only needs its id and subentry ids."""

    entry_id: str
    subentries: set[str] = field(default_factory=set)


class SingleOwnerDeviceRegistry:
    """Device registry implementing the Core 2026.8 ownership rules."""

    def __init__(
        self, *, current_integration_domain: str | None = "googlefindmy"
    ) -> None:
        """Create an empty registry.

        The default matches what real Core sees in the differential test: every
        step there runs through ``call_from_integration_frame``, so
        ``_current_integration_domain()`` returns ``"googlefindmy"``, not
        ``None``.  Defaulting to ``None`` would parametrise the two worlds
        differently and leave the "a foreign integration cancels a pending move"
        branch inactive on both sides for *different* reasons -- the kind of
        agreement that proves nothing.
        """
        self.devices: dict[str, SingleOwnerDeviceEntry] = {}
        self.entries: dict[str, _ConfigEntryStub] = {}
        #: What Core's ``_current_integration_domain()`` would return.
        self.current_integration_domain = current_integration_domain
        #: Every mutating call, so tests can assert on intent instead of kwargs.
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 0

    # -- setup helpers ---------------------------------------------------

    def add_config_entry(
        self, entry_id: str, subentries: set[str] | None = None
    ) -> None:
        """Register a config entry the double should know about."""
        self.entries[entry_id] = _ConfigEntryStub(entry_id, set(subentries or ()))

    def add_device(
        self,
        *,
        identifiers: set[tuple[str, str]],
        config_entry_id: str,
        config_subentry_id: str | None = None,
        name: str | None = None,
    ) -> SingleOwnerDeviceEntry:
        """Create a device owned by ``config_entry_id``."""
        self._next_id += 1
        device_id = f"device-{self._next_id}"
        entry = SingleOwnerDeviceEntry(
            id=device_id,
            identifiers=frozenset(identifiers),
            config_entry_id=config_entry_id,
            config_subentry_id=config_subentry_id,
            name=name,
        )
        self.devices[device_id] = entry
        return entry

    # -- read API --------------------------------------------------------

    def async_get(self, device_id: str) -> SingleOwnerDeviceEntry | None:
        """Return the device with ``device_id``, if any."""
        return self.devices.get(device_id)

    def async_get_device_by_identifier(
        self, identifier: tuple[str, str], config_entry_id: str
    ) -> SingleOwnerDeviceEntry | None:
        """Return the device owned by ``config_entry_id`` carrying ``identifier``."""
        for device in self.devices.values():
            if identifier in device.identifiers and device.config_entry_id == (
                config_entry_id
            ):
                return device
        return None

    def async_get_devices(
        self,
        identifiers: set[tuple[str, str]] | None = None,
        connections: set[tuple[str, str]] | None = None,
    ) -> list[SingleOwnerDeviceEntry]:
        """Return every device matching any of ``identifiers``."""
        if connections:
            raise NotImplementedError("connections lookup is not modelled")
        wanted = identifiers or set()
        return [
            device
            for device in self.devices.values()
            if wanted & set(device.identifiers)
        ]

    def async_entries_for_config_entry(
        self, config_entry_id: str
    ) -> list[SingleOwnerDeviceEntry]:
        """Return every device owned by ``config_entry_id``."""
        return [
            device
            for device in self.devices.values()
            if device.config_entry_id == config_entry_id
        ]

    # -- write API -------------------------------------------------------

    def async_remove_device(self, device_id: str) -> None:
        """Remove a device outright."""
        self.operations.append(("remove_device", {"device_id": device_id}))
        self.devices.pop(device_id, None)

    def async_update_device(
        self,
        device_id: str,
        *,
        add_config_entry_id: Any = UNDEFINED,
        add_config_subentry_id: Any = UNDEFINED,
        remove_config_entry_id: Any = UNDEFINED,
        remove_config_subentry_id: Any = UNDEFINED,
        new_config_entry_id: Any = UNDEFINED,
        new_config_subentry_id: Any = UNDEFINED,
        name: Any = UNDEFINED,
        manufacturer: Any = UNDEFINED,
        model: Any = UNDEFINED,
        sw_version: Any = UNDEFINED,
        entry_type: Any = UNDEFINED,
        configuration_url: Any = UNDEFINED,
        translation_key: Any = UNDEFINED,
        translation_placeholders: Any = UNDEFINED,
        new_identifiers: Any = UNDEFINED,
        via_device_id: Any = UNDEFINED,
    ) -> SingleOwnerDeviceEntry | None:
        """Apply the Core 2026.8 ownership rules.

        Mirrors ``homeassistant.helpers.device_registry.DeviceRegistry
        .async_update_device`` as of 2026.9.1, restricted to the ownership
        branches.  Returns ``None`` when the device was deleted, matching Core.

        The descriptive keywords after ``name`` are spelled out rather than
        swallowed by ``**kwargs`` on purpose, and the binding reason is not the
        capability profile: a var-keyword parameter does flip
        ``accepts_var_keyword``, but none of the values this double is read for
        would change (``single_owner_model`` stays true,
        ``subentry_kwarg_for_update`` stays ``new_config_subentry_id``). What it
        would break is ``_device_registry_allows_translation_update``, which
        looks for ``translation_key`` and ``translation_placeholders`` **in the
        parameters**: with a var-keyword it would answer ``False`` and the
        translation path of the code under test would stop running altogether.
        The keywords are recorded and applied, nothing more; the ownership rules
        above are the point.
        """
        self.operations.append(
            (
                "update_device",
                {
                    "device_id": device_id,
                    "add_config_entry_id": add_config_entry_id,
                    "add_config_subentry_id": add_config_subentry_id,
                    "remove_config_entry_id": remove_config_entry_id,
                    "remove_config_subentry_id": remove_config_subentry_id,
                    "new_config_entry_id": new_config_entry_id,
                    "new_config_subentry_id": new_config_subentry_id,
                },
            )
        )

        old = self.devices.get(device_id)
        if old is None:
            raise SingleOwnerError(f"Device {device_id} does not exist")

        self._validate(
            add_config_entry_id=add_config_entry_id,
            add_config_subentry_id=add_config_subentry_id,
            remove_config_entry_id=remove_config_entry_id,
            remove_config_subentry_id=remove_config_subentry_id,
            new_config_entry_id=new_config_entry_id,
            new_config_subentry_id=new_config_subentry_id,
        )

        target_entry: Any = UNDEFINED
        target_subentry: Any = UNDEFINED
        pending_move: Any = UNDEFINED

        if new_config_entry_id is not UNDEFINED:
            target_entry = new_config_entry_id
            target_subentry = (
                new_config_subentry_id
                if new_config_subentry_id is not UNDEFINED
                else None
            )
            # An immediate move supersedes a deferred one.
            pending_move = None
        elif new_config_subentry_id is not UNDEFINED:
            target_subentry = new_config_subentry_id
        else:
            if add_config_entry_id is not UNDEFINED:
                already_owner = add_config_entry_id == old.config_entry_id and (
                    add_config_subentry_id is UNDEFINED
                    or add_config_subentry_id == old.config_subentry_id
                )
                if not already_owner:
                    pending_move = PendingMove(
                        add_config_entry_id,
                        add_config_subentry_id
                        if add_config_subentry_id is not UNDEFINED
                        else None,
                        self.current_integration_domain,
                    )
            if remove_config_entry_id == old.config_entry_id and (
                remove_config_subentry_id is UNDEFINED
                or remove_config_subentry_id == old.config_subentry_id
            ):
                move_from_prior_call = pending_move is UNDEFINED
                move_target = (
                    pending_move if pending_move is not UNDEFINED else old.pending_move
                )
                # A deferred move only completes for the integration that armed it.
                if (
                    move_target is not None
                    and move_from_prior_call
                    and move_target.origin_domain is not None
                    and self.current_integration_domain is not None
                    and self.current_integration_domain != move_target.origin_domain
                ):
                    move_target = None
                if move_target is None:
                    self.async_remove_device(device_id)
                    return None
                target_entry = move_target.config_entry_id
                target_subentry = move_target.config_subentry_id
                pending_move = None

        if target_subentry not in (UNDEFINED, None):
            resolved_entry_id = (
                target_entry if target_entry is not UNDEFINED else old.config_entry_id
            )
            resolved_entry = self.entries.get(resolved_entry_id)
            if (
                resolved_entry is None
                or target_subentry not in resolved_entry.subentries
            ):
                raise SingleOwnerError(
                    f"Config entry {resolved_entry_id} has no subentry {target_subentry}"
                )

        changes: dict[str, Any] = {}
        if target_entry is not UNDEFINED and target_entry != old.config_entry_id:
            changes["config_entry_id"] = target_entry
        if target_subentry is not UNDEFINED and target_subentry != (
            old.config_subentry_id
        ):
            changes["config_subentry_id"] = target_subentry
        if pending_move is not UNDEFINED and pending_move != old.pending_move:
            changes["pending_move"] = pending_move
        if name is not UNDEFINED:
            changes["name"] = name
        for field_name, value in (
            ("manufacturer", manufacturer),
            ("model", model),
            ("sw_version", sw_version),
            ("entry_type", entry_type),
            ("configuration_url", configuration_url),
            ("translation_key", translation_key),
            ("translation_placeholders", translation_placeholders),
            ("via_device_id", via_device_id),
        ):
            if value is not UNDEFINED:
                changes[field_name] = value
        if new_identifiers is not UNDEFINED:
            changes["identifiers"] = frozenset(new_identifiers)

        updated = replace(old, **changes) if changes else old
        self.devices[device_id] = updated
        return updated

    # -- internals -------------------------------------------------------

    def _validate(
        self,
        *,
        add_config_entry_id: Any,
        add_config_subentry_id: Any,
        remove_config_entry_id: Any,
        remove_config_subentry_id: Any,
        new_config_entry_id: Any,
        new_config_subentry_id: Any,
    ) -> None:
        """Raise for the same argument combinations Core rejects."""
        if add_config_entry_id is not UNDEFINED:
            add_entry = self.entries.get(add_config_entry_id)
            if add_entry is None:
                raise SingleOwnerError(
                    f"Can't link device to unknown config entry {add_config_entry_id}"
                )
            if (
                add_config_subentry_id is not UNDEFINED
                and add_config_subentry_id
                and add_config_subentry_id not in add_entry.subentries
            ):
                raise SingleOwnerError(
                    f"Config entry {add_config_entry_id} has no"
                    f" subentry {add_config_subentry_id}"
                )
        elif add_config_subentry_id is not UNDEFINED:
            raise SingleOwnerError(
                "Can't add config subentry without specifying config entry"
            )

        if (
            remove_config_subentry_id is not UNDEFINED
            and remove_config_entry_id is UNDEFINED
        ):
            raise SingleOwnerError(
                "Can't remove config subentry without specifying config entry"
            )

        if (
            new_config_entry_id is not UNDEFINED
            and new_config_entry_id not in self.entries
        ):
            raise SingleOwnerError(
                f"Can't move device to unknown config entry {new_config_entry_id}"
            )

        if (
            new_config_entry_id is not UNDEFINED
            or new_config_subentry_id is not UNDEFINED
        ) and (
            add_config_entry_id is not UNDEFINED
            or remove_config_entry_id is not UNDEFINED
        ):
            raise SingleOwnerError(
                "Can't combine new_config_entry_id or new_config_subentry_id with "
                "add_config_entry_id or remove_config_entry_id"
            )
