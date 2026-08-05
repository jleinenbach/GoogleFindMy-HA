# tests/test_services_handler_contracts.py
"""Contract tests for the googlefindmy service handlers.

``async_register_services(hass, ctx)`` wires seven service handlers as closures
over an injected ``ctx`` (a 14-key context passed by ``__init__.py`` to avoid
import cycles). These tests build that full ctx with mocked callables, capture
the registered handlers, and pin:

* input validation (``device_id`` / ``request_uuid`` type + emptiness guards),
* the ``_resolve_runtime_for_device_id`` outcomes (no active entry, resolver
  ``HomeAssistantError`` -> not-found, coordinator dispatch, suppressed ops),
* token redaction in ``refresh_device_urls`` (the raw token-bearing URL is what
  is handed to ``ctx["redact_url_token"]`` for logging),
* the ``rebuild_registry`` payload-type guards (invalid entry_id / device_ids).

Fixture note: ``dr.async_get`` and ``get_url`` are patched at the module level so
handlers run without a live Home Assistant device registry / network helper.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from custom_components.googlefindmy import services
from custom_components.googlefindmy.const import StopSoundOutcome
from custom_components.googlefindmy.services import (
    HomeAssistantError,
    ServiceValidationError,
)
from tests.helpers.config_entries_stub import make_config_entry

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _FakeCall:
    """Minimal ``ServiceCall`` double exposing only ``.data``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def _make_hass(entries: list[Any] | None = None) -> SimpleNamespace:
    """Build a fake hass with a capturing service registry and entry list."""
    entry_list = list(entries or [])
    return SimpleNamespace(
        services=SimpleNamespace(async_register=lambda *a, **k: None),
        config_entries=SimpleNamespace(async_entries=lambda _domain: entry_list),
        data={},
    )


@pytest.fixture
def full_ctx() -> dict[str, Any]:
    """A complete 14-key ctx with every callable stubbed.

    Mirrors the keys documented on ``async_register_services``; every callable
    is a ``Mock``/``AsyncMock`` so a handler that reaches for any of them gets a
    benign stub rather than a ``KeyError``.
    """
    return {
        "domain": services.DOMAIN,
        "resolve_canonical": mock.Mock(return_value=("CANON", "Friendly Name")),
        "is_active_entry": mock.Mock(return_value=True),
        "primary_active_entry": mock.Mock(return_value=None),
        "opt": mock.Mock(return_value=False),
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


def _register(hass: SimpleNamespace, ctx: dict[str, Any]) -> dict[str, Any]:
    """Register services and return {service_name: handler}.

    ``async_register_services`` is declared ``async`` but its body performs only
    synchronous work (defining closures + calling ``async_register``). Driving
    the coroutine with a single ``send(None)`` runs it to completion without a
    loop, so this works both in a plain fixture and inside a running event loop
    (``asyncio.run`` would raise there).
    """
    handlers: dict[str, Any] = {}

    def _capture(domain: str, name: str, handler: Any) -> None:
        handlers[name] = handler

    hass.services.async_register = _capture  # type: ignore[attr-defined]
    coro = services.async_register_services(hass, ctx)
    try:
        coro.send(None)
    except StopIteration:
        pass
    else:  # pragma: no cover - registration must not await
        coro.close()
        raise RuntimeError("async_register_services unexpectedly awaited")
    return handlers


@pytest.fixture
def handlers(full_ctx: dict[str, Any]) -> dict[str, Any]:
    """Handlers registered against a fake hass with no config entries."""
    hass = _make_hass()
    return _register(hass, full_ctx)


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_all_seven_services_registered(self, handlers: dict[str, Any]) -> None:
        """Every documented service name is wired exactly once."""
        assert set(handlers) == {
            services.SERVICE_LOCATE_DEVICE,
            services.SERVICE_LOCATE_EXTERNAL,
            services.SERVICE_PLAY_SOUND,
            services.SERVICE_STOP_SOUND,
            services.SERVICE_REFRESH_DEVICE_URLS,
            services.SERVICE_REBUILD_DEVICE_REGISTRY,
            services.SERVICE_REBUILD_REGISTRY,
        }


# ---------------------------------------------------------------------------
# Input validation (no resolver reached)
# ---------------------------------------------------------------------------


class TestDeviceIdValidation:
    """The three device-scoped handlers reject a missing/blank device_id."""

    @pytest.mark.parametrize(
        "service_const",
        [
            "SERVICE_LOCATE_DEVICE",
            "SERVICE_LOCATE_EXTERNAL",
            "SERVICE_PLAY_SOUND",
            "SERVICE_STOP_SOUND",
        ],
    )
    @pytest.mark.parametrize("bad", [None, "", 123, {"nested": 1}])
    @pytest.mark.asyncio
    async def test_missing_or_non_str_device_id_raises_not_found(
        self, handlers: dict[str, Any], service_const: str, bad: Any
    ) -> None:
        handler = handlers[getattr(services, service_const)]
        with pytest.raises(ServiceValidationError) as excinfo:
            await handler(_FakeCall({"device_id": bad}))
        assert excinfo.value.translation_key == "device_not_found"

    @pytest.mark.asyncio
    async def test_stop_sound_rejects_non_str_request_uuid(
        self, handlers: dict[str, Any]
    ) -> None:
        """A non-string ``request_uuid`` fails closed before any dispatch."""
        handler = handlers[services.SERVICE_STOP_SOUND]
        with pytest.raises(ServiceValidationError) as excinfo:
            await handler(_FakeCall({"device_id": "dev1", "request_uuid": 999}))
        assert excinfo.value.translation_key == "stop_sound_failed"


# ---------------------------------------------------------------------------
# Resolver + dispatch contracts
# ---------------------------------------------------------------------------


def _hass_with_coordinator(coord: Any) -> SimpleNamespace:
    """Fake hass whose single entry exposes ``runtime_data.coordinator``."""
    runtime = SimpleNamespace(coordinator=coord)
    entry = SimpleNamespace(entry_id="e1", runtime_data=runtime, title="Account")
    return _make_hass([entry])


class TestResolverDispatch:
    @pytest.mark.asyncio
    async def test_no_active_entry_raises(self, full_ctx: dict[str, Any]) -> None:
        """With zero runtimes the handler raises the no_active_entry error."""
        hass = _make_hass()  # no entries -> no runtimes
        handlers = _register(hass, full_ctx)
        with pytest.raises(ServiceValidationError) as excinfo:
            await handlers[services.SERVICE_LOCATE_DEVICE](
                _FakeCall({"device_id": "dev1"})
            )
        assert excinfo.value.translation_key == "no_active_entry"

    @pytest.mark.asyncio
    async def test_resolver_home_assistant_error_maps_to_not_found(
        self, full_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``HomeAssistantError`` from the canonical resolver becomes a
        translated device_not_found error."""
        full_ctx["resolve_canonical"] = mock.Mock(
            side_effect=HomeAssistantError("bad id")
        )
        coord = SimpleNamespace()
        hass = _hass_with_coordinator(coord)
        handlers = _register(hass, full_ctx)
        with pytest.raises(ServiceValidationError) as excinfo:
            await handlers[services.SERVICE_LOCATE_DEVICE](
                _FakeCall({"device_id": "dev1"})
            )
        assert excinfo.value.translation_key == "device_not_found"

    @pytest.mark.asyncio
    async def test_locate_dispatches_to_coordinator(
        self, full_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolvable device dispatches ``async_locate_device`` with the
        canonical id from the resolver."""
        coord = SimpleNamespace(
            async_locate_device=mock.AsyncMock(),
            get_device_display_name=mock.Mock(return_value="Tag"),
        )
        hass = _hass_with_coordinator(coord)
        # No device-registry hit -> fall through to the coordinator scan.
        monkeypatch.setattr(
            services.dr,
            "async_get",
            lambda _h: SimpleNamespace(async_get=lambda _d: None),
        )
        handlers = _register(hass, full_ctx)
        await handlers[services.SERVICE_LOCATE_DEVICE](_FakeCall({"device_id": "dev1"}))
        coord.async_locate_device.assert_awaited_once_with("CANON")

    @pytest.mark.asyncio
    async def test_play_sound_suppressed_raises(
        self, full_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A coordinator that returns ``False`` (suppressed) surfaces as a
        play_sound_failed validation error."""
        coord = SimpleNamespace(
            async_play_sound=mock.AsyncMock(return_value=False),
            get_device_display_name=mock.Mock(return_value="Tag"),
        )
        hass = _hass_with_coordinator(coord)
        monkeypatch.setattr(
            services.dr,
            "async_get",
            lambda _h: SimpleNamespace(async_get=lambda _d: None),
        )
        handlers = _register(hass, full_ctx)
        with pytest.raises(ServiceValidationError) as excinfo:
            await handlers[services.SERVICE_PLAY_SOUND](
                _FakeCall({"device_id": "dev1"})
            )
        assert excinfo.value.translation_key == "play_sound_failed"

    @pytest.mark.parametrize(
        ("service_const", "coord_attr", "translation_key"),
        [
            ("SERVICE_LOCATE_DEVICE", "async_locate_device", "locate_failed"),
            ("SERVICE_LOCATE_EXTERNAL", "async_locate_device", "locate_failed"),
            ("SERVICE_PLAY_SOUND", "async_play_sound", "play_sound_failed"),
            ("SERVICE_STOP_SOUND", "async_stop_sound", "stop_sound_failed"),
        ],
    )
    @pytest.mark.asyncio
    async def test_downstream_failure_wrapped_as_translated_error(
        self,
        full_ctx: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        service_const: str,
        coord_attr: str,
        translation_key: str,
    ) -> None:
        """Any coordinator exception is surfaced as the handler's translated
        ``*_failed`` error, never as a raw traceback."""
        coord = SimpleNamespace(
            **{coord_attr: mock.AsyncMock(side_effect=RuntimeError("boom"))},
            get_device_display_name=mock.Mock(return_value="Tag"),
        )
        hass = _hass_with_coordinator(coord)
        monkeypatch.setattr(
            services.dr,
            "async_get",
            lambda _h: SimpleNamespace(async_get=lambda _d: None),
        )
        handlers = _register(hass, full_ctx)
        with pytest.raises(ServiceValidationError) as excinfo:
            await handlers[getattr(services, service_const)](
                _FakeCall({"device_id": "dev1"})
            )
        assert excinfo.value.translation_key == translation_key

    @pytest.mark.asyncio
    async def test_stop_sound_passes_request_uuid_through(
        self, full_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid ``request_uuid`` is forwarded verbatim to the coordinator."""
        coord = SimpleNamespace(
            async_stop_sound=mock.AsyncMock(return_value=StopSoundOutcome.CANCELLED),
            get_device_display_name=mock.Mock(return_value="Tag"),
        )
        hass = _hass_with_coordinator(coord)
        monkeypatch.setattr(
            services.dr,
            "async_get",
            lambda _h: SimpleNamespace(async_get=lambda _d: None),
        )
        handlers = _register(hass, full_ctx)
        await handlers[services.SERVICE_STOP_SOUND](
            _FakeCall({"device_id": "dev1", "request_uuid": "req-7"})
        )
        coord.async_stop_sound.assert_awaited_once_with("CANON", "req-7")

    @pytest.mark.asyncio
    async def test_uncorrelated_stop_is_reported_not_swallowed(
        self, full_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop without a cancel key must not be reported as plain success.

        The submission was accepted, but nothing proves an effect on the
        device. Returning silently would tell the user the ring was stopped
        when it may well keep playing (BSkando#195).
        """

        coord = SimpleNamespace(
            async_stop_sound=mock.AsyncMock(return_value=StopSoundOutcome.UNCORRELATED),
            get_device_display_name=mock.Mock(return_value="Tag"),
        )
        hass = _hass_with_coordinator(coord)
        monkeypatch.setattr(
            services.dr,
            "async_get",
            lambda _h: SimpleNamespace(async_get=lambda _d: None),
        )
        handlers = _register(hass, full_ctx)
        with pytest.raises(ServiceValidationError) as excinfo:
            await handlers[services.SERVICE_STOP_SOUND](
                _FakeCall({"device_id": "dev1"})
            )
        assert excinfo.value.translation_key == "stop_sound_uncorrelated"

    @pytest.mark.asyncio
    async def test_suppressed_stop_reports_its_own_cause(
        self, full_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop that was never sent gets its own, placeholder-correct key.

        ``stop_sound_failed`` carries an ``{error}`` placeholder that this call
        site cannot fill, so it would render with a stray literal.
        """

        coord = SimpleNamespace(
            async_stop_sound=mock.AsyncMock(return_value=StopSoundOutcome.FAILED),
            get_device_display_name=mock.Mock(return_value="Tag"),
        )
        hass = _hass_with_coordinator(coord)
        monkeypatch.setattr(
            services.dr,
            "async_get",
            lambda _h: SimpleNamespace(async_get=lambda _d: None),
        )
        handlers = _register(hass, full_ctx)
        with pytest.raises(ServiceValidationError) as excinfo:
            await handlers[services.SERVICE_STOP_SOUND](
                _FakeCall({"device_id": "dev1"})
            )
        assert excinfo.value.translation_key == "stop_sound_suppressed"


# ---------------------------------------------------------------------------
# Token redaction contract (refresh_device_urls)
# ---------------------------------------------------------------------------


class TestRefreshUrlRedaction:
    @pytest.mark.asyncio
    async def test_redactor_receives_raw_token_url(
        self,
        full_ctx: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The refresh handler hands the *raw* token-bearing URL to
        ``ctx["redact_url_token"]``; the persisted configuration_url carries the
        token, and only the redactor's masked output reaches the log (the raw
        token never appears in the emitted DEBUG record)."""
        entry = make_config_entry(entry_id="e1", title="Account")
        hass = _make_hass([entry])

        device = SimpleNamespace(
            id="devid",
            identifiers={(services.DOMAIN, "e1:CANON123")},
            config_entries=["e1"],
            serial_number=None,
            name="Tag",
            name_by_user=None,
        )
        update_mock = mock.Mock()
        fake_reg = SimpleNamespace(
            devices={"devid": device}, async_update_device=update_mock
        )
        monkeypatch.setattr(services.dr, "async_get", lambda _h: fake_reg)
        monkeypatch.setattr(services, "get_url", lambda *a, **k: "http://ha.local:8123")

        redactor = mock.Mock(return_value="http://ha.local:8123/...?token=***")
        full_ctx["redact_url_token"] = redactor
        handlers = _register(hass, full_ctx)

        with caplog.at_level("DEBUG", logger=services._LOGGER.name):
            await handlers[services.SERVICE_REFRESH_DEVICE_URLS](_FakeCall({}))

        # The device URL was updated with a token-bearing URL...
        update_mock.assert_called_once()
        written_url = update_mock.call_args.kwargs["configuration_url"]
        assert "/api/googlefindmy/map/CANON123?token=" in written_url
        # ...that exact raw URL is what the redactor was asked to mask...
        redactor.assert_called_once_with(written_url)
        # ...and the raw token must not leak into the emitted log record; only
        # the redactor's masked output is logged.
        raw_token = written_url.split("token=", 1)[1]
        assert raw_token not in caplog.text
        assert "token=***" in caplog.text


# ---------------------------------------------------------------------------
# rebuild_registry payload guards
# ---------------------------------------------------------------------------


class TestRebuildRegistryGuards:
    @pytest.mark.asyncio
    async def test_non_iterable_entry_id_payload_hits_type_guard(
        self, full_ctx: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-iterable ``entry_id`` payload (e.g. an int) trips the payload
        *type* guard specifically and returns without a reload.

        A ``dict`` would be iterable and slip past the type guard into the
        unresolvable-id branch, so an int is used to pin the type guard itself.
        """
        reload_mock = mock.AsyncMock()
        hass = _make_hass()
        hass.config_entries.async_reload = reload_mock  # type: ignore[attr-defined]
        handlers = _register(hass, full_ctx)
        with caplog.at_level("WARNING"):
            await handlers[services.SERVICE_REBUILD_REGISTRY](
                _FakeCall({"entry_id": 123})
            )
        reload_mock.assert_not_awaited()
        assert "Invalid entry_id payload type" in caplog.text

    @pytest.mark.asyncio
    async def test_invalid_device_ids_payload_returns_without_reload(
        self, full_ctx: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-iterable ``device_ids`` payload trips its type guard and returns
        without a reload."""
        reload_mock = mock.AsyncMock()
        hass = _make_hass()
        hass.config_entries.async_reload = reload_mock  # type: ignore[attr-defined]
        handlers = _register(hass, full_ctx)
        with caplog.at_level("WARNING"):
            await handlers[services.SERVICE_REBUILD_REGISTRY](
                _FakeCall({"device_ids": 123})
            )
        reload_mock.assert_not_awaited()
        assert "Invalid device_ids payload type" in caplog.text
