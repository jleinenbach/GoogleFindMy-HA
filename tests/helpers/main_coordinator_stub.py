# tests/helpers/main_coordinator_stub.py
"""Test stub for :class:`GoogleFindMyCoordinator` standalone unit tests.

Phase 3 AP-F: exercise the coordinator's pure helpers, static methods and
attribute-driven branches (``_get_ignored_set``, ``is_ignored``,
``safe_update_metric``, ``mark_recent_reconfigure``, ``recent_reconfigure_at``,
``cache`` property, ``_apply_semantic_mapping``, ``_find_semantic_match``,
``_record_semantic_label``, ``get_observed_semantic_labels``,
``_should_preserve_precise_home_coordinates``, ``auth_error_active``) without
the full HA runtime. ``MainCoordinatorStub`` bypasses
``GoogleFindMyCoordinator.__init__`` (which would pull in
``aiohttp_get_clientsession``, ``DataUpdateCoordinator.__init__`` and
``_get_api_class``) and seeds the minimum attribute set the targeted methods
read or mutate.

Deep async branches (``async_setup``, ``async_shutdown``, ``async_update_data``
and the Nova-roundtrip locate paths) are intentionally out of scope here; they
land in Phase 4 alongside ``__init__.py`` and ``config_flow.py``.
"""

from __future__ import annotations

from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator


def make_hass_stub(*, loop: Any | None = None) -> MagicMock:
    """Return a Home Assistant stub with ``data``, ``loop`` and task helpers."""

    hass = MagicMock(spec_set=["data", "loop", "async_create_task", "bus"])
    hass.data = {}
    hass.loop = loop if loop is not None else MagicMock()
    hass.async_create_task = MagicMock(return_value=None)
    hass.bus = MagicMock()
    return hass


class MainCoordinatorStub(GoogleFindMyCoordinator):
    """Minimal :class:`GoogleFindMyCoordinator` subclass for isolated unit tests.

    Cross-mixin ``_MixinBase`` stubs (``_entry_id``, ``_is_on_hass_loop``,
    ``_run_on_hass_loop``, ``_dispatch_async_request_refresh`` and friends)
    are seeded as ``MagicMock`` so methods under test can call them
    transparently; tests rebind per-case via :func:`setattr`. No
    ``super().__init__`` chain runs because
    ``DataUpdateCoordinator.__init__`` would require a full HA runtime.
    """

    def __init__(
        self,
        hass: Any | None = None,
        config_entry: Any | None = None,
        *,
        cache: Any | None = None,
    ) -> None:
        self.hass = hass if hass is not None else make_hass_stub()
        self.config_entry = config_entry
        self.data: list[dict[str, Any]] = []

        # Cache (private + property-backed)
        self._cache = cache if cache is not None else MagicMock()
        self._cache.async_get_cached_value = AsyncMock(return_value=None)
        self._cache.async_set_cached_value = AsyncMock(return_value=None)

        # Reconfigure marker
        self._recent_reconfigure_at: float | None = None

        # Performance metrics / errors
        self.performance_metrics: dict[str, float] = {}
        self.recent_errors: deque[tuple[float, str, str]] = deque(maxlen=20)
        self._setup_started_at: float | None = None
        self._setup_completed_at: float | None = None

        # Auth state (mirror production attribute names from
        # ``GoogleFindMyCoordinator.__init__`` so the ``auth_error_active``
        # property and any helper that reads ``_auth_error_message`` work).
        self._auth_error_active: bool = False
        self._auth_error_message: str | None = None

        # Reauth-reason state (FIX 3), mirroring production ``__init__`` defaults
        # so ``record_reauth_reason`` and the diagnostics mirror work here.
        self._consecutive_transient_auth_failures: int = 0
        self._reauth_reason: Any = None
        self._reauth_reason_logged: set[tuple[str, str]] = set()

        # Force-refresh state
        self._force_device_list_refresh: bool = False
        self._force_device_list_reason: str | None = None

        # Semantic-label cache (lazy in production, pre-seeded here for clarity)
        self._semantic_label_cache: dict[str, Any] = {}

        # Ignored devices fallback attribute (used when entry.options is empty)
        self.ignored_devices: list[str] | None = None

        # Stats / device caches consumed by the public reporting helpers
        self._device_location_data: dict[str, dict[str, Any]] = {}
        self._device_names: dict[str, str] = {}
        self._device_names_by_user: dict[str, str] = {}
        self._present_device_ids: set[str] = set()

        # Cross-mixin methods that the coordinator helpers may invoke transparently.
        entry_id = getattr(config_entry, "entry_id", None) if config_entry else None
        self._entry_id = MagicMock(return_value=entry_id)
        self._is_on_hass_loop = MagicMock(return_value=True)
        self._run_on_hass_loop = MagicMock(return_value=None)
        self._dispatch_async_request_refresh = MagicMock(return_value=None)
        self._haversine_distance = MagicMock(return_value=0.0)
        self._ensure_service_device_exists = MagicMock(return_value=None)
        self._reindex_poll_targets_from_device_registry = MagicMock(return_value=None)
        self._cancel_pending_subentry_repair = MagicMock(return_value=None)


__all__ = ["MainCoordinatorStub", "make_hass_stub"]
