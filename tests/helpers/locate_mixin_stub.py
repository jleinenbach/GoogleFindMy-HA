# tests/helpers/locate_mixin_stub.py
"""Test stub for :class:`LocateOperations` mixin standalone unit tests.

Phase 3 AP-C: exercise the locate mixin methods (``_normalize_coords``,
``can_play_sound``, ``_get_device_lock``, ``can_request_location``) and
gating branches of the async helpers (``async_locate_device``,
``async_play_sound``, ``async_stop_sound``) without the full coordinator.
``_MixinBase`` is runtime-empty, so a minimal subclass seeds every
attribute the mixin reads and pre-mocks cross-mixin methods to dodge the
``NotImplementedError`` traps that come from ``_MixinBase``.

The deep success-path branches of ``async_locate_device`` (Nova roundtrip,
Google Home filter, weighted fusion, cache commit) are intentionally out
of scope here; they land in Phase 4.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from custom_components.googlefindmy.coordinator.locate import LocateOperations


def make_hass_stub(*, loop: Any | None = None) -> MagicMock:
    """Return a Home Assistant stub with ``data``, ``loop`` and task helpers."""

    hass = MagicMock(spec_set=["data", "loop", "async_create_task"])
    hass.data = {}
    hass.loop = loop if loop is not None else MagicMock()
    hass.async_create_task = MagicMock(return_value=None)
    return hass


class LocateStub(LocateOperations):
    """Minimal :class:`LocateOperations` subclass for isolated mixin tests.

    Cross-mixin ``_MixinBase`` stubs are seeded as ``MagicMock`` so methods
    under test can call them transparently; tests rebind per-case via
    :func:`setattr`. No ``super().__init__`` chain runs because
    ``DataUpdateCoordinator.__init__`` would require a full HA runtime.
    """

    def __init__(
        self,
        hass: Any | None = None,
        config_entry: Any | None = None,
        *,
        api: Any | None = None,
        location_poll_interval: int = 120,
        min_poll_interval: int = 60,
    ) -> None:
        self.hass = hass if hass is not None else make_hass_stub()
        self.config_entry = config_entry
        self.data: list[dict[str, Any]] = []

        # Locate-state attributes the mixin reads or mutates.
        self._device_caps: dict[str, dict[str, Any]] = {}
        self._device_action_locks: dict[str, Any] = {}
        self._locate_inflight: set[str] = set()
        self._locate_cooldown_until: dict[str, float] = {}
        self._device_poll_cooldown_until: dict[str, float] = {}
        self._push_cooldown_until: float = 0.0
        self._device_location_data: dict[str, dict[str, Any]] = {}
        self._sound_request_uuids: dict[str, str] = {}
        self._sound_request_timestamps: dict[str, float] = {}
        self._last_poll_mono: float = 0.0
        self._present_last_seen: dict[str, float] = {}
        self._is_polling: bool = False

        # Cross-mixin scalars used by helpers.
        self.location_poll_interval = location_poll_interval
        self.min_poll_interval = min_poll_interval
        self.api = api if api is not None else MagicMock()
        self.api.async_get_device_location = AsyncMock(return_value={})
        self.api.async_play_sound = AsyncMock(return_value=(True, "uuid-stub"))
        self.api.async_stop_sound = AsyncMock(return_value=True)

        # Cross-mixin methods owned by other mixins or DataUpdateCoordinator:
        # mock them so default behavior is a no-op. Tests override per-case.
        entry_id = getattr(config_entry, "entry_id", None) if config_entry else None
        self._entry_id = MagicMock(return_value=entry_id)
        self.increment_stat = MagicMock(return_value=None)
        self.is_ignored = MagicMock(return_value=False)
        self.get_device_display_name = MagicMock(return_value=None)
        self._api_push_ready = MagicMock(return_value=True)
        self._ensure_device_name_cache = MagicMock(return_value={})
        self._set_auth_state = MagicMock(return_value=None)
        # FIX 3: cross-mixin choke point (real impl lives on the full
        # coordinator, not on ``LocateOperations``); mock like its siblings so
        # the direct locate-reauth sites can record without NotImplementedError.
        self.record_reauth_reason = MagicMock(return_value=None)
        self._record_semantic_label = MagicMock(return_value=None)
        self._apply_semantic_mapping = MagicMock(return_value=False)
        self._get_google_home_filter = MagicMock(return_value=None)
        self._should_preserve_precise_home_coordinates = MagicMock(return_value=False)
        self.update_device_cache = MagicMock(return_value=None)
        self._apply_weighted_location_fusion = MagicMock(return_value=True)
        self.note_error = MagicMock(return_value=None)
        self.push_updated = MagicMock(return_value=None)
        self._short_error_message = MagicMock(return_value="short-err")
        self._async_save_sound_uuids = AsyncMock(return_value=None)
        self.async_set_updated_data = MagicMock(return_value=None)
        self.async_request_refresh = AsyncMock(return_value=None)
        self._note_push_transport_problem = MagicMock(return_value=None)


__all__ = ["LocateStub", "make_hass_stub"]
