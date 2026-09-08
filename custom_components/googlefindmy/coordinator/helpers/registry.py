"""Registry utilities for the coordinator.

This module contains pure functions for device registry operations extracted from
coordinator.py for improved testability and maintainability (Phase 4).

Contents:
- extract_device_display_name(): Get human-friendly device name
- build_legacy_device_registry_kwargs(): Translate modern kwargs to legacy
- needs_legacy_kwarg_retry(): Check if legacy retry is needed
- parse_device_identifier(): Parse identifier tuple with multi-account support
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Re-export constants used by this module's functions
from ...const import (
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
)

__all__ = [
    "LEGACY_SERVICE_IDENTIFIER",
    "OWNERSHIP_ADD_KWARGS",
    "OWNERSHIP_REMOVE_KWARGS",
    "SERVICE_DEVICE_IDENTIFIER_PREFIX",
    "DeviceOwnership",
    "DeviceRegistryCapabilities",
    "DeviceRegistryOperation",
    "OwnershipIntent",
    "build_canonical_unique_id",
    "build_entity_unique_id_candidates",
    "build_legacy_device_registry_kwargs",
    "detect_device_registry_capabilities",
    "extract_canonical_device_id",
    "extract_device_display_name",
    "extract_service_subentry_ids",
    "extract_subentry_links",
    "has_hub_link",
    "has_subentry_link",
    "is_hub_device_check",
    "match_entity_by_device_id",
    "needs_legacy_kwarg_retry",
    "normalize_device_name",
    "parse_device_identifier",
    "plan_device_ownership",
    "resolve_device_by_identifiers",
    "resolve_tracker_subentry_candidate",
    "should_defer_service_subentry",
]


# ---------------------------------------------------------------------------
# Device Display Name
# ---------------------------------------------------------------------------


def extract_device_display_name(
    name_by_user: str | None,
    name: str | None,
    fallback: str | None,
) -> str:
    """Return the best human-friendly device name without sensitive data.

    Priority order:
    1. User-set name (name_by_user)
    2. Device name (name)
    3. Fallback

    Args:
        name_by_user: User-customized name from device registry.
        name: Default device name from device registry.
        fallback: Fallback name if others are unavailable.

    Returns:
        The best available name, stripped of leading/trailing whitespace.
        Returns empty string if all inputs are None/empty.
    """
    return (name_by_user or name or fallback or "").strip()


# ---------------------------------------------------------------------------
# Legacy Device Registry Kwargs
# ---------------------------------------------------------------------------


def build_legacy_device_registry_kwargs(
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate ownership keywords for cores that predate the current names.

    Mapping applied here:
    - add_config_entry_id -> config_entry_id
    - add_config_subentry_id -> config_subentry_id
    - remove_config_subentry_id -> (dropped, not supported in legacy)

    Version note corrected: the rename predates our declared minimum.
    Tag ``2025.9.1`` already ships ``add_config_subentry_id`` on
    ``async_update_device`` (``homeassistant/helpers/device_registry.py``, line
    1014 in that tag). This *naming* branch therefore serves registry doubles
    rather than supported cores, unlike the legacy *ownership* path that emits
    the ``add_*``/``remove_*`` quadruple, which is live on the declared minimum.
    Note that ``async_get_or_create`` keeps
    ``config_subentry_id`` in *every* release up to 2026.9: the keyword choice
    is a distinction between callers, not between core versions.

    Do not read this translator as a licence to write the old keywords. From
    Core 2026.8 ``add_config_entry_id`` attaches nothing and
    ``remove_config_entry_id`` on the owning entry deletes the device; see
    ``docs/AI_DEPRECATIONS_GUIDE.md``, section VI.

    Args:
        kwargs: Modern keyword arguments for device registry calls.

    Returns:
        A new dict with legacy keyword argument names.
        The original dict is not modified.
    """
    legacy_kwargs = dict(kwargs)

    if "add_config_entry_id" in legacy_kwargs:
        legacy_kwargs["config_entry_id"] = legacy_kwargs.pop("add_config_entry_id")

    if "add_config_subentry_id" in legacy_kwargs:
        legacy_kwargs["config_subentry_id"] = legacy_kwargs.pop(
            "add_config_subentry_id"
        )

    if "remove_config_subentry_id" in legacy_kwargs:
        legacy_kwargs.pop("remove_config_subentry_id")

    return legacy_kwargs


# ---------------------------------------------------------------------------
# Legacy Retry Detection
# ---------------------------------------------------------------------------


def needs_legacy_kwarg_retry(
    kwarg_name: str | None,
    err_str: str,
    kwargs: Mapping[str, Any],
) -> bool:
    """Determine if a TypeError requires legacy kwargs retry.

    When calling device registry APIs, modern Home Assistant versions accept
    new keyword names (add_config_entry_id, add_config_subentry_id). Older
    versions will raise TypeError for these unknown keywords.

    Args:
        kwarg_name: The config_subentry kwarg name supported by the API.
                    If "add_config_subentry_id", the API is modern and no retry needed.
        err_str: The string representation of the TypeError.
        kwargs: The keyword arguments that caused the error.

    Returns:
        True if the error indicates a legacy API that needs kwargs rewriting.
        False if the error is unrelated or the API is modern.
    """
    # Modern registries accept the renamed "add_config_subentry_id" keyword
    # and should surface the original TypeError to callers. Only older
    # versions that reject the new keyword should trigger a legacy rewrite.
    if kwarg_name == "add_config_subentry_id":
        return False

    # Check if each modern kwarg appears in both the error and our kwargs
    if "add_config_entry_id" in kwargs and "add_config_entry_id" in err_str:
        return True

    if "add_config_subentry_id" in kwargs and "add_config_subentry_id" in err_str:
        return True

    if "remove_config_subentry_id" in kwargs and "remove_config_subentry_id" in err_str:
        return True

    return False


# ---------------------------------------------------------------------------
# Device Registry Capability Profile and Ownership Planner
# ---------------------------------------------------------------------------
#
# The minimum supported core is 2025.9.1 (hacs.json, pyproject.toml). It knows
# neither ``new_config_entry_id`` nor ``new_config_subentry_id``, so the switch
# below is a runtime signature probe, not a version comparison. Do not replace
# it with a version check and do not delete the legacy branches: they are the
# only ones that run on the declared minimum. Raising the minimum to 2026.8.0 is
# not permitted before six months after that release; see AGENTS.md.
#
# See docs/AI_DEPRECATIONS_GUIDE.md, section VI, for why the old keywords
# changed meaning rather than name.

#: The ownership keywords that *add* a link on cores below 2026.8. From 2026.8
#: on, ``add_config_entry_id`` alone attaches nothing; it only arms a pending
#: move that a matching ``remove_config_entry_id`` then completes.
OWNERSHIP_ADD_KWARGS: tuple[str, ...] = (
    "add_config_entry_id",
    "add_config_subentry_id",
)

#: The ownership keywords that *drop* a link. From 2026.8 on,
#: ``remove_config_entry_id`` on the owning entry deletes the device and every
#: entity attached to it.
OWNERSHIP_REMOVE_KWARGS: tuple[str, ...] = (
    "remove_config_entry_id",
    "remove_config_subentry_id",
)


@dataclass(frozen=True, slots=True)
class DeviceRegistryCapabilities:
    """What the installed device registry callable accepts.

    Derived from the callable's signature, never from a version string: a fork,
    a backport or a patched core can carry any number, but the signature is what
    the call actually has to satisfy.
    """

    has_new_config_entry_id: bool
    has_new_config_subentry_id: bool
    has_add_config_subentry_id: bool
    has_legacy_config_subentry_id: bool
    accepts_var_keyword: bool

    @property
    def single_owner_model(self) -> bool:
        """True on Core 2026.8+, where a device has exactly one owner.

        Both new keywords are required, not just one: they arrived together in
        2026.8.0 and a callable carrying only one of them is a partial double,
        not a core. Verified against tags 2025.9.1, 2026.8.0 and 2026.9.0.
        """
        return self.has_new_config_entry_id and self.has_new_config_subentry_id

    @property
    def subentry_kwarg_for_update(self) -> str | None:
        """Keyword that expresses "the device shall sit in this subentry"."""
        if self.single_owner_model:
            return "new_config_subentry_id"
        if self.has_add_config_subentry_id:
            return "add_config_subentry_id"
        if self.has_legacy_config_subentry_id or self.accepts_var_keyword:
            return "config_subentry_id"
        return None

    @property
    def subentry_kwarg_for_shim(self) -> str | None:
        """Keyword a plain compatibility rename may use.

        Deliberately never ``new_config_subentry_id``: that keyword *moves* a
        device, and a rename shim must not become an ownership change. Ownership
        goes through :func:`plan_device_ownership` instead.

        This also answers the ``async_get_or_create`` question without a special
        case. Measured at tags 2025.9.1, 2026.8.0 and 2026.9.0: that call takes
        ``config_subentry_id`` in every one of them and never takes a ``new_*``
        keyword, so the preference order below already picks the right name for
        it.
        """
        if self.has_legacy_config_subentry_id:
            return "config_subentry_id"
        if self.has_add_config_subentry_id:
            return "add_config_subentry_id"
        if self.accepts_var_keyword:
            return "config_subentry_id"
        return None


def detect_device_registry_capabilities(
    call: Callable[..., Any],
) -> DeviceRegistryCapabilities:
    """Derive the capability profile from ``call``'s signature.

    An unreadable signature degrades to the all-false profile, which selects the
    legacy branches everywhere. That is the safe direction: guessing
    "single owner" would send ``new_config_subentry_id`` at a test double that
    swallows unknown keywords, and the test would pass while production broke.
    """
    parameters: Mapping[str, inspect.Parameter]
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        parameters = {}
    return DeviceRegistryCapabilities(
        has_new_config_entry_id="new_config_entry_id" in parameters,
        has_new_config_subentry_id="new_config_subentry_id" in parameters,
        has_add_config_subentry_id="add_config_subentry_id" in parameters,
        has_legacy_config_subentry_id="config_subentry_id" in parameters,
        accepts_var_keyword=any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
        ),
    )


class OwnershipIntent(Enum):
    """What the caller wants, independent of the core version."""

    MOVE = "move"
    """Pattern A: the device shall live in ``target_subentry_id``."""

    ENSURE = "ensure"
    """Pattern B: make sure it already does."""

    DETACH = "detach"
    """Pattern C: give up one specific ownership link."""


@dataclass(frozen=True, slots=True)
class DeviceOwnership:
    """Who owns a device right now, as read from the device entry.

    ``None`` instead of an instance means *unknown*, which is not the same as
    "owned by nobody". DETACH refuses to act on an unknown state, because on a
    single-owner core the removal of the owning entry deletes the device. That
    distinction is the reason this is a class rather than two ``str | None``
    parameters: with two parameters both cases collapse into ``None``.
    """

    entry_id: str | None
    subentry_id: str | None


@dataclass(frozen=True, slots=True)
class DeviceRegistryOperation:
    """One executable registry step."""

    method: str
    """Either ``async_update_device`` or ``async_remove_device``."""

    kwargs: Mapping[str, Any]


def plan_device_ownership(  # noqa: PLR0913 - one parameter per ownership axis
    intent: OwnershipIntent,
    *,
    caps: DeviceRegistryCapabilities,
    device_id: str,
    entry_id: str,
    target_subentry_id: str | None = None,
    detach_subentry_id: str | None = None,
    current: DeviceOwnership | None = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[DeviceRegistryOperation, ...]:
    """Translate an ownership intent into registry operations.

    ``current`` is the ownership read from the device entry, or ``None`` when the
    caller did not resolve the device. On cores below 2026.8 a device entry has
    no ``config_entry_id`` attribute, so ``current`` is ``None`` there. The
    legacy DETACH and ENSURE branches ignore it; the legacy MOVE reads
    ``current.subentry_id`` as a fallback, but only when ``detach_subentry_id``
    was omitted.

    ``detach_subentry_id`` names the link to give up: ``None`` means the hub link
    (the device sitting directly on the entry). It is *not* interchangeable with
    ``target_subentry_id``. Core only removes ownership when the removed subentry
    matches the one the device actually sits in; see
    ``homeassistant/helpers/device_registry.py``, ``async_update_device``, the
    branch guarded by ``remove_config_entry_id == old.config_entry_id`` and a
    matching ``remove_config_subentry_id``.

    Args:
        intent: What the caller wants to achieve.
        caps: Capability profile of the installed ``async_update_device``.
        device_id: The device to act on.
        entry_id: Our config entry.
        target_subentry_id: Where the device shall sit (MOVE, ENSURE).
        detach_subentry_id: Which link to give up (DETACH, legacy MOVE).
        current: Ownership read from the device entry, or ``None`` if unknown.
        extra: Additional keyword arguments for an ``async_update_device``
            operation. Ownership and subentry keywords do not belong in here;
            use the parameters above. A removal and an empty plan carry no
            keywords at all, so ``extra`` has no effect in those branches.

    Returns:
        Zero or more operations, in execution order. An empty tuple means
        "nothing to do", which is observably different from an update that
        changes nothing: it produces no deprecation report and no registry event.

    Raises:
        ValueError: On a single-owner core, when the intent cannot be carried
            out without knowing the current ownership: DETACH always, ENSURE
            when no ``target_subentry_id`` was given.
    """
    payload: dict[str, Any] = {"device_id": device_id, **dict(extra or {})}

    if intent is OwnershipIntent.DETACH:
        if caps.single_owner_model:
            if current is None:
                # Deleting on a guess is not an option: on this core the removal
                # of the owning entry removes the device and all its entities.
                raise ValueError(
                    "DETACH needs the current ownership; resolve the device first"
                )
            if (
                current.entry_id != entry_id
                or current.subentry_id != detach_subentry_id
            ):
                # Core would not touch the device either: the link the caller
                # wants to drop is not the link the device sits on. No-op, and
                # explicitly not a deletion.
                return ()
            return (
                DeviceRegistryOperation(
                    "async_remove_device", {"device_id": device_id}
                ),
            )
        payload["remove_config_entry_id"] = entry_id
        payload["remove_config_subentry_id"] = detach_subentry_id
        return (DeviceRegistryOperation("async_update_device", payload),)

    if intent is OwnershipIntent.ENSURE:
        if caps.single_owner_model:
            if (
                current is not None
                and current.entry_id == entry_id
                and (
                    target_subentry_id is None
                    or current.subentry_id == target_subentry_id
                )
            ):
                return ()
            if target_subentry_id is None and current is None:
                # Omitting ``new_config_subentry_id`` is not neutral: core sets
                # the subentry to None alongside the new entry (pinned against
                # real core as ``new_entry_moves_immediately`` in
                # tests/test_device_registry_single_owner_contract.py). Without
                # a target and without a known current owner this branch would
                # therefore move a device to the entry root on a guess.
                raise ValueError(
                    "ENSURE without a target subentry needs the current "
                    "ownership; resolve the device first"
                )
            # Setting the owning entry is idempotent in core (the update is only
            # recorded when the value differs), so it is set unconditionally
            # rather than depending on a ``current`` the caller may not have.
            payload["new_config_entry_id"] = entry_id
            if target_subentry_id is not None:
                payload["new_config_subentry_id"] = target_subentry_id
            return (DeviceRegistryOperation("async_update_device", payload),)
        payload["add_config_entry_id"] = entry_id
        if target_subentry_id is not None and (kwarg := caps.subentry_kwarg_for_update):
            payload[kwarg] = target_subentry_id
        return (DeviceRegistryOperation("async_update_device", payload),)

    # OwnershipIntent.MOVE
    if caps.single_owner_model:
        # Both keywords together: passing new_config_subentry_id alone makes core
        # resolve the subentry against the *old* owning entry and raise
        # HomeAssistantError when the device still belongs to another entry.
        payload["new_config_entry_id"] = entry_id
        payload["new_config_subentry_id"] = target_subentry_id
        return (DeviceRegistryOperation("async_update_device", payload),)

    surplus_subentry_id = (
        detach_subentry_id
        if detach_subentry_id is not None
        else (current.subentry_id if current is not None else None)
    )
    if surplus_subentry_id != target_subentry_id:
        # Removing the link we are about to add would cancel the move out. On a
        # pre-2026.8 core that empties the entry's subentry set, and if it was
        # the only link the device is deleted. Emit the add half alone instead.
        payload["remove_config_entry_id"] = entry_id
        payload["remove_config_subentry_id"] = surplus_subentry_id
    payload["add_config_entry_id"] = entry_id
    if kwarg := caps.subentry_kwarg_for_update:
        payload[kwarg] = target_subentry_id
    return (DeviceRegistryOperation("async_update_device", payload),)


# ---------------------------------------------------------------------------
# Device Lookup
# ---------------------------------------------------------------------------


def resolve_device_by_identifiers(
    dev_reg: Any, candidates: tuple[tuple[str, str], ...], *, entry_id: str
) -> Any | None:
    """Resolve a registry entry from ``candidates``, highest priority first.

    This is the single place in the integration that looks a device up by
    identifier. Call it instead of ``async_get_device``; do not build the entry
    scoping by hand at a call site.

    Priority is explicit: the caller orders ``candidates`` and the first hit
    wins. Before Core 2026.8 the call sites passed the whole set to a single
    ``async_get_device`` call, whose result depended on set iteration order and
    could resolve to a device owned by a *different* GoogleFindMy config entry
    that still carried the legacy unscoped identifier. Scoping the lookup to
    ``entry_id`` removes that cross-entry hit. That is a deliberate narrowing,
    not a regression: a device owned by another entry was never a valid answer.

    Core matrix:

    * ``2026.8.0`` and newer: ``async_get_device_by_identifier`` takes **one**
      identifier tuple plus the owning entry id and cannot be ambiguous
      (``homeassistant/helpers/device_registry.py`` at tag ``2026.9.0``, line
      1999).
    * up to ``2026.7``: neither that method nor ``async_get_devices`` exists, so
      the legacy branch below is the only one that runs on our declared minimum
      core ``2025.9.1``. Do not delete it as dead code.

    ``async_get_devices`` is deliberately not used as a middle step. It arrives
    in the very same core release (``2026.8.0``), so a registry that lacks
    ``async_get_device_by_identifier`` lacks it too and a registry that has the
    former never reaches the legacy branch. It also returns an unordered list,
    which would put the priority back into implicit code. It stays the right
    call wherever a *set* of devices is wanted, which is nowhere here.

    Core 2026.9 adds a main/child device distinction where this lookup searches
    main devices only (tag ``2026.9.0``, lines 1999-2021). This integration
    registers no child devices: it never calls ``async_get_or_create_child`` and
    never sets ``parent_device_id``. ``via_device`` is a different relation and
    does not make a device a child. Revisit this function if that changes.

    Args:
        dev_reg: The device registry, or any object with the same surface.
        candidates: ``(domain, identifier)`` tuples, highest priority first.
        entry_id: The config entry that must own the device.

    Returns:
        The matching registry entry, or ``None``.
    """
    if not candidates:
        return None

    by_identifier = getattr(dev_reg, "async_get_device_by_identifier", None)
    if callable(by_identifier):  # HA 2026.8+
        for identifier in candidates:
            # A ``TypeError`` from here propagates on purpose. Catching it and
            # retrying unscoped would silently undo the entry scoping this
            # branch exists for, and the repository already rules that modern
            # registries surface their ``TypeError`` instead of being rewritten
            # into a legacy call (``tests/AGENTS.md``, the modern registry
            # TypeError propagation reminder).
            device = by_identifier(identifier, entry_id)
            if device is not None:
                return device
        return None

    legacy = getattr(dev_reg, "async_get_device", None)  # HA <= 2026.7
    if callable(legacy):
        return legacy(identifiers=set(candidates))
    return None


# ---------------------------------------------------------------------------
# Device Identifier Parsing
# ---------------------------------------------------------------------------


def parse_device_identifier(
    identifier: Any,
    domain: str,
    entry_id: str | None,
    service_prefix: str,
    legacy_service_id: str,
) -> str | None:
    """Parse a device identifier tuple and extract the device ID.

    Multi-account compatibility:
    - Since 2025.5+ we use **entry-scoped device identifiers** in the Device Registry
      to guarantee global uniqueness across multiple accounts:
          (DOMAIN, f"{entry_id}:{device_id}")
    - For backward compatibility we also recognize legacy identifiers:
          (DOMAIN, device_id)

    Args:
        identifier: A (domain, identifier) tuple or list from device registry.
        domain: The integration domain to match (e.g., "googlefindmy").
        entry_id: The current config entry ID for namespaced matching.
        service_prefix: Prefix for service device identifiers to filter out.
        legacy_service_id: Legacy service device identifier to filter out.

    Returns:
        The canonical device_id if the identifier belongs to this entry.
        None if the identifier doesn't match, is malformed, or is a service device.
    """
    # Robust check: strict unpacking causes crashes with 3-tuple identifiers
    if not isinstance(identifier, (tuple, list)) or len(identifier) != 2:
        return None

    ident_domain, ident = identifier

    # Must be our domain with a non-empty string identifier
    if ident_domain != domain or not isinstance(ident, str) or not ident:
        return None

    # Handle namespaced format "<entry_id>:<device_id>"
    if ":" in ident:
        if entry_id and ident.startswith(entry_id + ":"):
            return ident.split(":", 1)[1]  # return canonical device_id
        # Identifier belongs to a different entry; ignore.
        return None

    # Skip service device identifiers
    if ident.startswith(service_prefix) or ident == legacy_service_id:
        return None

    # Legacy format -> accept as-is
    return ident


# ---------------------------------------------------------------------------
# Phase 8: Device Name Normalization
# ---------------------------------------------------------------------------


def normalize_device_name(name: Any) -> str | None:
    """Normalize device name to lowercase for comparison.

    Strips whitespace and converts to lowercase (casefold).

    Args:
        name: Device name (any type).

    Returns:
        Normalized lowercase string, or None if invalid/empty.
    """
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    if not stripped:
        return None
    return stripped.casefold()


# ---------------------------------------------------------------------------
# Phase 8: Subentry Links Extraction
# ---------------------------------------------------------------------------


def extract_subentry_links(device: Any, entry_id: str | None) -> set[str | None]:
    """Extract subentry links from a device object for a given entry_id.

    Checks config_entries_subentries mapping first, falls back to
    config_subentry_id attribute.

    Args:
        device: Device registry entry object.
        entry_id: The config entry ID to look up.

    Returns:
        Set of subentry link strings (may include None for hub links).
    """
    if device is None or not entry_id:
        return set()

    # Try config_entries_subentries mapping first
    mapping_obj = getattr(device, "config_entries_subentries", None)
    if isinstance(mapping_obj, Mapping):
        raw_links = mapping_obj.get(entry_id)
        if isinstance(raw_links, Collection) and not isinstance(
            raw_links, (str, bytes, Mapping)
        ):
            typed_links: set[str | None] = set()
            for item in raw_links:
                if item is None:
                    typed_links.add(None)
                elif isinstance(item, str):
                    typed_links.add(item)
            return typed_links
        if raw_links is None:
            return set()

    # Fallback to config_subentry_id attribute
    fallback = getattr(device, "config_subentry_id", None)
    if isinstance(fallback, str):
        return {fallback}

    # If device has config_entries but no subentry, return {None}
    config_entries = getattr(device, "config_entries", None)
    if config_entries is not None and fallback is None:
        return {None}

    return set()


# ---------------------------------------------------------------------------
# Phase 8: Subentry Link Checks
# ---------------------------------------------------------------------------


def has_subentry_link(links: set[str | None], target_id: str | None) -> bool:
    """Check if target subentry ID is in links set.

    Args:
        links: Set of subentry links (may include None).
        target_id: Target subentry ID to check.

    Returns:
        True if target_id is in links and target_id is not None.
    """
    if target_id is None:
        return False
    return target_id in links


def has_hub_link(links: set[str | None]) -> bool:
    """Check if device has a hub link (None in links set).

    Hub links are represented by None in the subentry links set,
    indicating the device is linked to the config entry without
    a specific subentry.

    Args:
        links: Set of subentry links.

    Returns:
        True if None is in links.
    """
    return None in links


# ---------------------------------------------------------------------------
# Phase 8: Hub Device Detection
# ---------------------------------------------------------------------------


def is_hub_device_check(
    device_id: str | None,
    hub_device_id: str | None,
    identifiers: Any,
    parent_identifier: tuple[str, str],
) -> bool:
    """Check if a device is the hub/service anchor device.

    A device is considered the hub if:
    1. Its ID matches the hub_device_id, OR
    2. Its identifiers contain the parent_identifier

    Args:
        device_id: The device's ID.
        hub_device_id: The known hub device ID.
        identifiers: The device's identifiers (set of tuples).
        parent_identifier: The parent/service device identifier tuple.

    Returns:
        True if the device is the hub/service device.
    """
    # Check by device ID match
    if (
        hub_device_id is not None
        and device_id is not None
        and device_id == hub_device_id
    ):
        return True

    # Check by identifier match
    if isinstance(identifiers, Collection) and not isinstance(
        identifiers, (str, bytes, Mapping)
    ):
        return parent_identifier in identifiers

    return False


# ---------------------------------------------------------------------------
# Phase 8: Tracker Subentry Resolution
# ---------------------------------------------------------------------------


def resolve_tracker_subentry_candidate(
    candidate: str | None,
    entry_tracker_id: str | None,
    tracker_subentry_ids: set[str],
) -> str | None:
    """Resolve a tracker subentry candidate to a valid subentry ID.

    Resolution rules:
    1. If candidate is None, return None
    2. If entry_tracker_id is set:
       - candidate must equal entry_tracker_id
       - if tracker_subentry_ids is non-empty, candidate must be in it
    3. If entry_tracker_id is None:
       - if tracker_subentry_ids is non-empty, candidate must be in it
       - if tracker_subentry_ids is empty, accept candidate as-is

    Args:
        candidate: The candidate subentry ID to validate.
        entry_tracker_id: The entry's tracker subentry ID (if set).
        tracker_subentry_ids: Set of valid tracker subentry IDs.

    Returns:
        The validated subentry ID, or None if validation fails.
    """
    if candidate is None:
        return None

    if entry_tracker_id is not None:
        # Must match entry_tracker_id
        if candidate != entry_tracker_id:
            return None
        # If tracker_subentry_ids exists, must be in it
        if tracker_subentry_ids and candidate not in tracker_subentry_ids:
            return None
        return candidate

    # No entry_tracker_id set
    if tracker_subentry_ids:
        # Must be in tracker_subentry_ids
        if candidate in tracker_subentry_ids:
            return candidate
        return None

    # No restrictions - accept as-is
    return candidate


# ---------------------------------------------------------------------------
# Phase 9: Service Device Helper Functions
# ---------------------------------------------------------------------------


def extract_service_subentry_ids(
    entry_subentries: Any,
    entry_service_subentry_id: str | None,
    subentry_type_service: str,
    service_subentry_key: str,
) -> set[str]:
    """Extract service subentry IDs from entry.subentries mapping.

    Identifies subentries that are service-related by checking:
    1. subentry_type == subentry_type_service
    2. data.group_key == service_subentry_key

    Provisional subentries (ending in '-provisional') are skipped unless
    they match entry_service_subentry_id.

    Args:
        entry_subentries: The entry.subentries mapping.
        entry_service_subentry_id: The entry's service subentry ID (if set).
        subentry_type_service: The subentry type constant for service.
        service_subentry_key: The group_key constant for service.

    Returns:
        Set of service subentry IDs.
    """
    if not isinstance(entry_subentries, Mapping):
        return set()

    result: set[str] = set()
    for subentry_id, subentry in entry_subentries.items():
        # Skip invalid subentry IDs
        if not isinstance(subentry_id, str) or not subentry_id:
            continue

        # Skip provisional unless it matches entry_service_subentry_id
        if (
            subentry_id.endswith("-provisional")
            and subentry_id != entry_service_subentry_id
        ):
            continue

        # Check subentry_type
        subentry_type = getattr(subentry, "subentry_type", None)

        # Check group_key in data
        group_key: Any = None
        data_obj = getattr(subentry, "data", None)
        if isinstance(data_obj, Mapping):
            group_key = data_obj.get("group_key")

        # Include if it's a service subentry
        if subentry_type == subentry_type_service or (
            isinstance(group_key, str) and group_key == service_subentry_key
        ):
            result.add(subentry_id)

    return result


def should_defer_service_subentry(
    service_subentry_id: str | None,
    current_subentries: Any,
    entry_id: str | None,
    service_subentry_key: str,
) -> bool:
    """Check if config_subentry_id should be deferred until registry catches up.

    Returns True if the subentry_id is not in current_subentries and is not
    the stable default pattern.

    Args:
        service_subentry_id: The service config_subentry_id to check.
        current_subentries: The current entry.subentries mapping.
        entry_id: The config entry ID.
        service_subentry_key: The group_key constant for service.

    Returns:
        True if the subentry_id should be deferred.
    """
    if service_subentry_id is None:
        return False

    if not isinstance(current_subentries, Mapping):
        return False

    # Check if subentry is in registry
    if service_subentry_id in current_subentries:
        return False

    # Check for stable default pattern
    if isinstance(entry_id, str) and entry_id:
        stable_default = f"{entry_id}-{service_subentry_key}-subentry"
        if service_subentry_id == stable_default:
            return False

    # Defer if not found
    return True


# ---------------------------------------------------------------------------
# Phase 12: Entity Lookup Helper Functions
# ---------------------------------------------------------------------------


def extract_canonical_device_id(
    identifiers: Collection[Any] | None,
    domain: str,
    entry_id: str | None = None,
    service_prefix: str = "",
    legacy_service_id: str = "",
) -> str | None:
    """Extract canonical device ID from device registry identifiers.

    Scans identifiers looking for a device ID matching the domain.
    Supports both simple and namespaced (entry_id:device_id) formats.

    Args:
        identifiers: Set of identifier tuples from device registry.
        domain: The integration domain to match.
        entry_id: Optional entry_id for namespaced identifier matching.
        service_prefix: Prefix for service device identifiers to skip.
        legacy_service_id: Legacy service device identifier to skip.

    Returns:
        The canonical device ID, or None if not found.
    """
    if identifiers is None:
        return None

    if not isinstance(identifiers, Collection) or isinstance(
        identifiers, (str, bytes, Mapping)
    ):
        return None

    namespaced_result: str | None = None
    simple_result: str | None = None

    for identifier in identifiers:
        # Must be 2-tuple
        if not isinstance(identifier, (tuple, list)) or len(identifier) != 2:
            continue

        ident_domain, ident_value = identifier

        # Must match domain
        if ident_domain != domain:
            continue

        # Must be non-empty string
        if not isinstance(ident_value, str) or not ident_value:
            continue

        # Skip service device identifiers
        if service_prefix and ident_value.startswith(service_prefix):
            continue
        if legacy_service_id and ident_value == legacy_service_id:
            continue

        # Handle namespaced format
        if ":" in ident_value:
            if entry_id and ident_value.startswith(entry_id + ":"):
                namespaced_result = ident_value.split(":", 1)[1]
            # If entry_id doesn't match, skip this identifier
            continue

        # Simple format
        simple_result = ident_value

    # Prefer namespaced result when entry_id is provided
    if namespaced_result is not None:
        return namespaced_result

    return simple_result


def build_entity_unique_id_candidates(
    device_id: str,
    entry_id: str | None,
    subentry_identifier: str | None,
    domain: str,
    subentry_key: str | None = None,
) -> list[str]:
    """Generate list of unique_id candidates for entity lookup.

    Produces various unique_id formats in priority order for finding
    an entity that may have been created with different ID schemes.

    Args:
        device_id: The device ID.
        entry_id: The config entry ID.
        subentry_identifier: The stable subentry identifier.
        domain: The integration domain.
        subentry_key: Optional subentry key if different from identifier.

    Returns:
        List of unique_id candidates in priority order (canonical first).
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if candidate and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    # Build canonical format first (highest priority)
    if entry_id and subentry_identifier and device_id:
        add(f"{entry_id}:{subentry_identifier}:{device_id}")

    # Subentry key variant (if different from identifier)
    if entry_id and subentry_key and subentry_key != subentry_identifier and device_id:
        add(f"{entry_id}:{subentry_key}:{device_id}")

    # Entry:device format
    if entry_id and device_id:
        add(f"{entry_id}:{device_id}")

    # Domain_entry_device format
    if entry_id and device_id:
        add(f"{domain}_{entry_id}_{device_id}")

    # Legacy domain_device format
    if device_id:
        add(f"{domain}_{device_id}")

    return candidates


def build_canonical_unique_id(
    entry_id: str | None,
    subentry_identifier: str | None,
    device_id: str | None,
) -> str | None:
    """Build canonical unique_id from components.

    Format: entry_id:subentry_identifier:device_id
    Skips empty components (except device_id which is required).

    Args:
        entry_id: The config entry ID.
        subentry_identifier: The stable subentry identifier.
        device_id: The device ID.

    Returns:
        Canonical unique_id string, or None if required parts are missing.
    """
    # Entry_id is required
    if not entry_id or not isinstance(entry_id, str):
        return None
    entry_id = entry_id.strip()
    if not entry_id:
        return None

    # Device_id is required
    if not device_id or not isinstance(device_id, str):
        return None
    device_id = device_id.strip()
    if not device_id:
        return None

    # Subentry_identifier is optional
    parts: list[str] = [entry_id]

    if subentry_identifier and isinstance(subentry_identifier, str):
        stripped = subentry_identifier.strip()
        if stripped:
            parts.append(stripped)

    parts.append(device_id)

    return ":".join(parts)


def match_entity_by_device_id(  # noqa: PLR0917
    unique_id: Any,
    config_entry_id: str | None,
    device_id: str,
    target_entry_id: str | None,
    domain: str,
    platform: str,
    entity_domain: str,
    entity_platform: str,
) -> bool:
    """Check if an entity registry entry matches device criteria.

    Used for fallback entity lookup when direct unique_id match fails.

    Args:
        unique_id: The entity's unique_id.
        config_entry_id: The entity's config_entry_id.
        device_id: The device ID to match (must be contained in unique_id).
        target_entry_id: The target entry ID to filter by (or None for no filter).
        domain: The expected domain (e.g., "device_tracker").
        platform: The expected platform (e.g., "googlefindmy").
        entity_domain: The entity's actual domain.
        entity_platform: The entity's actual platform.

    Returns:
        True if the entity matches all criteria.
    """
    # Validate required inputs
    if not device_id or not isinstance(device_id, str):
        return False

    # Check domain and platform match
    if entity_domain != domain or entity_platform != platform:
        return False

    # Check config_entry_id if target is specified
    if target_entry_id is not None and config_entry_id is not None:
        if config_entry_id != target_entry_id:
            return False

    # Check unique_id contains device_id
    if not isinstance(unique_id, str):
        return False

    return device_id in unique_id
