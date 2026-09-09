# tests/test_diagnostics_buffer_summary.py
"""Diagnostics buffer summaries surface sanitized coordinator payloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy._reauth_reason import (
    ReauthReason,
    ReauthReasonCode,
)
from custom_components.googlefindmy.const import (
    CONF_OAUTH_TOKEN,
    DOMAIN,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
)
from tests.helpers import drain_loop
from tests.helpers.single_owner_device_registry import SingleOwnerDeviceRegistry


class _StubDiagnosticsBuffer:
    """Diagnostics buffer stub exposing redaction-sensitive payloads."""

    _WARNING_DETAIL = "warning detail " * 20
    _ERROR_DETAIL = "error detail " * 20

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": {"warnings": 2, "errors": 1},
            "warnings": [
                {
                    "code": "warn-device",
                    "device_id": "device-123",
                    "device_name": "Living Room Phone",
                    "detail": self._WARNING_DETAIL,
                }
            ],
            "errors": [
                {
                    "code": "err-device",
                    "device_id": "device-987",
                    "device_name": "Bedroom Tablet",
                    "detail": self._ERROR_DETAIL,
                }
            ],
        }


class _StubCoordinator:
    """Coordinator stub exposing a diagnostics buffer."""

    def __init__(self) -> None:
        self._diag = _StubDiagnosticsBuffer()
        self._device_names: dict[str, str] = {}
        self._device_location_data: dict[str, object] = {}
        self._last_poll_mono: float | None = None
        self.stats: dict[str, int] = {}
        self.performance_metrics: dict[str, float] = {}
        self.recent_errors: list[object] = []
        self._enabled_poll_device_ids: set[str] = set()
        self._present_device_ids: set[str] = set()
        self._is_polling = True

    def attach_subentry_manager(
        self, manager: object, *, is_reload: bool = False
    ) -> None:
        self.subentry_manager = manager
        self._attached_is_reload = is_reload


class _StubEntry:
    """Minimal config entry stub referencing the coordinator."""

    def __init__(
        self,
        coordinator: _StubCoordinator,
        *,
        data: dict[str, object] | None = None,
        options: dict[str, object] | None = None,
    ) -> None:
        self.entry_id = "entry-id"
        self.version = 1
        self.domain = DOMAIN
        self.data = data or {}
        self.options = options or {}
        self.runtime_data = SimpleNamespace(coordinator=coordinator)


class _StubHass:
    """Home Assistant stub providing coordinator access."""

    def __init__(self, entry: _StubEntry, coordinator: _StubCoordinator) -> None:
        self.data = {
            DOMAIN: {
                "entries": {entry.entry_id: SimpleNamespace(coordinator=coordinator)}
            }
        }


def _redact(data, keys):  # pragma: no cover - deterministic helper in tests
    if isinstance(data, dict):
        return {
            key: _redact(value, keys) for key, value in data.items() if key not in keys
        }
    if isinstance(data, list):
        return [_redact(item, keys) for item in data]
    return data


def _run(coro):
    """Execute an async coroutine within an isolated event loop."""

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        drain_loop(loop)


def test_async_get_config_entry_diagnostics_includes_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics include sanitized buffer summaries with redacted IDs."""

    coordinator = _StubCoordinator()
    entry = _StubEntry(coordinator)
    hass = _StubHass(entry, coordinator)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )
    monkeypatch.setattr(diagnostics, "async_redact_data", _redact)

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    coordinator_block = payload.get("coordinator")
    assert coordinator_block is not None

    diag_payload = coordinator_block.get("diagnostics_buffer")
    assert diag_payload is not None

    summary = diag_payload.get("summary")
    assert summary == {"warnings": 2, "errors": 1}

    warnings_preview = diag_payload.get("warnings_preview")
    assert isinstance(warnings_preview, list)
    assert len(warnings_preview) == 1
    first_warning = warnings_preview[0]
    assert "device_id" not in first_warning
    assert "device_name" not in first_warning
    assert first_warning["detail"].endswith("…")
    assert len(first_warning["detail"]) <= 160

    errors_preview = diag_payload.get("errors_preview")
    assert isinstance(errors_preview, list)
    assert len(errors_preview) == 1
    first_error = errors_preview[0]
    assert "device_id" not in first_error
    assert "device_name" not in first_error
    assert first_error["detail"].endswith("…")
    assert len(first_error["detail"]) <= 160


def test_diagnostics_merge_entry_data_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics merge entry data defaults with option overrides."""

    coordinator = _StubCoordinator()
    entry = _StubEntry(
        coordinator,
        data={
            OPT_LOCATION_POLL_INTERVAL: 600,
            OPT_DEVICE_POLL_DELAY: 10,
            OPT_ENABLE_STATS_ENTITIES: True,
            OPT_GOOGLE_HOME_FILTER_ENABLED: False,
            OPT_GOOGLE_HOME_FILTER_KEYWORDS: "legacy",
            OPT_IGNORED_DEVICES: ["legacy-id"],
            CONF_OAUTH_TOKEN: "secret-token",
        },
        options={
            OPT_LOCATION_POLL_INTERVAL: "45",  # coercion
            OPT_GOOGLE_HOME_FILTER_ENABLED: True,
            OPT_GOOGLE_HOME_FILTER_KEYWORDS: "one, two, three",
            OPT_IGNORED_DEVICES: {"dev1": {}, "dev2": {}},
            OPT_ENABLE_STATS_ENTITIES: False,
        },
    )
    hass = _StubHass(entry, coordinator)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    effective_config = payload["effective_config"]
    assert effective_config[OPT_LOCATION_POLL_INTERVAL] == "45"
    assert effective_config[OPT_DEVICE_POLL_DELAY] == 10
    assert effective_config[OPT_GOOGLE_HOME_FILTER_KEYWORDS] == [
        diagnostics.REDACTED,
        diagnostics.REDACTED,
        diagnostics.REDACTED,
    ]
    assert effective_config[OPT_IGNORED_DEVICES] == [diagnostics.REDACTED] * 2
    assert effective_config[CONF_OAUTH_TOKEN] == diagnostics.REDACTED

    config_summary = payload["config"]
    assert config_summary["location_poll_interval"] == 45
    assert config_summary["device_poll_delay"] == 10
    assert config_summary["google_home_filter_enabled"] is True
    assert config_summary["enable_stats_entities"] is False
    assert config_summary["google_home_filter_keywords_count"] == 3
    assert config_summary["ignored_devices_count"] == 2
    assert "movement_threshold" not in config_summary


def test_diagnostics_includes_reauth_reason_when_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX 3: a recorded reauth reason surfaces in the coordinator block."""

    coordinator = _StubCoordinator()
    coordinator._reauth_reason = ReauthReason(
        code=ReauthReasonCode.HTTP_401_AFTER_REFRESH,
        origin="polling.py:_async_update_data",
        counters={"consecutive_transient_auth_failures": 3},
        recorded_at=1_700_000_000.0,
    )
    entry = _StubEntry(coordinator)
    hass = _StubHass(entry, coordinator)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )
    monkeypatch.setattr(diagnostics, "async_redact_data", _redact)

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    reauth = payload["coordinator"]["reauth_reason"]
    assert reauth["code"] == "http_401_after_refresh"
    assert reauth["origin"] == "polling.py:_async_update_data"
    assert reauth["counters"] == {"consecutive_transient_auth_failures": 3}
    # Mutation counter-check: skipping the wiring line drops this key entirely.


def test_device_count_asks_the_registry_for_this_entry_only(
    monkeypatch: pytest.MonkeyPatch,
    single_owner_device_registry: SingleOwnerDeviceRegistry,
) -> None:
    """The count covers this entry's devices, not the whole registry.

    What this pins and what it deliberately does not: it pins that a foreign
    entry's device is not counted, so removing the scoping altogether turns the
    answer from two into three.  It does **not** distinguish AP-17's helper call
    from the pre-AP-17 expression, and no assertion on the resulting number can:
    on a single-owner registry ``DeviceEntry.config_entries`` is derived from
    ``config_entry_id``, so both readings return the same set on every supported
    core.  Measured against this double: the old expression yields 2, the helper
    yields 2, an unscoped count yields 3.  The reading itself is pinned by
    ``test_device_count_goes_through_the_entry_scoped_helper`` below and, at the
    tree level, by the ratchet in
    ``tests/test_guard_device_registry_kwargs.py``.
    """

    coordinator = _StubCoordinator()
    entry = _StubEntry(coordinator)
    hass = _StubHass(entry, coordinator)

    registry = single_owner_device_registry
    registry.add_config_entry(entry.entry_id)
    registry.add_config_entry("foreign-entry")
    registry.add_device(
        identifiers={(DOMAIN, "ours-1")}, config_entry_id=entry.entry_id
    )
    registry.add_device(
        identifiers={(DOMAIN, "ours-2")}, config_entry_id=entry.entry_id
    )
    registry.add_device(
        identifiers={(DOMAIN, "theirs")}, config_entry_id="foreign-entry"
    )

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(diagnostics.dr, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert payload["registries"]["device"]["devices_count"] == 2


def test_device_count_goes_through_the_entry_scoped_helper(
    monkeypatch: pytest.MonkeyPatch,
    single_owner_device_registry: SingleOwnerDeviceRegistry,
) -> None:
    """AP-17: the reading itself, since no count can tell the two apart.

    This is the only assertion here that goes red if the entry-scoped helper is
    swapped back for a walk over the registry-wide mapping, which is what AP-17
    removed.  It watches *which* question is asked, not how the answer is
    spelled: the argument under test is the entry id, and a helper called for a
    foreign entry -- or not called at all -- fails.
    """

    coordinator = _StubCoordinator()
    entry = _StubEntry(coordinator)
    hass = _StubHass(entry, coordinator)

    registry = single_owner_device_registry
    registry.add_config_entry(entry.entry_id)
    registry.add_config_entry("foreign-entry")
    registry.add_device(
        identifiers={(DOMAIN, "ours-1")}, config_entry_id=entry.entry_id
    )
    # A foreign device, so the count below is not satisfied by an unscoped
    # tally.  Without it both readings answer 1 and the second assertion is
    # vacuous: a body that calls the helper, discards the result and counts the
    # whole registry would keep this test green.
    registry.add_device(
        identifiers={(DOMAIN, "theirs")}, config_entry_id="foreign-entry"
    )

    asked_for: list[str] = []
    real_helper = diagnostics.dr.async_entries_for_config_entry

    def _spy(reg, config_entry_id):
        asked_for.append(config_entry_id)
        return real_helper(reg, config_entry_id)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(diagnostics.dr, "async_get", lambda _hass: registry)
    monkeypatch.setattr(diagnostics.dr, "async_entries_for_config_entry", _spy)
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert asked_for == [entry.entry_id]
    assert payload["registries"]["device"]["devices_count"] == 1


def test_device_count_is_none_when_the_registry_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that cannot answer yields ``None``, never a wrong number.

    Diagnostics must not fail the whole report over one count, so the block
    carries a broad ``except``.  This pins that the fallback is ``None`` and not
    a silently wrong ``0``, which would read like "this entry owns no devices".
    """

    coordinator = _StubCoordinator()
    entry = _StubEntry(coordinator)
    hass = _StubHass(entry, coordinator)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(diagnostics.dr, "async_get", lambda _hass: SimpleNamespace())
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert payload["registries"]["device"]["devices_count"] is None
