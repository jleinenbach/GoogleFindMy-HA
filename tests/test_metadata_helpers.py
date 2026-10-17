from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.network import NoURLAvailableError

import custom_components.googlefindmy as integration
from custom_components.googlefindmy.const import (
    DOMAIN,
    OPT_MAP_VIEW_ENABLED,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    map_token_hex_digest,
    map_token_secret_seed,
)
from custom_components.googlefindmy.entity import GoogleFindMyDeviceEntity
from tests.helpers.config_entries_stub import make_config_entry


@pytest.mark.asyncio
async def test_async_normalize_device_names_strips_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device names are normalized while user overrides remain untouched."""

    hass = SimpleNamespace()
    hass.data = {}

    registry = dr.async_get(hass)
    prefixed = registry.async_get_or_create(
        config_entry_id="entry-id",
        identifiers={(DOMAIN, "device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Find My - Alpha",
    )
    user_named = registry.async_get_or_create(
        config_entry_id="entry-id",
        identifiers={(DOMAIN, "device-beta")},
        manufacturer="Google",
        model="Nest",
        name="Find My - Beta",
    )
    user_named.name_by_user = "My Beta"

    await integration._async_normalize_device_names(hass)

    assert prefixed.name == "Alpha"
    assert any(update["name"] == "Alpha" for update in registry.updated)
    assert user_named.name == "Find My - Beta"
    assert not any(update["device_id"] == user_named.id for update in registry.updated)


@pytest.mark.asyncio
async def test_async_refresh_device_urls_updates_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh helper rebuilds configuration URLs for integration devices."""

    fake_now = 1_209_600  # deterministic week bucket
    base_url = "https://example.test"

    entry = SimpleNamespace(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_TOKEN_EXPIRATION: True},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)
    monkeypatch.setattr(integration.time, "time", lambda: fake_now)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )

    await integration._async_refresh_device_urls(hass)

    expected_token = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", entry.entry_id, True, now=fake_now)
    )
    assert device.configuration_url == (
        f"{base_url}/api/googlefindmy/map/device-alpha?token={expected_token}"
    )
    update = registry.updated[-1]
    assert update["device_id"] == device.id
    assert update["configuration_url"] == device.configuration_url
    assert update["translation_placeholders"] in ({}, None)
    assert update["new_identifiers"] is None


@pytest.mark.asyncio
async def test_async_refresh_device_urls_clears_url_when_map_view_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh helper clears a stale configuration_url when Map View is off.

    Rather than refreshing a link to a deliberately-unregistered endpoint, the
    device's ``configuration_url`` is cleared entirely.
    """

    base_url = "https://example.test"

    entry = make_config_entry(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_ENABLED: False},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )
    registry.async_update_device(
        device_id=device.id,
        configuration_url=f"{base_url}/api/googlefindmy/map/device-alpha?token=stale",
    )
    assert device.configuration_url is not None

    await integration._async_refresh_device_urls(hass)

    # Real HA's ``device_registry.async_update_device`` treats the ``UNDEFINED``
    # sentinel as "leave unchanged" and ``None`` as "clear this field" -- the
    # opposite of what one might guess. The shared stub registry here does not
    # model that distinction: unlike real HA, it skips applying a ``None`` value
    # to the device object itself (see ``_StubDeviceRegistry.async_update_device``
    # in ``tests/conftest.py``), so ``device.configuration_url`` stays stale even
    # though the call is correct. The recorded call in ``updated`` is therefore
    # the source of truth here, not ``device.configuration_url``: it proves the
    # production code passed ``None`` (the real clearing sentinel) for the
    # disabled case, which is what matters.
    update = registry.updated[-1]
    assert update["device_id"] == device.id
    assert update["configuration_url"] is None


@pytest.mark.asyncio
async def test_async_refresh_device_urls_skips_an_already_empty_disabled_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled entry's device without a link is neither written nor counted."""

    entry = make_config_entry(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_ENABLED: False},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    monkeypatch.setattr(
        integration, "get_url", lambda _hass, **kwargs: "https://example.test"
    )

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )
    assert device.configuration_url is None
    writes_before = len(registry.updated)

    await integration._async_refresh_device_urls(hass)

    assert [
        u for u in registry.updated[writes_before:] if u["device_id"] == device.id
    ] == []


@pytest.mark.asyncio
async def test_async_refresh_device_urls_restores_the_link_after_re_enabling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning Map View back on brings the device link back on the next refresh.

    The disabled entry leaves the device without a link; once the option is
    on again (as after the options-flow reload), the refresh writes a fresh
    tokenized URL instead of keeping the device link-less.
    """

    base_url = "https://example.test"
    entry = make_config_entry(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_ENABLED: False},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )

    await integration._async_refresh_device_urls(hass)
    assert device.configuration_url is None

    entry.options[OPT_MAP_VIEW_ENABLED] = True
    await integration._async_refresh_device_urls(hass)

    expected_token = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", entry.entry_id, False)
    )
    assert device.configuration_url == (
        f"{base_url}/api/googlefindmy/map/device-alpha?token={expected_token}"
    )


@pytest.mark.asyncio
async def test_async_refresh_device_urls_prefers_an_enabled_owner_of_a_shared_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below Core 2026.8 a disabled first owner does not clear a shared link.

    The device lists two owning entries (legacy ``config_entries``); the first
    has Map View off, the second on. The refresh signs the link for the enabled
    entry instead of clearing it.
    """

    base_url = "https://example.test"
    disabled = make_config_entry(
        entry_id="entry-1", options={OPT_MAP_VIEW_ENABLED: False}
    )
    enabled = make_config_entry(entry_id="entry-2")
    entries = {disabled.entry_id: disabled, enabled.entry_id: enabled}
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: list(entries.values())
    )
    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=disabled.entry_id,
        identifiers={(DOMAIN, f"{enabled.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )
    # Pre-2026.8 shape: no single owner, a list of owners with the disabled
    # entry first.
    device.config_entry_id = None
    device.config_entries = [disabled.entry_id, enabled.entry_id]

    await integration._async_refresh_device_urls(hass)

    expected_token = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", enabled.entry_id, False)
    )
    assert device.configuration_url == (
        f"{base_url}/api/googlefindmy/map/device-alpha?token={expected_token}"
    )


@pytest.mark.asyncio
async def test_async_refresh_device_urls_clears_disabled_url_without_a_base_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A disabled entry's stale link is cleared even with no reachable HA URL.

    Clearing ``configuration_url`` for a disabled entry needs no base URL to
    compute anything, so it must not be skipped just because Home Assistant
    has no external/internal URL configured -- and, since no *enabled* entry
    needs a base URL here either, no "no reachable URL available" warning
    should be logged.
    """

    entry = make_config_entry(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_ENABLED: False},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    def _no_url(_hass: object, **kwargs: object) -> str:
        raise NoURLAvailableError

    monkeypatch.setattr(integration, "get_url", _no_url)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )
    registry.async_update_device(
        device_id=device.id,
        configuration_url="https://stale.example/api/googlefindmy/map/device-alpha?token=stale",
    )

    caplog.set_level(logging.DEBUG)
    await integration._async_refresh_device_urls(hass)

    update = registry.updated[-1]
    assert update["device_id"] == device.id
    assert update["configuration_url"] is None

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "reachable URL" in record.getMessage()
    ]
    assert warnings == [], (
        "no enabled entry needs a base URL here, so no warning should fire"
    )


def test_device_configuration_url_warns_when_external_url_missing(
    caplog: pytest.LogCaptureFixture,
    stub_homeassistant_network,
) -> None:
    """Absolute URL resolution warns once and returns ``None`` when unavailable."""

    stub_homeassistant_network(error=NoURLAvailableError("no external URL"))

    hass = SimpleNamespace(data={"core.uuid": "ha-uuid"})
    entity = SimpleNamespace(hass=hass, _base_url_warning_emitted=False)

    caplog.set_level(logging.WARNING)

    first_result = GoogleFindMyDeviceEntity._resolve_absolute_base_url(entity)
    second_result = GoogleFindMyDeviceEntity._resolve_absolute_base_url(entity)

    assert first_result is None
    assert second_result is None

    warnings = [
        record
        for record in caplog.records
        if "Unable to resolve any Home Assistant URL for map view"
        in record.getMessage()
    ]
    assert len(warnings) == 1
    assert entity._base_url_warning_emitted is True


@pytest.mark.asyncio
async def test_periodic_refresh_follows_the_week_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two week rollovers must leave the device page with a usable link.

    The map accepts the current and the previous weekly bucket, so two rollovers
    is the first moment at which a link written at setup is certainly stale.
    """

    base_url = "https://example.test"
    week = 7 * 24 * 3600
    now = {"value": 1_209_600}

    entry = make_config_entry(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_TOKEN_EXPIRATION: True},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)
    monkeypatch.setattr(integration.time, "time", lambda: now["value"])

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )

    await integration._async_refresh_device_urls(hass)
    at_setup = device.configuration_url

    now["value"] += 2 * week
    await integration._async_refresh_device_urls(hass, periodic=True)

    expected = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", entry.entry_id, True, now=now["value"])
    )
    assert device.configuration_url != at_setup
    assert device.configuration_url == (
        f"{base_url}/api/googlefindmy/map/device-alpha?token={expected}"
    )


@pytest.mark.asyncio
async def test_periodic_refresh_reports_an_unreachable_url_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A daily timer must not turn a startup warning into a daily warning."""

    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])

    def _no_url(_hass: object, **_kwargs: object) -> str:
        raise NoURLAvailableError

    monkeypatch.setattr(integration, "get_url", _no_url)

    caplog.set_level(logging.DEBUG)
    for _ in range(10):
        await integration._async_refresh_device_urls(hass, periodic=True)

    above_debug = [
        record
        for record in caplog.records
        if record.levelno > logging.DEBUG
        and "Skipping configuration URL refresh" in record.getMessage()
    ]
    assert len(above_debug) == 1

    # A user-triggered refresh still reports every time: somebody is waiting.
    caplog.clear()
    await integration._async_refresh_device_urls(hass)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_startup_state_is_recorded_so_the_first_tick_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The startup run records the situation even though it always reports.

    With the recording behind a short-circuiting ``or``, a startup that logged
    the warning would not have stored it, and the first daily tick would have
    logged the identical line again -- the very repetition the suppressor exists
    to prevent.
    """

    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])

    def _no_url(_hass: object, **_kwargs: object) -> str:
        raise NoURLAvailableError

    monkeypatch.setattr(integration, "get_url", _no_url)

    caplog.set_level(logging.DEBUG)
    await integration._async_refresh_device_urls(hass)  # startup, reports
    caplog.clear()
    await integration._async_refresh_device_urls(hass, periodic=True)  # first tick

    assert not [record for record in caplog.records if record.levelno > logging.DEBUG]


@pytest.mark.asyncio
async def test_periodic_refresh_reports_again_after_the_situation_changes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Suppression must not swallow a *new* state, only a repeated one."""

    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [])

    state = {"url": "https://example.test"}

    def _get_url(_hass: object, **kwargs: object) -> str:
        if state["url"] is None:
            raise NoURLAvailableError
        return state["url"]

    monkeypatch.setattr(integration, "get_url", _get_url)

    caplog.set_level(logging.DEBUG)
    await integration._async_refresh_device_urls(hass, periodic=True)
    state["url"] = None
    await integration._async_refresh_device_urls(hass, periodic=True)
    await integration._async_refresh_device_urls(hass, periodic=True)

    above_debug = [
        record
        for record in caplog.records
        if record.levelno > logging.DEBUG
        and "Skipping configuration URL refresh" in record.getMessage()
    ]
    assert len(above_debug) == 1


@pytest.mark.asyncio
async def test_refresh_device_urls_uses_the_owning_entry_not_the_first_domain_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The map token is derived from the entry that owns the device.

    Two config entries of this domain, and the device belongs to the second.
    The map token is seeded per entry, so a pass that resolved the entry from
    the domain list rather than from the device would mint a URL the map view
    rejects.

    **What this pins and what it does not.** The pre-AP-16 code read the same
    ownership, through ``device.config_entries``; this test is therefore green
    on the previous state too and is a regression guard, not proof of progress.
    Its value is that it is the only test that fails when the ownership source
    is swapped for the domain list -- the shape a whole-registry walk invites,
    because the entry is no longer sitting in the loop header.
    """

    fake_now = 1_209_600
    base_url = "https://example.test"

    first_entry = make_config_entry(
        entry_id="entry-first", options={OPT_MAP_VIEW_TOKEN_EXPIRATION: True}
    )
    owning_entry = make_config_entry(
        entry_id="entry-owner", options={OPT_MAP_VIEW_TOKEN_EXPIRATION: True}
    )

    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [first_entry, owning_entry]
    )

    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)
    monkeypatch.setattr(integration.time, "time", lambda: fake_now)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=owning_entry.entry_id,
        identifiers={(DOMAIN, f"{owning_entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )

    await integration._async_refresh_device_urls(hass)

    expected_token = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", owning_entry.entry_id, True, now=fake_now)
    )
    wrong_token = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", first_entry.entry_id, True, now=fake_now)
    )
    assert expected_token != wrong_token
    assert device.configuration_url is not None
    assert device.configuration_url.endswith(f"token={expected_token}")


def test_migrate_legacy_unique_ids_scopes_the_service_device_to_this_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service-device identifier migration walks this entry's devices only.

    Two entries each carry the unscoped legacy identifier ``(DOMAIN,
    "integration")`` -- the exact state this migration exists to end. Each entry
    must namespace its own device and leave the other one alone; a pass that
    asked the registry for the wrong entry would either migrate nothing (silent
    no-op) or rewrite a device it does not own.

    **Which core this world belongs to.** From Core 2026.8 identifiers are
    unique only *within* a config entry, so two devices sharing ``(DOMAIN,
    "integration")`` across entries is a state that core produces. On the
    declared minimum ``2025.9.1`` identifiers are registry-wide unique and
    ``async_get_or_create`` would have returned the first device instead of
    creating a second; the stub does not deduplicate, so the fixture is only
    reachable on the newer core. Stated because a reader would otherwise take
    the setup for a description of both.

    Green on the previous state as well -- a regression guard. It discriminates
    against a pass that resolves the wrong entry, which is what the rewrite of
    the loop header could have introduced.
    """

    from homeassistant.helpers import entity_registry as er

    hass = SimpleNamespace(data={})
    registry = dr.async_get(hass)

    ours = registry.async_get_or_create(
        config_entry_id="entry-ours",
        identifiers={(DOMAIN, "integration")},
        manufacturer="Google",
        model="Service",
        name="Service ours",
    )
    theirs = registry.async_get_or_create(
        config_entry_id="entry-theirs",
        identifiers={(DOMAIN, "integration")},
        manufacturer="Google",
        model="Service",
        name="Service theirs",
    )

    entry = make_config_entry(entry_id="entry-ours", options={})
    ent_reg = er.async_get(hass)

    integration._migrate_legacy_unique_ids(ent_reg, registry, entry)

    assert (DOMAIN, "integration_entry-ours") in ours.identifiers
    assert (DOMAIN, "integration") not in ours.identifiers
    assert theirs.identifiers == {(DOMAIN, "integration")}


@pytest.mark.asyncio
async def test_async_refresh_device_urls_skips_a_device_with_malformed_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device whose identifiers cannot be read is skipped, the others refresh."""

    base_url = "https://example.test"
    entry = make_config_entry(entry_id="entry-1", options={OPT_MAP_VIEW_ENABLED: True})
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)

    registry = dr.async_get(hass)
    broken = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "placeholder")},
        manufacturer="Google",
        model="Nest",
        name="Broken",
    )
    # ``len()`` of a non-sized identifier raises TypeError inside the scan.
    broken.identifiers = {7}  # type: ignore[assignment]
    good = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )

    await integration._async_refresh_device_urls(hass)

    updated_ids = [update["device_id"] for update in registry.updated]
    assert good.id in updated_ids
    assert broken.id not in updated_ids


@pytest.mark.asyncio
async def test_async_refresh_device_urls_survives_a_failing_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry error while clearing one disabled link does not stop the others."""

    base_url = "https://example.test"
    entry = make_config_entry(entry_id="entry-1", options={OPT_MAP_VIEW_ENABLED: False})
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)

    registry = dr.async_get(hass)
    devices = [
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}:device-{name}")},
            manufacturer="Google",
            model="Nest",
            name=name,
        )
        for name in ("alpha", "beta")
    ]
    for device in devices:
        registry.async_update_device(
            device_id=device.id, configuration_url=f"{base_url}/stale"
        )
    registry.updated.clear()

    real_update = registry.async_update_device
    failed: list[str] = []

    def _update(**kwargs: object) -> object:
        if not failed:
            failed.append(str(kwargs["device_id"]))
            raise ValueError("registry rejected the update")
        return real_update(**kwargs)

    monkeypatch.setattr(registry, "async_update_device", _update)

    await integration._async_refresh_device_urls(hass)

    assert len(failed) == 1
    cleared = [
        u["device_id"] for u in registry.updated if u["configuration_url"] is None
    ]
    assert cleared == [d.id for d in devices if d.id != failed[0]]
