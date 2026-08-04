"""Subentry operations for GoogleFindMyCoordinator.

This module contains subentry-related methods extracted from main.py
during Phase 3 of the refactoring.
"""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers import device_registry as dr

from ..const import (
    DOMAIN,
    LITERAL_CORE_KEY_OWNER,
    NON_DEVICE_SUBENTRY_TYPES,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SERVICE_SUBENTRY_TRANSLATION_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    TRACKER_SUBENTRY_TRANSLATION_KEY,
)
from ._mixin_typing import _MixinBase
from .helpers.subentry import (
    detect_missing_core_subentry_keys as _detect_missing_core_keys_impl,
)
from .helpers.subentry import (
    extract_subentry_group_key as _extract_group_key_impl,
)
from .helpers.subentry import (
    filter_provisional_identifier as _filter_provisional_impl,
)
from .helpers.subentry import (
    group_devices_by_subentry as _group_devices_impl,
)
from .helpers.subentry import (
    sanitize_subentry_identifier as _sanitize_subentry_id_impl,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .. import ConfigEntrySubentryDefinition, ConfigEntrySubEntryManager

_LOGGER = logging.getLogger(__name__)


# --- Subentry feature sets ---------------------------------------------------
_DEFAULT_SUBENTRY_FEATURES: tuple[str, ...] = (
    "binary_sensor",
    "button",
    "device_tracker",
    "sensor",
)

_SERVICE_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(SERVICE_FEATURE_PLATFORMS))
)
_TRACKER_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(TRACKER_FEATURE_PLATFORMS))
)


# --- SubentryMetadata dataclass ----------------------------------------------
@dataclass(slots=True, frozen=True)
class SubentryMetadata:
    """Lightweight view of a config-entry subentry relevant to platforms."""

    key: str
    config_subentry_id: str | None
    features: tuple[str, ...]
    title: str | None
    poll_intervals: Mapping[str, int]
    filters: Mapping[str, Any]
    feature_flags: Mapping[str, Any]
    visible_device_ids: tuple[str, ...]
    enabled_device_ids: tuple[str, ...]

    def stable_identifier(self) -> str:
        """Return the identifier to use when namespacing entities."""

        return self.config_subentry_id or self.key

    @property
    def subentry_id(self) -> str | None:
        """Backwards-compatible alias for the config subentry identifier."""

        return self.config_subentry_id


# --- Helper functions --------------------------------------------------------
def _sanitize_subentry_identifier(candidate: Any) -> str | None:
    """Return a normalized subentry identifier or ``None`` when fabricated."""
    return _sanitize_subentry_id_impl(candidate)


# --- SubentryOperations mixin ------------------------------------------------


class SubentryOperations(_MixinBase):
    """Subentry operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage config entry subentries,
    including creation, updates, and synchronization of subentry indices
    with tracked devices.
    """

    # Attribute declarations for mypy (actual values set in GoogleFindMyCoordinator.__init__)
    _subentry_manager: ConfigEntrySubEntryManager | None
    _pending_subentry_repair: asyncio.Task[None] | None
    _present_last_seen: dict[str, float]
    _present_device_ids: set[str]

    def attach_subentry_manager(
        self,
        manager: ConfigEntrySubEntryManager,
        *,
        is_reload: bool = False,
    ) -> None:
        """Attach the config entry subentry manager to the coordinator."""

        self._subentry_manager = manager
        self._skip_repair_during_reload_refresh = bool(is_reload)
        self._reload_repair_skip_pending_release = False
        if manager is None:
            return

        try:
            self._refresh_subentry_index(
                skip_manager_update=True, skip_repair=is_reload
            )
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "Initial subentry refresh failed during setup: %s",
                err,
            )
            return

        service_meta = self._subentry_metadata.get(SERVICE_SUBENTRY_KEY)
        if service_meta is not None and service_meta.config_subentry_id:
            try:
                self._ensure_service_device_exists()
            except Exception as err:  # noqa: BLE001 - defensive guard
                _LOGGER.debug(
                    "Service device ensure skipped during setup: %s",
                    err,
                )

    def _default_subentry_key(self) -> str:
        """Return the default subentry key used when no explicit mapping exists."""

        return self._default_subentry_key_value or "core_tracking"

    async def async_wait_subentry_visibility_updates(
        self,
    ) -> None:
        """Await pending visibility updates scheduled by the subentry manager."""

        manager = self._subentry_manager
        wait_visible = getattr(manager, "async_wait_visible_device_updates", None)
        if not callable(wait_visible):
            return

        try:
            await wait_visible()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "[%s] Visibility wait helper skipped due to: %s",
                self._entry_id() or "unknown",
                err,
            )

    def _build_core_subentry_definitions(
        self,
    ) -> list[ConfigEntrySubentryDefinition]:
        """Return definitions for the core tracker/service subentries."""

        entry = self.config_entry or getattr(self, "entry", None)
        entry_id = getattr(entry, "entry_id", None) if entry is not None else None
        if entry is None or not isinstance(entry_id, str) or not entry_id:
            _LOGGER.debug(
                "Skipping core subentry repair: config entry unavailable (entry=%s)",
                entry,
            )
            return []

        try:
            from .. import ConfigEntrySubentryDefinition  # local import to avoid cycles
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "Skipping core subentry repair: definition factory import failed (%s)",
                err,
            )
            return []

        runtime_data = getattr(entry, "runtime_data", None)
        fcm_receiver = getattr(runtime_data, "fcm_receiver", None)
        google_home_filter = getattr(runtime_data, "google_home_filter", None)

        fcm_push_enabled = fcm_receiver is not None
        has_google_home_filter = google_home_filter is not None
        entry_title = getattr(entry, "title", None) or "Google Find My"

        tracker_features = list(_TRACKER_SUBENTRY_FEATURES or TRACKER_FEATURE_PLATFORMS)
        service_features = list(_SERVICE_SUBENTRY_FEATURES or SERVICE_FEATURE_PLATFORMS)

        tracker_definition = ConfigEntrySubentryDefinition(
            key=TRACKER_SUBENTRY_KEY,
            title="Google Find My devices",
            data={
                "features": tracker_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_TRACKER,
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
            translation_key=TRACKER_SUBENTRY_TRANSLATION_KEY,
        )
        service_definition = ConfigEntrySubentryDefinition(
            key=SERVICE_SUBENTRY_KEY,
            title="Google Find Hub Service",
            data={
                "features": service_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_SERVICE,
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
            translation_key=SERVICE_SUBENTRY_TRANSLATION_KEY,
        )

        return [tracker_definition, service_definition]

    def _schedule_core_subentry_repair(self, missing_keys: set[str]) -> None:
        """Schedule a repair task to recreate missing core subentries."""

        if not missing_keys:
            return

        manager = self._subentry_manager
        hass = getattr(self, "hass", None)
        if manager is None or hass is None:
            return

        pending = self._pending_subentry_repair
        if pending is not None and not pending.done():
            _LOGGER.debug(
                "Core subentry repair already running; deferring additional request (%s)",
                sorted(missing_keys),
            )
            return

        entry_id = self._entry_id() or "unknown"

        async def _repair() -> None:
            try:
                if not self._config_entry_exists(entry_id):
                    _LOGGER.debug(
                        "Skipping core subentry repair for %s: entry removed", entry_id
                    )
                    return

                definitions = self._build_core_subentry_definitions()
                if not definitions:
                    _LOGGER.debug(
                        "Core subentry repair skipped for %s: definitions unavailable",
                        entry_id,
                    )
                    return

                _LOGGER.debug(
                    "Repairing missing subentries %s for entry %s",
                    sorted(missing_keys),
                    entry_id,
                )
                await manager.async_sync(definitions)
            except asyncio.CancelledError:  # pragma: no cover - task cancelled
                raise
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.warning(
                    "Core subentry repair failed for entry %s: %s",
                    entry_id,
                    err,
                )
                return
            finally:
                self._pending_subentry_repair = None

            if not self._config_entry_exists(entry_id):
                _LOGGER.debug(
                    "Skipping core subentry post-processing for %s: entry removed",
                    entry_id,
                )
                return

            self._ensure_service_device_exists()
            self._refresh_subentry_index()
            _LOGGER.debug("Core subentry repair completed for entry %s", entry_id)

        task_name = f"{DOMAIN}.repair_core_subentries"
        create_task = getattr(hass, "async_create_task", None)
        if callable(create_task):
            task = create_task(_repair(), name=task_name)
        else:  # pragma: no cover - fallback for legacy stubs
            task = asyncio.create_task(_repair(), name=task_name)
        self._pending_subentry_repair = task

    def _cancel_pending_subentry_repair(self) -> None:
        """Cancel any pending core subentry repair task."""

        pending = self._pending_subentry_repair
        if pending is None:
            return

        if not pending.done():
            pending.cancel()

        self._pending_subentry_repair = None

    def _refresh_subentry_index(
        self,
        visible_devices: Sequence[Mapping[str, Any]] | None = None,
        *,
        skip_manager_update: bool = False,
        skip_repair: bool = False,
    ) -> None:
        """Refresh internal subentry metadata caches."""

        if not hasattr(self, "_present_last_seen"):
            self._present_last_seen = {}
        if not hasattr(self, "_present_device_ids"):
            self._present_device_ids = set()

        reload_skip_active = bool(
            getattr(self, "_skip_repair_during_reload_refresh", False)
        )
        reload_skip_consumed = False
        if reload_skip_active and not skip_repair:
            skip_repair = True
            reload_skip_consumed = True
            self._reload_repair_skip_pending_release = True

        entry = self.config_entry

        entry_id = getattr(entry, "entry_id", None)
        entry_service_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "service_subentry_id", None))
            if entry is not None
            else None
        )
        entry_tracker_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "tracker_subentry_id", None))
            if entry is not None
            else None
        )

        service_provisional_seen = False
        tracker_provisional_seen = False

        # The trailing ``subentry_type`` is carried through unfolded: the fold
        # below rewrites ``group_key`` but the type is what ranks two subentries
        # that end up on the same core key, so it must survive the rewrite.
        # The trailing ``bool`` is the re-homing flag. A folded subentry's ids
        # have to stop counting as an *assignment* so the unassigned-device
        # merge can reclaim them, but they must keep working as that group's
        # *allow-list*. Dropping the ids from ``data`` did both at once, and the
        # second half was the damage, demonstrably so for the tracker fold
        # below: ``allow_filter`` reads a missing list as "no restriction", not
        # as "no assignment", so a parked tracker that won ``core_tracking``
        # was handed the entire device index -- including devices a different
        # group owns -- and ``manager.update_visible_device_ids`` persisted
        # that. On the service fold the same removal was inert, because that
        # branch overwrites the ids with ``()`` and the manager loop skips the
        # key; both are written the same way regardless, since that inertness
        # is an accident of order rather than a guarantee. The two effects are
        # carried separately from here on.
        raw_entries: list[
            tuple[str, str | None, dict[str, Any], str | None, str | None, bool]
        ] = []
        core_group_keys_present: set[str] = set()
        if entry and getattr(entry, "subentries", None):
            for subentry in entry.subentries.values():
                data = dict(getattr(subentry, "data", {}) or {})
                subentry_id_raw = getattr(subentry, "subentry_id", None)
                group_key = _extract_group_key_impl(data, subentry_id_raw)
                ids_are_rehomable = False
                if group_key in (SERVICE_SUBENTRY_KEY, TRACKER_SUBENTRY_KEY):
                    core_group_keys_present.add(group_key)
                # The repair detection above deliberately reads the *stored*
                # key: which core groups exist on disk is what it answers.
                # That exception now has to carry **two** shapes, not one, and
                # the second needs its own proof of being inert. A folded
                # ``hub`` reports the service key as present while the index
                # describes that group with a synthesised placeholder; the
                # devices are still reclaimed, so the report is inert rather
                # than lossy. A ``tracker`` parked on the service key reports
                # *service* as present while the fold below indexes it under
                # ``core_tracking``. It is inert *for the device assignment*,
                # by the same mechanism, made explicit rather than assumed by
                # analogy: the fold exempts its stored ids from this view's
                # assignment bookkeeping (they stay its allow-list), so the
                # unassigned-device merge collects the devices
                # into the tracker group, which is where the type axis puts
                # them anyway. "Inert" stops there, and the two further
                # consequences are named rather than folded into that word.
                # The report can cause a repair that believes a service group
                # exists when only a parked tracker stores that key; moving the
                # read to the canonical key would change *which* subentry the
                # repair creates, and that is a decision of its own rather than
                # a side effect of this one. And where the parked tracker is
                # the *only* holder of the service key, the fold leaves the
                # service group described by the synthesised placeholder
                # instead of by that subentry -- measured, not derived. That
                # placeholder is rejected by ``registry.py``'s
                # ``_is_real_service_subentry``, because
                # ``extract_service_subentry_ids`` still collects the parked
                # subentry through its stored ``group_key``, so the set is
                # non-empty and does not contain the placeholder. The service
                # device then keeps its base identifier but loses the
                # ``<entry>:<subentry>:service`` one it carried before. Both
                # directions are unattractive (the old one bound the service
                # device to a *tracker* subentry), so this is a change of
                # shape, not a regression, and it is pinned by
                # ``::test_ap4_a_parked_tracker_alone_leaves_the_service_group_
                # synthesised``.
                # Everything below indexes runtime state, and there the
                # ``subentry_type`` is authoritative. The manager is *not*
                # symmetric here, stated because assuming it were is what hides
                # the third writer: ``_refresh_from_entry`` (`__init__.py`)
                # canonicalises ``service`` and ``tracker`` by type but leaves
                # ``hub`` on its stored key, so a legacy hub can still be
                # ``managed_subentries["core_tracking"]``. Anyone reading a
                # subentry out of that mapping by key has to apply the type
                # check themselves; the visibility write-back in
                # ``async_setup_entry`` does exactly that. The set is shared
                # with the options flow rather than restated, so the side that
                # refuses such a subentry as an assignment target and the side
                # that indexes it cannot drift apart. ``hub`` belongs in it for
                # the same reason ``service`` does: `HubSubentryFlowHandler`
                # sets ``_group_key = SERVICE_SUBENTRY_KEY`` and the service
                # feature platforms, so a hub *is* the service group under a
                # second entry point. Either one still storing a legacy or
                # tracker key would otherwise occupy a device-bearing key here,
                # overwrite the twin that legitimately owns it, and have
                # visible ids written back to it. Tracker subentries keep their
                # stored key wherever that key names a group of its own,
                # because several tracker groups with distinct keys are a
                # supported shape; the one exception is a ``tracker`` parked on
                # the *service* key, which the second branch below folds onto
                # ``TRACKER_SUBENTRY_KEY``. The manager folds every tracker
                # unconditionally, so the two sides stay asymmetric for a
                # legacy per-account key and agree for the parked shape.
                if (
                    getattr(subentry, "subentry_type", None)
                    in NON_DEVICE_SUBENTRY_TYPES
                    and group_key != SERVICE_SUBENTRY_KEY
                ):
                    group_key = SERVICE_SUBENTRY_KEY
                    # Whatever ids such a subentry stores did not get there by a
                    # deliberate move onto the service group.
                    # `_accepts_device_assignment` refuses one as an assignment
                    # target on either axis, so the options flow cannot produce
                    # such a move. What *can* reach storage is the write-back a
                    # mis-keyed twin attracts. One of the two routes to it has
                    # since been closed: the sync in
                    # `_async_sync_feature_subentries` now resolves through
                    # `_canonical_core_key_of` on both of its branches, so it no
                    # longer hands a hub storing ``core_tracking`` the tracker
                    # payload. The second has since been closed as well:
                    # `_BaseSubentryFlow._resolve_existing` still prefers the
                    # flow's own ``subentry``, which identifies the subentry the
                    # user opened, but its fallback scan now asks
                    # `_may_answer_for` in addition to the stored ``group_key``.
                    # Both *scan* routes are shut, so no new residue is
                    # produced. The qualifier is not a hedge: the flow
                    # resolver's hand-over branch above the scan stays
                    # deliberately unguarded, and that it produces no residue
                    # today rests on it being unreachable
                    # (`async_get_supported_subentry_types` returns `{}`), not
                    # on it being closed. The ids already in storage predate
                    # either closure, which is what this exemption is for and
                    # why closing the routes does not make it redundant. Those
                    # ids are the residue, and that verdict rests on this
                    # mechanism rather
                    # than on any claim about which targets older releases
                    # offered. Folding alone would strand them: the service
                    # branch below forces the visible ids to empty while
                    # ``stored_assigned_ids`` would still count them as
                    # assigned, so the unassigned-device merge would not pull
                    # them into the tracker group and they would sit in no
                    # group at all. Excusing them from that bookkeeping (the
                    # stored subentry is untouched) lets that merge reclaim
                    # them.
                    #
                    # The exemption stays bound to the fold, deliberately. One
                    # that already stores the canonical key is a different
                    # case: a device sitting there may be a move the user made
                    # while the service group was still an offered target, and
                    # ``test_a_device_moved_to_the_service_subentry_is_left_alone``
                    # pins that it must not be reclaimed. Only the *mis-keyed*
                    # ids are residue.
                    #
                    # It exempts the ids from the bookkeeping and leaves them in
                    # ``data``. Removing them there would also clear this
                    # group's allow-list, and an absent list means "show
                    # everything" downstream. That is inert on *this* branch
                    # only because the service branch overwrites the ids with
                    # ``()`` a few hundred lines below -- an accident of order,
                    # not a guarantee -- so the branch is written the same way
                    # as its tracker mirror, where the same removal was
                    # measurably harmful.
                    ids_are_rehomable = True
                elif (
                    getattr(subentry, "subentry_type", None) == SUBENTRY_TYPE_TRACKER
                    and group_key == SERVICE_SUBENTRY_KEY
                ):
                    # The mirror image of the fold above, and the shape the
                    # config flow deliberately leaves in place: a ``tracker``
                    # parked on the service key survives
                    # ``_async_cleanup_stale_subentries`` (pinned by
                    # ``test_a_tracker_parked_on_the_service_key_is_not_swept_up``)
                    # because removal is irreversible and parking is not. It
                    # then arrives *here*, and until this fold the outcome was
                    # that its devices were described by no group at all: the
                    # service branch below forces the metadata ids to ``()``
                    # while ``stored_assigned_ids`` still counts them, so the
                    # unassigned-device merge does not reclaim them either.
                    #
                    # Its ids are exempted from the assignment bookkeeping just
                    # as in the fold above, but for a different reason, and the
                    # reason is spelled out below rather than here: there the
                    # ids are residue of a mis-keyed group, here they are a
                    # live group's ids that the rank may take away again.
                    #
                    # This is the type axis, not the key axis, which is what
                    # keeps ``test_a_device_moved_to_the_service_subentry_is_
                    # left_alone`` untouched: the subentry there is
                    # ``service``-typed and stores the canonical key, so it
                    # neither matches this branch nor changes meaning. A
                    # deliberate user move onto the service group is still left
                    # alone; what moves is a *tracker group* that happens to
                    # store the wrong key.
                    group_key = TRACKER_SUBENTRY_KEY
                    # The fold alone does not put the devices anywhere, which
                    # was measured rather than assumed: with a canonical
                    # tracker also present, the parked subentry loses the
                    # core-key rank below, so nothing describes its ids -- and
                    # ``stored_assigned_ids`` (filled above the rank) still
                    # counts them, so the unassigned-device merge does not
                    # reclaim them either. That is the very state this step
                    # exists to end, only moved one key to the left. Exempting
                    # the ids from that bookkeeping (the stored subentry is
                    # untouched) hands them to the merge, which puts them in
                    # the tracker group -- the group the type axis says they
                    # belong to.
                    #
                    # Only from the bookkeeping, though. Until this line they
                    # were removed from ``data`` outright, which also emptied
                    # this group's allow-list, and ``allow_filter`` reads an
                    # absent list as "no restriction". The winner case was the
                    # one that broke: a parked tracker that keeps the slot was
                    # handed the whole device index, devices another group owns
                    # included, and the manager write-back persisted the widened
                    # list, so those devices were exposed through two subentries
                    # at once. The claim that this matched the pre-fold shape
                    # ("alone, its ids were already joined by every unassigned
                    # device") held only where no other group owned anything;
                    # the merge adds *unassigned* devices, while an absent
                    # filter adds *every* device. Keeping the list makes the
                    # winner see its own ids plus whatever the merge hands it,
                    # and the loser is re-homed by the exemption alone.
                    #
                    # Keeping a *non-empty* list, that is. An earlier version of
                    # the sentence above stopped there and was wrong for the
                    # shape reached by moving the last device out of a group:
                    # a stored ``()`` normalises to nothing and collapses into
                    # the same absent-filter reading, so the promise held for
                    # every list except the empty one. The normalisation keeps
                    # an empty set for folded groups instead
                    # (``::test_ap4_a_parked_tracker_with_an_empty_allow_list_
                    # stays_empty``); the general collapse is untouched and
                    # stays ``U-26``.
                    #
                    # What the fold did *not* newly break is the observable
                    # width, and saying otherwise would misplace the blame:
                    # measured against `f7c9eb47`, the same shape already
                    # produced the whole index through the branch that
                    # synthesises a missing tracker subentry (``U-25``). The
                    # fold changes which subentry that width is attributed to,
                    # from the synthesised placeholder to this stored one.
                    ids_are_rehomable = True
                identifier = _sanitize_subentry_identifier(subentry_id_raw)

                # Use filter_provisional_identifier for service subentries
                if group_key == SERVICE_SUBENTRY_KEY:
                    identifier, was_filtered = _filter_provisional_impl(
                        identifier,
                        group_key,
                        entry_service_subentry_id,
                        SERVICE_SUBENTRY_KEY,
                        TRACKER_SUBENTRY_KEY,
                    )
                    if was_filtered:
                        service_provisional_seen = True
                # Use filter_provisional_identifier for tracker subentries
                elif group_key == TRACKER_SUBENTRY_KEY:
                    identifier, was_filtered = _filter_provisional_impl(
                        identifier,
                        group_key,
                        entry_tracker_subentry_id,
                        SERVICE_SUBENTRY_KEY,
                        TRACKER_SUBENTRY_KEY,
                    )
                    if was_filtered:
                        tracker_provisional_seen = True

                raw_entries.append(
                    (
                        group_key,
                        identifier,
                        data,
                        getattr(subentry, "title", None),
                        getattr(subentry, "subentry_type", None),
                        ids_are_rehomable,
                    )
                )

        if entry is not None:
            missing_core_keys = _detect_missing_core_keys_impl(
                core_group_keys_present,
                SERVICE_SUBENTRY_KEY,
                TRACKER_SUBENTRY_KEY,
            )
        else:
            missing_core_keys = set()

        if not raw_entries:
            raw_entries.append(
                (
                    "core_tracking",
                    None,
                    {
                        "features": _DEFAULT_SUBENTRY_FEATURES,
                        "feature_flags": {},
                    },
                    getattr(entry, "title", None),
                    None,
                    False,
                )
            )

        ignored = self._get_ignored_set()
        device_index: dict[str, dict[str, Any]] = {}
        now_mono = time.monotonic()

        device_registry: dr.DeviceRegistry | None = None
        registry_lookup: Callable[[str], dr.DeviceEntry | None] | None = None
        hass_obj = getattr(self, "hass", None)
        if hass_obj is not None:
            try:
                device_registry = dr.async_get(hass_obj)
            except Exception:  # defensive: registry helpers may not be patched in tests
                device_registry = None
            else:
                candidate_lookup = getattr(device_registry, "async_get", None)
                if callable(candidate_lookup):
                    registry_lookup = candidate_lookup

        canonical_to_registry_id: dict[str, str] = {}
        registry_to_canonical: dict[str, str] = {}
        # Ids that some subentry's *stored* allow-list claims, collected while the
        # lists are read below and used by the unassigned-device merge at the end.
        stored_assigned_ids: set[str] = set()
        if device_registry is not None:
            candidate_entries: list[Any] = []
            raw_devices = getattr(device_registry, "devices", None)
            if isinstance(raw_devices, Mapping):
                candidate_entries.extend(raw_devices.values())
            else:
                registry_entries = getattr(device_registry, "_entries", None)
                if isinstance(registry_entries, Mapping):
                    candidate_entries.extend(registry_entries.values())

            if not candidate_entries:
                entry_id = self._entry_id()
                fetch_entries = getattr(dr, "async_entries_for_config_entry", None)
                if callable(fetch_entries) and entry_id:
                    try:
                        candidate_entries.extend(
                            fetch_entries(device_registry, entry_id)
                        )
                    except Exception:  # defensive: stub mismatches / legacy HA versions
                        candidate_entries = []

            for device_entry in candidate_entries:
                try:
                    canonical = self._extract_our_identifier(device_entry)
                except Exception:  # defensive: tolerate stub deviations
                    canonical = None
                if not canonical:
                    continue
                device_id_attr = getattr(device_entry, "id", None)
                if isinstance(device_id_attr, str) and device_id_attr:
                    canonical_to_registry_id.setdefault(canonical, device_id_attr)
                    registry_to_canonical.setdefault(device_id_attr, canonical)

        def _register_device(candidate: Mapping[str, Any]) -> None:
            dev_id = candidate.get("id")
            if not isinstance(dev_id, str) or not dev_id:
                fallback_id = candidate.get("device_id")
                if isinstance(fallback_id, str) and fallback_id:
                    dev_id = fallback_id
                else:
                    return
            if dev_id in ignored:
                return
            name = (
                candidate.get("name")
                if isinstance(candidate.get("name"), str)
                else None
            )
            device_index.setdefault(dev_id, {"id": dev_id, "name": name})

        if visible_devices is not None:
            for dev in visible_devices:
                if isinstance(dev, Mapping):
                    _register_device(dev)
        else:
            for dev in self.data or []:
                if isinstance(dev, Mapping):
                    _register_device(dev)

        if device_index:
            self._present_device_ids = set(device_index)
            for dev_id in device_index:
                self._present_last_seen.setdefault(dev_id, now_mono)

        previous_visible: dict[str, tuple[str, ...]] = {
            key: meta.visible_device_ids
            for key, meta in self._subentry_metadata.items()
        }

        metadata: dict[str, SubentryMetadata] = {}
        feature_map: dict[str, str] = {}
        default_key: str | None = None
        manager_visible: dict[str, tuple[str, ...]] = {}

        def _current_poll_intervals() -> Mapping[str, int]:
            return MappingProxyType(
                {
                    "location": int(self.location_poll_interval),
                    "minimum": int(self.min_poll_interval),
                    "device": int(self.device_poll_delay),
                }
            )

        def _current_filters() -> Mapping[str, Any]:
            return MappingProxyType(
                {
                    "ignored_device_ids": tuple(sorted(ignored)),
                    "allow_history_fallback": bool(self.allow_history_fallback),
                }
            )

        # Rank of whichever subentry currently describes each *core* group, so a
        # later one can only take that slot by ranking strictly better. A key
        # absent from the mapping means its slot is still free. Keyed per group
        # rather than held in one variable because both core keys are ranked:
        # they compete independently, and a tracker candidate must not be
        # measured against the service incumbent.
        core_slot_rank: dict[str, tuple[int, int, bool, str]] = {}

        for (
            group_key,
            subentry_id,
            data,
            title,
            subentry_type,
            ids_are_rehomable,
        ) in raw_entries:
            raw_features = data.get("features")
            if isinstance(raw_features, (list, tuple, set)):
                normalized_features = tuple(
                    sorted(
                        {
                            str(feature)
                            for feature in raw_features
                            if isinstance(feature, str)
                        }
                    )
                )
            else:
                normalized_features = _DEFAULT_SUBENTRY_FEATURES

            if group_key == SERVICE_SUBENTRY_KEY:
                features = _SERVICE_SUBENTRY_FEATURES or normalized_features
            elif group_key == TRACKER_SUBENTRY_KEY:
                features = _TRACKER_SUBENTRY_FEATURES or normalized_features
            else:
                features = normalized_features or _DEFAULT_SUBENTRY_FEATURES

            raw_flags = data.get("feature_flags")
            feature_flags: dict[str, Any]
            if isinstance(raw_flags, Mapping):
                feature_flags = {str(key): raw_flags[key] for key in raw_flags}
            else:
                feature_flags = dict[str, Any]()

            raw_allowed = data.get("visible_device_ids")
            normalized_allowed: set[str] | None = None
            if isinstance(raw_allowed, (list, tuple, set)):
                collected: set[str] = set()
                for item in raw_allowed:
                    if not isinstance(item, str) or not item:
                        continue
                    cleaned = item.rsplit(":", 1)[-1] if ":" in item else item
                    if cleaned:
                        collected.add(cleaned)
                if collected:
                    normalized_allowed = set(collected)
                    if registry_lookup is not None:
                        resolved: set[str] = set()
                        for candidate in collected:
                            try:
                                device_entry = registry_lookup(candidate)
                            except Exception:  # defensive against stub mismatches
                                device_entry = None
                            if device_entry is None:
                                continue
                            canonical = self._extract_our_identifier(device_entry)
                            if canonical:
                                resolved.add(canonical)
                        if resolved:
                            normalized_allowed.update(resolved)
                elif ids_are_rehomable:
                    # A stored list that is *present but empty* says "this
                    # group owns nothing". Everywhere else in this loop that
                    # statement is lost: it collapses into ``None`` below, and
                    # ``allow_filter`` reads ``None`` as *no restriction*, so
                    # the group is handed the whole device index. That reading
                    # is older and wider than this pass and is carried as
                    # ``U-26`` (see ``agents/runtime_patterns/AGENTS.md``);
                    # changing it for every group needs its own migration
                    # reasoning, because a user whose list emptied through the
                    # repairs move would go from seeing everything to seeing
                    # nothing.
                    #
                    # For a *folded* group it is not a matter of taste. The
                    # branches above moved this subentry onto a key it does not
                    # store, and the comment there promises the winner "sees
                    # its own ids plus whatever the merge hands it". With an
                    # empty list and the collapse to ``None`` that promise is
                    # false: it sees every device, including what another group
                    # owns, and ``manager.update_visible_device_ids`` is handed
                    # that widened list. Keeping the empty set makes the
                    # promise true for the one shape this pass created, and no
                    # further -- the loser half is unaffected, because a group
                    # owning nothing has nothing to re-home.
                    #
                    # On the service fold this is inert: that branch forces the
                    # visible ids to ``()`` regardless. Written for both anyway,
                    # so the two folds keep the same shape.
                    normalized_allowed = set()
                else:
                    normalized_allowed = None

            if normalized_allowed and not ids_are_rehomable:
                # Every id a subentry claims as its own, in both spellings the
                # loop produced -- *not* every id in this view: a mis-keyed twin
                # and a parked tracker were folded onto a key that is not the
                # one they stored, and the fold exempts them here, which is
                # exactly how their devices reach the merge. They keep their
                # allow-list; only their claim to *own* those devices is
                # suspended -- for the parked tracker because the rank below
                # may hand the key to someone else, for the mis-keyed twin
                # because its ids are residue either way. The unassigned-device merge needs the *stored*
                # assignment, not the metadata one: the service key has its
                # visible ids forced to empty a few lines below, so a device the
                # user moved there would otherwise look unassigned and be pulled
                # back into the tracker -- and persisted there.
                stored_assigned_ids.update(normalized_allowed)

            allow_filter = normalized_allowed

            if device_index:
                base_ids = [
                    dev_id
                    for dev_id in device_index
                    if allow_filter is None or dev_id in allow_filter
                ]
            else:
                base_ids = [
                    dev_id
                    for dev_id in previous_visible.get(group_key, ())
                    if allow_filter is None or dev_id in allow_filter
                ]

            visibility_candidates: list[str] = list(base_ids)
            if normalized_allowed:
                visibility_candidates.extend(normalized_allowed)

            visible_ids = tuple(sorted(dict.fromkeys(visibility_candidates)))
            if group_key != SERVICE_SUBENTRY_KEY and registry_to_canonical:
                canonicalized_ids: list[str] = []
                for dev_id in visible_ids:
                    canonicalized_ids.append(dev_id)
                    canonical_id = registry_to_canonical.get(dev_id)
                    if canonical_id and canonical_id != dev_id:
                        canonicalized_ids.append(canonical_id)
                visible_ids = tuple(sorted(dict.fromkeys(canonicalized_ids)))

            if group_key == SERVICE_SUBENTRY_KEY:
                visible_ids = cast(tuple[str, ...], ())
                enabled_ids = cast(tuple[str, ...], ())
                manager_visible_ids = cast(tuple[str, ...], ())
            else:
                enabled_ids = tuple(
                    sorted(
                        dev_id
                        for dev_id in visible_ids
                        if dev_id in self._enabled_poll_device_ids
                    )
                )
                manager_visible_ids = tuple(
                    dict.fromkeys(
                        canonical_to_registry_id.get(dev_id, dev_id)
                        for dev_id in visible_ids
                    )
                )

            if group_key in (SERVICE_SUBENTRY_KEY, TRACKER_SUBENTRY_KEY):
                # Several subentries can answer for one core key, and without a
                # rank the surviving description depends on the order
                # ``entry.subentries`` happens to yield. Two shapes reach the
                # service key: the repair path creates a canonically keyed
                # service subentry while a mis-keyed twin from an early
                # migration is still on disk; and a ``hub`` stores this key *by
                # design* (``HubSubentryFlowHandler._group_key``), a shape the
                # config flow deliberately preserves instead of sweeping it.
                #
                # A ``tracker`` parked on the service key was a third until the
                # fold above gained its tracker branch; it now leaves for
                # ``TRACKER_SUBENTRY_KEY`` before this rank sees it. What is
                # pinned by a mutation is the alone shape
                # (``::test_ap4_a_parked_tracker_alone_leaves_the_service_group_
                # synthesised``); with a real ``service`` subentry also present
                # the departure has no observable effect on the slot, because
                # the parked twin would have lost the owner field anyway, so
                # ``::test_ap4_a_parked_tracker_leaves_the_service_pool`` covers
                # both iteration orders as a regression anchor rather than as a
                # proof. Stated that way because an earlier draft called it
                # "measured in both iteration orders", which the tree did not
                # check.
                # That is the same shape the config flow excludes from the
                # service pool through ``_may_answer_for``, so the two sides
                # agree on it now instead of diverging.
                #
                # The tracker key is ranked by the same block rather than left
                # to iteration order, and that is what this commit changes.
                # ``_resolve_existing`` in the flow was already parametric in
                # the key (its ``min(...)`` is), and
                # ``ConfigEntrySubEntryManager._candidate_score`` takes the
                # canonical key as an argument, so the service-only shape here
                # was the last asymmetry of the three sides. Two ``tracker``
                # subentries both storing ``TRACKER_SUBENTRY_KEY`` are the
                # reachable collision; several tracker groups under *distinct*
                # keys stay a supported shape and never meet here, because they
                # keep their own ``group_key``.
                #
                # The ordering criteria are the ones
                # ``config_flow._resolve_existing`` applies, and they have to
                # agree: a slot won there and lost here would rebind the group's
                # device to a subentry the platforms do not select. An exact
                # stored-key match beats a folded twin (there expressed as
                # ``pool = exact or folded`` rather than as a rank field, same
                # effect), the type that *literally* owns the key beats one that
                # merely folds onto it (the entity platforms match
                # ``subentry_type`` literally, via
                # ``known_ids_for_subentry_type``), and only among equals does
                # the lowest identifier decide, where the value is arbitrary and
                # stability is the whole point. Ranking only the first two would
                # leave exactly the order-dependence this replaces.
                #
                # Two differences are deliberate rather than overlooked, and
                # saying "field for field" here would paper over both. The flow
                # additionally prefers its seeded candidate, a notion this pass
                # has no equivalent of; and it sorts on the *raw*
                # ``subentry_id`` where this pass sorts on the sanitised,
                # provisional-filtered one, which is why the missing-id field
                # exists here and has no counterpart there. The second can only
                # diverge for an id shape no production site in
                # ``custom_components/`` creates.
                #
                # A third used to sit between them and is gone: the flow gates
                # candidates through ``_may_answer_for``, which excludes a
                # ``tracker`` storing the service key, while this pass let it
                # compete for that key and win on the exact field. The fold
                # above now removes it from this key too, so the sides no
                # longer disagree there.
                candidate_rank = (
                    0 if data.get("group_key") == group_key else 1,
                    (
                        0
                        if subentry_type == LITERAL_CORE_KEY_OWNER.get(group_key)
                        else 1
                    ),
                    # ``subentry_id`` here is the *sanitised and
                    # provisional-filtered* identifier, so it can be ``None``
                    # for a subentry that has one on disk. Ordering by
                    # ``subentry_id or ""`` alone would sort those *below* every
                    # real identifier and hand them the slot deterministically,
                    # passing over the subentry that carries the registry
                    # bindings. What the group is left with then differs by
                    # route, and only one of the two ends in a placeholder: a
                    # *blank* id lets the stable-id block at the end of this
                    # method substitute ``{entry_id}-{key}-subentry``, while a
                    # *provisional* one sets the key's ``*_provisional_seen``
                    # flag, which that same block honours by skipping the key,
                    # so the group keeps ``config_subentry_id=None``. For the
                    # service key ``registry.py::_is_real_service_subentry``
                    # then resolves it via ``entry.service_subentry_id``. Both
                    # are wrong for the same reason, so missing sorts last
                    # either way.
                    subentry_id is None,
                    subentry_id or "",
                )
                incumbent_rank = core_slot_rank.get(group_key)
                if incumbent_rank is not None and candidate_rank >= incumbent_rank:
                    continue
                # Displacing a weaker holder needs no cleanup, and the reason is
                # per-key rather than universal, so it is spelled out for both.
                # The loop writes four things below, and they divide in two.
                # ``metadata`` and ``manager_visible`` are keyed by *group*, so
                # a later winner overwrites the loser's entry instead of
                # needing it removed. ``feature_map`` (keyed by *feature*, via
                # ``setdefault``) and ``default_key`` (a scalar behind an
                # ``is None`` guard) are never overwritten at all -- stated
                # because an earlier draft folded them into the overwrite
                # argument, which does not hold for them. They are harmless for
                # a different reason: the *value* written in both cases is
                # ``group_key`` itself, and winner and loser carry the same
                # core key by construction, so a loser's entry never points at
                # the wrong group. What a loser can still widen is the
                # ``feature_map`` key *set*, where the per-key feature
                # constants are empty and each subentry brings its own
                # ``data["features"]`` (see the fallback above): the map is
                # then the union rather than the winner's list. That is a
                # coarser mapping, not a misdirected one.
                # For the service key the
                # statement is stronger still: ``manager_visible`` is never
                # filled for it at all (see the guard below). Saying that for
                # the tracker key would be false -- ``manager_visible`` *is*
                # filled there -- which is why the overwrite argument, not the
                # never-filled one, carries the generalisation.
                #
                # The one deliberate exception is ``stored_assigned_ids``, which
                # is filled above the rank and therefore also counts the ids of
                # a loser *that still holds them there* -- a folded twin or a
                # parked tracker was exempted further up and is not
                # counted. That is what keeps the unassigned-device merge from
                # reclaiming a device the user moved, and it is unchanged here.
                core_slot_rank[group_key] = candidate_rank

            metadata[group_key] = SubentryMetadata(
                key=group_key,
                config_subentry_id=subentry_id,
                features=features,
                title=title,
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType(dict(feature_flags)),
                visible_device_ids=visible_ids,
                enabled_device_ids=enabled_ids,
            )

            if group_key != SERVICE_SUBENTRY_KEY:
                manager_visible[group_key] = manager_visible_ids

            for feature in features:
                feature_map.setdefault(feature, group_key)

            if default_key is None:
                default_key = group_key

        if SERVICE_SUBENTRY_KEY not in metadata:
            service_features = _SERVICE_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            stable_service_id: str | None
            if isinstance(entry_id, str) and entry_id and not service_provisional_seen:
                stable_service_id = f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry"
            else:
                stable_service_id = None
            metadata[SERVICE_SUBENTRY_KEY] = SubentryMetadata(
                key=SERVICE_SUBENTRY_KEY,
                config_subentry_id=stable_service_id,
                features=service_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=(),
                enabled_device_ids=(),
            )
            for feature in service_features:
                feature_map.setdefault(feature, SERVICE_SUBENTRY_KEY)

        if TRACKER_SUBENTRY_KEY not in metadata:
            tracker_features = _TRACKER_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            previous_tracker_visible = previous_visible.get(TRACKER_SUBENTRY_KEY, ())
            stable_tracker_id: str | None
            if isinstance(entry_id, str) and entry_id and not tracker_provisional_seen:
                stable_tracker_id = f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry"
            else:
                stable_tracker_id = None

            if device_index:
                tracker_visible_ids = tuple(sorted(device_index.keys()))
            else:
                tracker_visible_ids = previous_tracker_visible
            metadata[TRACKER_SUBENTRY_KEY] = SubentryMetadata(
                key=TRACKER_SUBENTRY_KEY,
                config_subentry_id=stable_tracker_id,
                features=tracker_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=tracker_visible_ids,
                enabled_device_ids=tuple(
                    dev_id
                    for dev_id in tracker_visible_ids
                    if dev_id in self._enabled_poll_device_ids
                ),
            )
            manager_visible[TRACKER_SUBENTRY_KEY] = tuple(
                dict.fromkeys(
                    canonical_to_registry_id.get(dev_id, dev_id)
                    for dev_id in tracker_visible_ids
                )
            )
            for feature in tracker_features:
                feature_map.setdefault(feature, TRACKER_SUBENTRY_KEY)

        # A device the account gained after the last subentry sync appears in no
        # allow-list at all: the stored lists are a device-to-subentry assignment,
        # not a user's show/hide choice, and every writer of the tracker list only
        # ever feeds back what ``allow_filter`` already let through -- so the list
        # can shrink and stay put, but nothing adds to it once the initial sync
        # filled it. Two places already treat "assigned to nobody" as "belongs to
        # the tracker": ``group_devices_by_subentry`` routes such a device to the
        # default key, and the branch above that builds a missing tracker subentry
        # uses the full device index. The stored list has to agree, or the metadata
        # would keep calling a device invisible while its entity already exists --
        # which is exactly what the silent-add path produces.
        #
        # The question asked is "does any *stored* list claim this id", not "does
        # the metadata": a device the user moved to the service subentry is in that
        # stored list, while its metadata visible ids are forced to empty above.
        # Going by the metadata would quietly undo such a move and persist the
        # device under the tracker instead.
        if device_index and TRACKER_SUBENTRY_KEY in metadata:
            assigned_ids: set[str] = set(stored_assigned_ids)
            for assigned_meta in metadata.values():
                assigned_ids.update(assigned_meta.visible_device_ids)
            unassigned_ids = [
                dev_id for dev_id in device_index if dev_id not in assigned_ids
            ]
            if unassigned_ids:
                tracker_meta = metadata[TRACKER_SUBENTRY_KEY]
                merged_visible = tuple(
                    sorted(
                        dict.fromkeys(
                            (*tracker_meta.visible_device_ids, *unassigned_ids)
                        )
                    )
                )
                metadata[TRACKER_SUBENTRY_KEY] = replace(
                    tracker_meta,
                    visible_device_ids=merged_visible,
                    enabled_device_ids=tuple(
                        sorted(
                            dev_id
                            for dev_id in merged_visible
                            if dev_id in self._enabled_poll_device_ids
                        )
                    ),
                )
                manager_visible[TRACKER_SUBENTRY_KEY] = tuple(
                    dict.fromkeys(
                        canonical_to_registry_id.get(dev_id, dev_id)
                        for dev_id in merged_visible
                    )
                )

        if isinstance(entry_id, str) and entry_id:
            stable_ids = {
                SERVICE_SUBENTRY_KEY: f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry",
                TRACKER_SUBENTRY_KEY: f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry",
            }

            for key, default_id in stable_ids.items():
                if (key == SERVICE_SUBENTRY_KEY and service_provisional_seen) or (
                    key == TRACKER_SUBENTRY_KEY and tracker_provisional_seen
                ):
                    continue
                meta = metadata.get(key)
                if meta is None or meta.config_subentry_id is not None:
                    continue

                metadata[key] = replace(meta, config_subentry_id=default_id)

        self._subentry_metadata = metadata
        self._feature_to_subentry = feature_map
        if TRACKER_SUBENTRY_KEY in metadata:
            default_key = TRACKER_SUBENTRY_KEY
        elif default_key is None and metadata:
            default_key = next(iter(metadata))
        if default_key:
            self._default_subentry_key_value = default_key

        manager = self._subentry_manager
        if reload_skip_consumed:
            if visible_devices is not None or missing_core_keys:
                self._skip_repair_during_reload_refresh = False
                self._reload_repair_skip_pending_release = False
        elif (
            reload_skip_active
            and self._reload_repair_skip_pending_release
            and (visible_devices is not None or missing_core_keys)
        ):
            self._skip_repair_during_reload_refresh = False
            self._reload_repair_skip_pending_release = False

        if not skip_repair and missing_core_keys:
            self._schedule_core_subentry_repair(missing_core_keys)

        if manager and manager_visible and not skip_manager_update:
            for group_key, visible_ids in manager_visible.items():
                if group_key == SERVICE_SUBENTRY_KEY:
                    continue
                manager.update_visible_device_ids(group_key, visible_ids)
        # Ensure snapshot container has entries for all known keys
        for key in list(self._subentry_snapshots):
            if key not in metadata:
                self._subentry_snapshots.pop(key, None)
        for key in metadata:
            self._subentry_snapshots.setdefault(key, ())

    def _group_snapshot_by_subentry(
        self, snapshot: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Return snapshot entries grouped by subentry key."""
        # Build device-to-subentry mapping from metadata
        device_to_key: dict[str, str] = {}
        for key, meta in self._subentry_metadata.items():
            for dev_id in meta.visible_device_ids:
                device_to_key.setdefault(dev_id, key)

        return _group_devices_impl(
            snapshot,
            device_to_key,
            self._default_subentry_key(),
            set(self._subentry_metadata.keys()),
        )

    def _store_subentry_snapshots(self, snapshot: Sequence[Mapping[str, Any]]) -> None:
        """Persist grouped snapshots for subentry-aware consumers."""

        grouped = self._group_snapshot_by_subentry(snapshot)
        self._subentry_snapshots = {
            key: tuple(entries) for key, entries in grouped.items()
        }

    def _resolve_subentry_key_for_feature(self, feature: str) -> str:
        """Return the subentry key for a platform feature without warnings."""

        return self._feature_to_subentry.get(feature, self._default_subentry_key())

    def get_subentry_key_for_feature(self, feature: str) -> str:
        """Return the subentry key responsible for a platform feature."""

        warnings.warn(
            "get_subentry_key_for_feature() is deprecated; pass the subentry key "
            "explicitly when constructing entities.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._resolve_subentry_key_for_feature(feature)

    def get_subentry_metadata(
        self,
        *,
        key: str | None = None,
        feature: str | None = None,
    ) -> SubentryMetadata | None:
        """Return metadata for a given subentry key or feature."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            return None
        return self._subentry_metadata.get(lookup_key)

    def stable_subentry_identifier(
        self,
        *,
        key: str | None = None,
        feature: str | None = None,
    ) -> str:
        """Return the stable identifier string for a subentry."""

        meta = self.get_subentry_metadata(key=key, feature=feature)
        if meta is not None:
            return meta.stable_identifier()
        if key:
            return key
        if feature:
            return feature
        return self._default_subentry_key()

    def get_subentry_snapshot(
        self,
        key: str | None = None,
        *,
        feature: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a copy of the current snapshot for a subentry."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            lookup_key = self._default_subentry_key()
        entries = self._subentry_snapshots.get(lookup_key)
        if not entries:
            return []
        return [dict(row) for row in entries]

    def is_device_visible_in_subentry(self, subentry_key: str, device_id: str) -> bool:
        """Return True if a device is visible within the subentry scope.

        Handles both raw device IDs and namespaced identifiers (ENTRY_ID:DEVICE_ID)
        to ensure robust visibility checks in multi-account setups.
        """

        meta = self._subentry_metadata.get(subentry_key)
        if meta is None:
            return False

        # Fast path: Check for exact match (raw ID)
        if device_id in meta.visible_device_ids:
            return True

        # Robust path: Check for namespaced IDs (e.g., "01KBB...:DEVICE_ID")
        # The registry index may contain the fully qualified identifier.
        suffix = f":{device_id}"
        for visible_id in meta.visible_device_ids:
            if visible_id.endswith(suffix):
                return True

        return False

    def get_device_location_data_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> dict[str, Any] | None:
        """Return location data for a device if it belongs to the subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_location_data(device_id)

    def get_display_location_data_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> dict[str, Any] | None:
        """Return the published display row for a device within the subentry.

        Subentry-visibility-gated wrapper around ``get_display_location_data``
        (coordinator last-good display selection), the shared source for the
        Plus Code sensor and the device_tracker ``plus_code`` attribute.
        """

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_display_location_data(device_id)

    def get_device_last_seen_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> datetime | None:
        """Return last_seen for a device within the given subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_last_seen(device_id)
