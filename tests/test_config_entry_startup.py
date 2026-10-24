from __future__ import annotations

import asyncio
import functools
import importlib
import logging
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_hass_data_layout import _prepare_async_setup_entry_harness


@pytest.mark.asyncio
async def test_device_trackers_populate_after_initial_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Initial setup should create tracker entities once the first refresh succeeds."""

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id

    loop = asyncio.get_running_loop()

    snapshot = [{"id": "tracker-1", "name": "Backpack"}]

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        self.first_refresh_calls += 1  # type: ignore[attr-defined]
        self.data = list(snapshot)

    def _find_tracker(self, device_id: str):  # type: ignore[no-untyped-def]
        del device_id

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={
            "async_config_entry_first_refresh": _first_refresh,
            "find_tracker_entity_entry": _find_tracker,
        },
    )

    harness = _prepare_async_setup_entry_harness(monkeypatch, factory, loop)
    integration = harness.integration
    entry = harness.entry
    hass = harness.hass
    coordinator_cls = harness.coordinator_cls

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    monkeypatch.setattr(device_tracker, "GoogleFindMyCoordinator", coordinator_cls)

    added_entities: list[Any] = []

    def _async_add_entities(
        entities: list[Any], update_before_add: bool = False
    ) -> None:
        added_entities.extend(entities)
        assert update_before_add is True

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry)

    if hass._tasks:
        await asyncio.gather(*hass._tasks)

    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime_data, "coordinator", None)
    assert coordinator is not None
    assert getattr(coordinator, "first_refresh_calls", 0) == 1
    assert coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY) == snapshot

    await device_tracker.async_setup_entry(hass, entry, _async_add_entities)

    assert added_entities, "Tracker entities should be created on startup"
    # Should have 2 entities per device: main tracker + last location
    assert len(added_entities) == 2
    # First entity should be the main tracker (without :last_location suffix)
    main_tracker = added_entities[0]
    assert getattr(main_tracker, "device_id", None) == "tracker-1"
    assert main_tracker.unique_id.endswith(":tracker-1")
    assert not main_tracker.unique_id.endswith(":last_location")
    # Second entity should be the last location tracker
    last_location = added_entities[1]
    assert getattr(last_location, "device_id", None) == "tracker-1"
    assert last_location.unique_id.endswith(":tracker-1:last_location")
    assert getattr(entry.runtime_data.coordinator, "first_refresh_calls", 0) == 1

    # Coordinator snapshot should reflect the first refresh payload for subsequent scans.
    follow_up = device_tracker.resolve_coordinator(entry).get_subentry_snapshot(
        TRACKER_SUBENTRY_KEY
    )
    assert follow_up == snapshot


@pytest.mark.asyncio
async def test_setup_retry_keeps_translation_and_reason_text(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed first refresh keeps the cause's key and its readable text.

    Home Assistant raises ``ConfigEntryNotReady`` from the first refresh with
    the translation fields of the ``UpdateFailed`` cause and no message of its
    own (``async_config_entry_first_refresh``). The setup wrapper must pass
    those fields on, because HA builds the setup-retry reason of the
    integration card from them. Its text must still come from the cause:
    without cached translations the core renders the bare key instead.
    """

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id

    loop = asyncio.get_running_loop()
    cause_text = "Cold start: empty device list; treating as transient."

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        del self
        cause = UpdateFailed(
            cause_text,
            translation_domain=DOMAIN,
            translation_key="cold_start_empty_device_list",
        )
        # Mirrors the core: key and domain copied, no message, cause chained.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cold_start_empty_device_list",
            translation_placeholders=None,
        ) from cause

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={"async_config_entry_first_refresh": _first_refresh},
    )
    harness = _prepare_async_setup_entry_harness(monkeypatch, factory, loop)

    assert await harness.integration.async_setup(harness.hass, {}) is True
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ConfigEntryNotReady) as excinfo,
    ):
        await harness.integration.async_setup_entry(harness.hass, harness.entry)

    err = excinfo.value
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "cold_start_empty_device_list"
    assert err.translation_placeholders is None
    assert str(err) == f"Initial refresh failed: {cause_text}"
    assert "cold_start_empty_device_list" not in str(err)

    logged = [
        r.getMessage()
        for r in caplog.records
        if "Initial coordinator refresh failed" in r.getMessage()
    ]
    assert len(logged) == 1, logged
    assert cause_text in logged[0]
    assert "cold_start_empty_device_list" not in logged[0]


def _chained(outer: Exception, inner: Exception) -> Exception:
    outer.__cause__ = inner
    return outer


def _logged_refresh_failures(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "Initial coordinator refresh failed" in r.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("boom"),
        ConfigEntryNotReady("not ready yet"),
        # Only a ConfigEntryNotReady hands its text over to the cause; any
        # other message-less exception keeps the old, empty text.
        _chained(RuntimeError(), ValueError("inner detail")),
    ],
    ids=["plain_exception", "not_ready_without_cause", "plain_exception_with_cause"],
)
async def test_setup_retry_without_translation_keeps_the_old_text(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    """A failure without a translatable cause keeps the previous behaviour.

    The text is ``Initial refresh failed: <str(err)>`` as before, the ERROR
    log names the same text, and no translation field is invented.
    """

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        del self
        raise failure

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={"async_config_entry_first_refresh": _first_refresh},
    )
    harness = _prepare_async_setup_entry_harness(
        monkeypatch, factory, asyncio.get_running_loop()
    )

    assert await harness.integration.async_setup(harness.hass, {}) is True
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ConfigEntryNotReady) as excinfo,
    ):
        await harness.integration.async_setup_entry(harness.hass, harness.entry)

    err = excinfo.value
    assert str(err) == f"Initial refresh failed: {failure}"
    assert err.__cause__ is failure
    logged = _logged_refresh_failures(caplog)
    assert len(logged) == 1, logged
    assert logged[0].endswith(f"Initial coordinator refresh failed: {failure}")
    assert err.translation_domain is None
    assert err.translation_key is None
    assert err.translation_placeholders is None


@pytest.mark.asyncio
async def test_setup_retry_keeps_an_own_message_over_the_cause(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a message-less ``ConfigEntryNotReady`` takes its text from the cause."""

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        del self
        raise ConfigEntryNotReady("not ready yet") from ValueError("inner detail")

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={"async_config_entry_first_refresh": _first_refresh},
    )
    harness = _prepare_async_setup_entry_harness(
        monkeypatch, factory, asyncio.get_running_loop()
    )

    assert await harness.integration.async_setup(harness.hass, {}) is True
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ConfigEntryNotReady) as excinfo,
    ):
        await harness.integration.async_setup_entry(harness.hass, harness.entry)

    assert str(excinfo.value) == "Initial refresh failed: not ready yet"
    logged = _logged_refresh_failures(caplog)
    assert len(logged) == 1, logged
    assert logged[0].endswith("Initial coordinator refresh failed: not ready yet")


@pytest.mark.asyncio
async def test_setup_retry_passes_the_placeholders_of_the_cause_on(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The placeholders reach the card as well, or it shows ``{status}`` raw."""

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id
    placeholders = {"status": "500"}

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        del self
        cause = UpdateFailed(
            "HTTP Server Error 500: down",
            translation_domain=DOMAIN,
            translation_key="device_list_server_error",
            translation_placeholders=placeholders,
        )
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_list_server_error",
            translation_placeholders=placeholders,
        ) from cause

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={"async_config_entry_first_refresh": _first_refresh},
    )
    harness = _prepare_async_setup_entry_harness(
        monkeypatch, factory, asyncio.get_running_loop()
    )

    assert await harness.integration.async_setup(harness.hass, {}) is True
    with pytest.raises(ConfigEntryNotReady) as excinfo:
        await harness.integration.async_setup_entry(harness.hass, harness.entry)

    err = excinfo.value
    assert err.translation_key == "device_list_server_error"
    assert err.translation_placeholders == {"status": "500"}
    assert str(err) == "Initial refresh failed: HTTP Server Error 500: down"


@pytest.mark.asyncio
async def test_setup_retry_takes_the_key_from_the_cause_on_older_cores(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Older cores raise a bare ``ConfigEntryNotReady`` from the first refresh.

    Home Assistant 2025.9.1 (the floor in ``hacs.json``) raises
    ``ConfigEntryNotReady()`` and only sets ``__cause__``; it copies no
    translation field. The wrapper then takes the fields from the cause, or
    the card stays untranslated on those versions.
    """

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id
    cause_text = "Cold start: empty device list; treating as transient."

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        del self
        raise ConfigEntryNotReady() from UpdateFailed(
            cause_text,
            translation_domain=DOMAIN,
            translation_key="cold_start_empty_device_list",
        )

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={"async_config_entry_first_refresh": _first_refresh},
    )
    harness = _prepare_async_setup_entry_harness(
        monkeypatch, factory, asyncio.get_running_loop()
    )

    assert await harness.integration.async_setup(harness.hass, {}) is True
    with pytest.raises(ConfigEntryNotReady) as excinfo:
        await harness.integration.async_setup_entry(harness.hass, harness.entry)

    err = excinfo.value
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "cold_start_empty_device_list"
    assert err.translation_placeholders is None
    assert str(err) == f"Initial refresh failed: {cause_text}"


@pytest.mark.asyncio
async def test_coordinator_first_refresh_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator should fall back to async_refresh when HA lacks helper."""

    from custom_components.googlefindmy import coordinator as coordinator_module

    monkeypatch.delattr(
        coordinator_module.DataUpdateCoordinator,
        "async_config_entry_first_refresh",
    )

    called: list[None] = []

    class _Harness(coordinator_module.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self.config_entry = make_config_entry(entry_id="entry-test")

        async def async_refresh(self) -> None:  # type: ignore[override]
            called.append(None)

    harness = _Harness()

    await harness.async_config_entry_first_refresh()

    assert called, "Fallback should invoke async_refresh when helper is absent"


async def _run_credential_seed(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
    *,
    entry_data: dict[str, Any],
    cached: dict[str, Any],
) -> Any:
    """Run ``async_setup_entry`` over a prepared entry/cache pair.

    Returns the harness so callers can read the cache the integration actually
    seeded and the seed helpers it actually called.
    """

    loop = asyncio.get_running_loop()
    factory = functools.partial(stub_coordinator_factory, data=[])
    harness = _prepare_async_setup_entry_harness(monkeypatch, factory, loop)
    harness.entry.data = dict(entry_data)
    harness.cache.values.update(cached)

    assert await harness.integration.async_setup(harness.hass, {}) is True
    assert await harness.integration.async_setup_entry(harness.hass, harness.entry)
    if harness.hass._tasks:
        await asyncio.gather(*harness.hass._tasks)
    return harness


@pytest.mark.asyncio
async def test_setup_drops_cached_credentials_the_entry_no_longer_carries(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A replaced secrets bundle must not come back out of the token cache.

    Credentials that replace older ones express "this account has no bundle any
    more" by leaving ``secrets_data`` out of ``entry.data``; a flat merge cannot
    say it any other way. The cache still mirrored the old value, and the seed
    recovered it from there, chose the bundle branch and let the integration keep
    running on exactly the credentials the user had just replaced (AGENTS.md § 4
    requires a regression for stale tokens).
    """

    harness = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={
            CONF_GOOGLE_EMAIL: "user@example.com",
            CONF_OAUTH_TOKEN: "aas_et/NEW_TOKEN_VALUE",
        },
        cached={
            DATA_SECRET_BUNDLE: {"owner_key": "STALE_OWNER_KEY"},
            DATA_AAS_TOKEN: "aas_et/OLD_TOKEN_VALUE",
        },
    )
    integration = harness.integration

    integration._async_save_secrets_data.assert_not_awaited()
    integration._async_seed_manual_credentials.assert_awaited_once()
    assert integration._async_seed_manual_credentials.await_args.args[1] == (
        "aas_et/NEW_TOKEN_VALUE"
    ), "the entry's own credentials are what the seed must use"
    assert DATA_SECRET_BUNDLE not in harness.cache.values, (
        "a superseded bundle left in the cache is found again by every "
        "cache-first fallback, not only by this seed"
    )
    assert DATA_AAS_TOKEN not in harness.cache.values


@pytest.mark.asyncio
async def test_setup_still_recovers_credentials_for_an_entry_that_has_none(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The counter-direction: the cold-start gap must still be filled.

    The recovery exists for a migrated installation whose credentials never
    reached ``entry.data`` (Issue #152): without it the FCM receiver starts with
    empty ``fcm_credentials`` and locations stay unknown. An entry that carries
    nothing of its own has no removal to respect, so nothing may be dropped.
    """

    harness = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: "user@example.com"},
        cached={
            DATA_SECRET_BUNDLE: {"owner_key": "MIGRATED_OWNER_KEY"},
            DATA_AAS_TOKEN: "aas_et/MIGRATED_TOKEN_VALUE",
        },
    )
    integration = harness.integration

    integration._async_save_secrets_data.assert_awaited_once()
    assert integration._async_save_secrets_data.await_args.args[1] == {
        "owner_key": "MIGRATED_OWNER_KEY"
    }
    assert harness.cache.values[DATA_SECRET_BUNDLE] == {
        "owner_key": "MIGRATED_OWNER_KEY"
    }, "dropping the only copy of the credentials would break the cold start"
    assert harness.cache.values[DATA_AAS_TOKEN] == "aas_et/MIGRATED_TOKEN_VALUE"


@pytest.mark.asyncio
async def test_setup_keeps_cached_credentials_the_entry_still_carries(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The other counter-direction: only *absent* keys are superseded.

    An entry that carries a bundle and an AAS token of its own says nothing
    about removals, and a drop here would delete live credentials from the cache
    on every single startup.
    """

    harness = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={
            CONF_GOOGLE_EMAIL: "user@example.com",
            DATA_SECRET_BUNDLE: {"owner_key": "CURRENT_OWNER_KEY"},
            DATA_AAS_TOKEN: "aas_et/CURRENT_TOKEN_VALUE",
        },
        cached={
            DATA_SECRET_BUNDLE: {"owner_key": "CURRENT_OWNER_KEY"},
            DATA_AAS_TOKEN: "aas_et/CURRENT_TOKEN_VALUE",
        },
    )
    integration = harness.integration

    integration._async_save_secrets_data.assert_awaited_once()
    assert harness.cache.values[DATA_SECRET_BUNDLE] == {
        "owner_key": "CURRENT_OWNER_KEY"
    }
    assert harness.cache.values[DATA_AAS_TOKEN] == "aas_et/CURRENT_TOKEN_VALUE"
