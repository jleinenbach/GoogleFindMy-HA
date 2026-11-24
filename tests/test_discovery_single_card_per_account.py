# tests/test_discovery_single_card_per_account.py
"""One discovery card per account, driven through Home Assistant's flow manager.

The login container rewrites ``secrets.json`` several times during one run, and
the bundle watcher raises a discovery flow for every rewrite that passes the
payload gate. Before the fix each of those flows stayed open as its own card, so
one login left two or three identical cards "Google Find My Device" behind.

The tests start every flow through ``hass.config_entries.flow.async_init`` and
confirm through ``async_configure`` (or the confirmation step itself, where the
test must keep Home Assistant's own protection marker out of the picture). They
count open flows with ``include_uninitialized=True``, so a regression shows up
as the number of cards rather than as an empty result.

Two harness details are deliberate:

* The suite installs stubbed Home Assistant modules, and some tests leave a
  stubbed ``homeassistant.loader`` behind. Two parts of the flow manager touch
  the loader: the handler lookup (``_async_get_flow_handler``) and, once an
  entry of the domain exists, ``_support_single_config_entry_only``. Which
  loader they see depends on which tests ran earlier in the same worker. The
  fixture replaces exactly those two, in the module the manager itself runs
  from: the first returns this integration's ``ConfigFlow``, the second the
  manifest's answer (no single-entry restriction). Everything else -- flow
  progress, aborts, ``async_configure`` -- stays Home Assistant's own code.
* ``_ingest_discovery_credentials`` is replaced: it validates tokens against
  Google, which is not what these tests are about. Payload normalisation and the
  single-key gate stay real.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

if (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is None
):  # pragma: no cover - optional dependency
    pytest.skip(
        "pytest-homeassistant-custom-component is required for the flow manager tests",
        allow_module_level=True,
    )

pytest_plugins = ("pytest_homeassistant_custom_component",)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_ACCOUNT_A = "alpha@example.com"
_ACCOUNT_B = "bravo@example.com"
# Long enough for the token plausibility check in the payload normaliser.
_TOKEN = "aas_et/" + "A" * 80


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """Activate real Home Assistant modules from the central conftest fixture."""


@pytest.fixture
async def configured_accounts(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[set[str]]:
    """Resolve the handler for the real manager and stub credential validation.

    Yields the set of account addresses that count as already configured: for
    those the stubbed validation reports an update, which makes the confirmed
    card ask before overwriting.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    manager_globals = type(hass.config_entries.flow).async_create_flow.__globals__

    async def _flow_handler(_hass: Any, domain: str, _config: Any) -> Any:
        assert domain == config_flow.DOMAIN
        return config_flow.ConfigFlow

    monkeypatch.setitem(manager_globals, "_async_get_flow_handler", _flow_handler)

    async def _single_entry_only(_hass: Any, domain: str) -> bool:
        # The manifest declares no single_config_entry. Core reads it through
        # the loader as soon as an entry of the domain exists, and the stubbed
        # loader some tests leave behind lacks the attribute.
        assert domain == config_flow.DOMAIN
        return False

    monkeypatch.setitem(
        manager_globals, "_support_single_config_entry_only", _single_entry_only
    )

    configured: set[str] = set()

    async def _fake_ingest(
        flow: Any,
        normalized: Any,
        *,
        existing_entry: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        updates = {"oauth_token": _TOKEN} if normalized.email in configured else None
        return {"data": {"oauth_token": _TOKEN}}, updates

    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _fake_ingest)
    yield configured


def _payload(email: str, **secrets: Any) -> dict[str, Any]:
    from custom_components.googlefindmy.const import (  # noqa: PLC0415
        CONF_GOOGLE_EMAIL,
    )

    return {CONF_GOOGLE_EMAIL: email, "secrets_json": {"aas_token": _TOKEN, **secrets}}


async def _discover(hass: HomeAssistant, payload: dict[str, Any]) -> Any:
    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    return await hass.config_entries.flow.async_init(
        config_flow.DOMAIN,
        context={"source": config_flow.SOURCE_DISCOVERY},
        data=payload,
    )


def _cards(hass: HomeAssistant, email: str) -> list[Any]:
    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    unique_id = config_flow.unique_account_id(config_flow.normalize_email(email))
    return list(
        hass.config_entries.flow.async_progress_by_handler(
            config_flow.DOMAIN,
            include_uninitialized=True,
            match_context={"unique_id": unique_id},
        )
    )


def _shared_key_of(hass: HomeAssistant, flow_id: str) -> Any:
    # The manager exposes results, not handlers; the pending payload is the
    # content the card would import, so the test reads it off the handler.
    flow = hass.config_entries.flow._progress[flow_id]
    return flow._pending_discovery_payload.secrets_bundle["shared_key"]


@pytest.mark.asyncio
async def test_three_bundles_of_one_account_leave_exactly_one_card(
    hass: HomeAssistant, configured_accounts: set[str]
) -> None:
    """Every new bundle of an account replaces its open card; other accounts stay."""

    await _discover(hass, _payload(_ACCOUNT_A, shared_key="A1A1"))
    await _discover(hass, _payload(_ACCOUNT_B, shared_key="B1B1"))
    await _discover(hass, _payload(_ACCOUNT_A, shared_key="A2A2"))
    third = await _discover(hass, _payload(_ACCOUNT_A, shared_key="A3A3"))

    cards_a = _cards(hass, _ACCOUNT_A)
    assert len(cards_a) == 1
    assert cards_a[0]["flow_id"] == third["flow_id"]
    assert _shared_key_of(hass, third["flow_id"]) == "A3A3"
    assert len(_cards(hass, _ACCOUNT_B)) == 1


@pytest.mark.asyncio
async def test_intermediate_rewrites_of_one_login_run_leave_one_card(
    hass: HomeAssistant, configured_accounts: set[str]
) -> None:
    """The rewrites of one login run end as one card carrying the last content."""

    rewrites: list[dict[str, Any]] = [
        {"oauth_token": _TOKEN},
        {"oauth_token": _TOKEN, "aas_token": _TOKEN},
        {"shared_key": "C1C1"},
        {"shared_key": "C1C1", "owner_key": "D1D1"},
        {
            "shared_key": "C1C1",
            "owner_key": "D1D1",
            "owner_key_versions": {"1": "D1D1"},
        },
        {
            "shared_key": "C2C2",
            "owner_key": "D1D1",
            "owner_key_versions": {"1": "D1D1"},
            "fcm_credentials": {"gcm": {"android_id": "1"}},
        },
    ]
    results = [await _discover(hass, _payload(_ACCOUNT_A, **r)) for r in rewrites]

    # The first two lack a shared key: the payload gate rejects them without a card.
    assert [r.get("reason") for r in results[:2]] == ["keys_missing", "keys_missing"]
    cards = _cards(hass, _ACCOUNT_A)
    assert len(cards) == 1
    assert cards[0]["flow_id"] == results[-1]["flow_id"]
    assert _shared_key_of(hass, results[-1]["flow_id"]) == "C2C2"


@pytest.mark.asyncio
async def test_a_card_the_user_already_submitted_is_not_superseded(
    hass: HomeAssistant, configured_accounts: set[str]
) -> None:
    """A confirmed card waiting in the overwrite question survives a new bundle.

    The confirmation step is called directly, not through ``async_configure``:
    current Home Assistant marks a flow ``dismiss_protected`` inside
    ``async_configure`` by itself, older releases do not. Calling the step keeps
    that marker out, so the test pins the integration's own marker, which is
    what protects the card on those older releases.
    """

    configured_accounts.add(_ACCOUNT_A)
    first = await _discover(hass, _payload(_ACCOUNT_A, shared_key="E1E1"))
    flow = hass.config_entries.flow._progress[first["flow_id"]]
    assert "dismiss_protected" not in flow.context

    answer = await flow.async_step_discovery_confirm({})
    assert answer["step_id"] == "discovery_overwrite"

    second = await _discover(hass, _payload(_ACCOUNT_A, shared_key="E2E2"))

    flow_ids = {card["flow_id"] for card in _cards(hass, _ACCOUNT_A)}
    assert flow_ids == {first["flow_id"], second["flow_id"]}


@pytest.mark.asyncio
async def test_a_card_submitted_through_home_assistant_is_not_superseded(
    hass: HomeAssistant, configured_accounts: set[str]
) -> None:
    """The same protection on the real path, ``async_configure``."""

    configured_accounts.add(_ACCOUNT_A)
    first = await _discover(hass, _payload(_ACCOUNT_A, shared_key="F1F1"))
    answer = await hass.config_entries.flow.async_configure(first["flow_id"], {})
    assert answer["step_id"] == "discovery_overwrite"

    second = await _discover(hass, _payload(_ACCOUNT_A, shared_key="F2F2"))

    flow_ids = {card["flow_id"] for card in _cards(hass, _ACCOUNT_A)}
    assert flow_ids == {first["flow_id"], second["flow_id"]}


@pytest.mark.asyncio
async def test_a_card_still_validating_its_bundle_is_superseded_too(
    hass: HomeAssistant,
    configured_accounts: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow whose first step has not finished yet is replaced as well.

    Validation takes a network round trip, so a rewrite can arrive while the
    previous flow is still inside its first step. Such a flow has no step yet
    and only shows up with ``include_uninitialized=True``; missed, it would
    finish afterwards and add a second card. Home Assistant reports the flow
    removed during its step as ``UnknownFlow`` to the caller (compared by name:
    the test and the manager may see different module instances).
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    release = asyncio.Event()
    calls = 0

    async def _gated_ingest(
        flow: Any,
        normalized: Any,
        *,
        existing_entry: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
        return {"data": {"oauth_token": _TOKEN}}, None

    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _gated_ingest)

    first = asyncio.ensure_future(
        _discover(hass, _payload(_ACCOUNT_A, shared_key="J1J1"))
    )
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0)
    assert calls == 1

    second = await _discover(hass, _payload(_ACCOUNT_A, shared_key="J2J2"))
    release.set()
    (outcome,) = await asyncio.gather(first, return_exceptions=True)

    assert type(outcome).__name__ == "UnknownFlow"
    flow_ids = {card["flow_id"] for card in _cards(hass, _ACCOUNT_A)}
    assert flow_ids == {second["flow_id"]}


@pytest.mark.asyncio
async def test_a_non_discovery_flow_of_the_account_is_left_alone(
    hass: HomeAssistant, configured_accounts: set[str]
) -> None:
    """Only discovery cards are replaced; a reauth flow of the account stays.

    The reauth flow is registered through the manager's private
    ``_async_add_flow_progress``: starting a real reauth needs a configured
    entry and its setup, which is not what this test is about.
    """

    from homeassistant import config_entries  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    reauth = config_flow.ConfigFlow()
    reauth.hass = hass
    reauth.handler = config_flow.DOMAIN
    reauth.flow_id = "reauth-of-account-a"
    reauth.context = {
        "source": config_entries.SOURCE_REAUTH,
        "unique_id": config_flow.unique_account_id(
            config_flow.normalize_email(_ACCOUNT_A)
        ),
    }
    hass.config_entries.flow._async_add_flow_progress(reauth)

    card = await _discover(hass, _payload(_ACCOUNT_A, shared_key="H1H1"))

    flow_ids = {flow["flow_id"] for flow in _cards(hass, _ACCOUNT_A)}
    assert flow_ids == {reauth.flow_id, card["flow_id"]}


@pytest.mark.asyncio
async def test_the_card_title_names_the_account(
    hass: HomeAssistant, configured_accounts: set[str]
) -> None:
    """The card carries the account address and Home Assistant shows it as title.

    Home Assistant renders a discovery card's title from the translation key
    ``config.flow_title`` and the flow's ``title_placeholders``; without the key
    the placeholder is never used and every card reads only the integration
    name, so two accounts cannot be told apart.
    """

    from homeassistant.helpers.translation import (  # noqa: PLC0415
        async_get_translations,
    )

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    await _discover(hass, _payload(_ACCOUNT_A, shared_key="G1G1"))
    cards = _cards(hass, _ACCOUNT_A)
    assert len(cards) == 1
    placeholders = cards[0]["context"]["title_placeholders"]
    assert placeholders["email"] == _ACCOUNT_A

    translations = await async_get_translations(
        hass, "en", "config", {config_flow.DOMAIN}
    )
    flow_title = translations.get(f"component.{config_flow.DOMAIN}.config.flow_title")
    assert flow_title is not None
    assert _ACCOUNT_A in flow_title.format(**placeholders)


# --- After a confirmation: the account's other cards close ------------------


def _closing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.INFO
        and "other discovery card" in record.getMessage()
    ]


async def _submitted_card(hass: HomeAssistant, email: str, shared_key: str) -> Any:
    """Raise a card and submit it, so it waits in the step after the card."""

    card = await _discover(hass, _payload(email, shared_key=shared_key))
    await hass.config_entries.flow.async_configure(card["flow_id"], {})
    return card


def _add_configured_entry(hass: HomeAssistant, email: str) -> Any:
    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        MockConfigEntry,
    )

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415
    from custom_components.googlefindmy.const import (  # noqa: PLC0415
        CONF_GOOGLE_EMAIL,
    )

    entry = MockConfigEntry(
        domain=config_flow.DOMAIN,
        title=email,
        data={CONF_GOOGLE_EMAIL: email},
        unique_id=config_flow.unique_account_id(config_flow.normalize_email(email)),
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_confirming_one_card_closes_the_others_of_that_account_only(
    hass: HomeAssistant,
    configured_accounts: set[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Accepting the overwrite closes every other card of the account, even answered ones."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    configured_accounts.add(_ACCOUNT_A)
    _add_configured_entry(hass, _ACCOUNT_A)
    first = await _submitted_card(hass, _ACCOUNT_A, "K1K1")
    await _submitted_card(hass, _ACCOUNT_A, "K2K2")
    await _discover(hass, _payload(_ACCOUNT_A, shared_key="K3K3"))
    await _discover(hass, _payload(_ACCOUNT_B, shared_key="L1L1"))
    assert len(_cards(hass, _ACCOUNT_A)) == 3

    caplog.set_level(logging.INFO, logger=config_flow.__name__)
    result = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"overwrite_credentials": True}
    )

    assert result["reason"] == "credentials_updated"
    assert _cards(hass, _ACCOUNT_A) == []
    assert len(_cards(hass, _ACCOUNT_B)) == 1
    records = _closing_records(caplog)
    assert len(records) == 1
    assert "closed 2 other" in records[0].getMessage()
    assert _ACCOUNT_A not in records[0].getMessage()


@pytest.mark.asyncio
async def test_declining_one_card_closes_the_others_too(
    hass: HomeAssistant,
    configured_accounts: set[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keeping the stored credentials ends the account's other cards as well."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    configured_accounts.add(_ACCOUNT_A)
    first = await _submitted_card(hass, _ACCOUNT_A, "N1N1")
    await _discover(hass, _payload(_ACCOUNT_A, shared_key="N2N2"))
    assert len(_cards(hass, _ACCOUNT_A)) == 2

    caplog.set_level(logging.INFO, logger=config_flow.__name__)
    result = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"overwrite_credentials": False}
    )

    assert result["reason"] == "credentials_kept"
    assert _cards(hass, _ACCOUNT_A) == []
    records = _closing_records(caplog)
    assert len(records) == 1
    assert "closed 1 other" in records[0].getMessage()
    assert _ACCOUNT_A not in records[0].getMessage()


def _stub_entry_creation(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the device probe and the entry setup; both talk to Google."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    async def _no_devices(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _api(self: Any) -> tuple[Any, str, str]:
        return object(), _ACCOUNT_A, _TOKEN

    monkeypatch.setattr(config_flow, "_try_probe_devices", _no_devices)
    monkeypatch.setattr(config_flow.ConfigFlow, "_async_build_api_and_username", _api)
    monkeypatch.setattr(
        hass.config_entries, "async_setup", AsyncMock(return_value=True)
    )


@pytest.mark.asyncio
async def test_a_new_entry_logs_the_cards_it_closes(
    hass: HomeAssistant,
    configured_accounts: set[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating the entry closes the other card and says so in one INFO line.

    Home Assistant aborts flows of the same unique id after a created entry by
    itself, but writes nothing; the integration closes them first so the line
    with the number exists. The device probe and the entry setup are replaced:
    both talk to Google and are not what this test is about.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    _stub_entry_creation(hass, monkeypatch)
    first = await _submitted_card(hass, _ACCOUNT_A, "M1M1")
    await _discover(hass, _payload(_ACCOUNT_A, shared_key="M2M2"))
    assert len(_cards(hass, _ACCOUNT_A)) == 2

    caplog.set_level(logging.INFO, logger=config_flow.__name__)
    result = await hass.config_entries.flow.async_configure(first["flow_id"], {})

    assert result["type"] == "create_entry"
    assert _cards(hass, _ACCOUNT_A) == []
    records = _closing_records(caplog)
    assert len(records) == 1
    assert "closed 1 other" in records[0].getMessage()
    assert _ACCOUNT_A not in records[0].getMessage()


@pytest.mark.asyncio
async def test_a_new_entry_from_a_card_stages_deleting_the_bundle_it_imported(
    hass: HomeAssistant,
    configured_accounts: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry created from a card stages the delete of exactly that bundle.

    The entry is created on the submit of the device selection. The staged job
    must name the confirmed bundle's account and content digest, and its ticket
    must survive the flow removal Home Assistant performs right after the
    entry is created; otherwise the consumed bundle stays on disk and the
    watcher offers it again after a restart.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    _stub_entry_creation(hass, monkeypatch)
    card = await _discover(hass, _payload(_ACCOUNT_A, shared_key="C1C1"))
    # The content the card carries is the bundle the entry will import.
    confirmed = hass.config_entries.flow._progress[
        card["flow_id"]
    ]._pending_discovery_payload
    assert confirmed is not None
    await hass.config_entries.flow.async_configure(card["flow_id"], {})

    result = await hass.config_entries.flow.async_configure(card["flow_id"], {})
    assert result["type"] == "create_entry"

    tickets = hass.data.get(config_flow.DOMAIN, {}).get(
        config_flow.PENDING_CONTAINER_CLEANUP_KEY
    )
    assert tickets, "no cleanup ticket survived the created entry"
    jobs = [job for ticket in tickets for job in ticket.jobs]
    assert len(jobs) == 1
    assert jobs[0].imported_stable_key == (
        config_flow._stable_key_for_discovery_payload(confirmed)
    )
    assert jobs[0].imported_digest == config_flow._digest_for_discovery_payload(
        confirmed
    )
    assert jobs[0].imported_digest is not None


# --- After a confirmation: a younger bundle on disk comes back ----------------


async def _start_watcher(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, paths: list[Path]
) -> Any:
    """Run the real watcher over ``paths`` and let it start real flows.

    Only the hop through Home Assistant's discovery helper is replaced: the
    watcher's dispatch builds the payload with the production helper and starts
    the flow through ``async_init``.
    """

    from custom_components.googlefindmy import config_flow, discovery  # noqa: PLC0415

    monkeypatch.setattr(discovery, "_default_watch_paths", lambda: list(paths))
    monkeypatch.setattr(discovery, "_collect_extra_watch_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)

    async def _trigger(_hass: Any, **kwargs: Any) -> Any:
        payload = discovery._assemble_cloud_discovery_payload(
            email=kwargs["email"],
            token=kwargs["token"],
            secrets_bundle=kwargs.get("secrets_bundle"),
            discovery_ns=kwargs.get("discovery_ns") or "",
            discovery_stable_key=kwargs.get("discovery_stable_key") or "",
            title=kwargs.get("title"),
            source=config_flow.SOURCE_DISCOVERY,
        )
        # Set by key rather than through the helper's keyword, so the harness
        # also runs against a tree whose helper predates the keyword.
        if kwargs.get("bundle_mtime") is not None:
            payload["discovery_bundle_mtime"] = kwargs["bundle_mtime"]
        await _discover(hass, payload)
        return discovery.CloudDiscoveryOutcome.ACCEPTED

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _trigger)

    manager = discovery.DiscoveryManager(hass)
    await manager.async_start()
    hass.data.setdefault(config_flow.DOMAIN, {})["discovery_manager"] = manager
    return manager


@pytest.fixture
async def watched_bundle(
    hass: HomeAssistant,
    configured_accounts: set[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[Path, Any]]:
    """One watched file; the account counts as configured.

    A confirmed card therefore waits in the overwrite question.
    """

    path = tmp_path / "data" / "secrets.json"
    configured_accounts.add(_ACCOUNT_A)
    manager = await _start_watcher(hass, monkeypatch, [path])
    try:
        yield path, manager
    finally:
        await manager.async_stop()


def _write_bundle(
    path: Path, shared_key: str, mtime: int, *, token: str = _TOKEN
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"google_email": _ACCOUNT_A, "aas_token": token, "shared_key": shared_key}
        ),
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


async def _scan(hass: HomeAssistant, manager: Any) -> None:
    await manager.async_force_secrets_scan()
    await hass.async_block_till_done()


async def _confirm_older_while_younger_is_open(
    hass: HomeAssistant, path: Path, manager: Any
) -> tuple[str, str]:
    """B1 answered, B2 (younger, on disk) open as a card; then B1 is declined."""

    _write_bundle(path, "B1B1", 1_000)
    await _scan(hass, manager)
    (older,) = _cards(hass, _ACCOUNT_A)
    await hass.config_entries.flow.async_configure(older["flow_id"], {})

    _write_bundle(path, "B2B2", 2_000)
    await _scan(hass, manager)
    younger = next(
        card["flow_id"]
        for card in _cards(hass, _ACCOUNT_A)
        if card["flow_id"] != older["flow_id"]
    )
    assert _shared_key_of(hass, younger) == "B2B2"

    result = await hass.config_entries.flow.async_configure(
        older["flow_id"], {"overwrite_credentials": False}
    )
    assert result["reason"] == "credentials_kept"
    return older["flow_id"], younger


@pytest.mark.asyncio
async def test_a_younger_bundle_closed_by_a_confirmation_comes_back_once(
    hass: HomeAssistant, watched_bundle: tuple[Path, Any]
) -> None:
    """The younger bundle's card closes with the confirmation and returns once.

    Without the re-arm the watcher keeps the younger bundle's signature as
    already dispatched, and the bundle on disk would never show up again.
    """

    path, manager = watched_bundle
    _older, younger = await _confirm_older_while_younger_is_open(hass, path, manager)

    await _scan(hass, manager)
    await _scan(hass, manager)

    cards = _cards(hass, _ACCOUNT_A)
    assert len(cards) == 1
    assert cards[0]["flow_id"] != younger
    assert _shared_key_of(hass, cards[0]["flow_id"]) == "B2B2"


@pytest.mark.asyncio
async def test_a_younger_bundle_after_confirmation_becomes_exactly_one_new_card(
    hass: HomeAssistant, watched_bundle: tuple[Path, Any]
) -> None:
    """A bundle written after the confirmation is exactly one card; no entry changes."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    path, manager = watched_bundle
    await _confirm_older_while_younger_is_open(hass, path, manager)

    _write_bundle(path, "B3B3", 3_000)
    await _scan(hass, manager)

    cards = _cards(hass, _ACCOUNT_A)
    assert len(cards) == 1
    assert _shared_key_of(hass, cards[0]["flow_id"]) == "B3B3"
    assert hass.config_entries.async_entries(config_flow.DOMAIN) == []


def test_rearm_keeps_the_confirmed_digest_settled(tmp_path: Path) -> None:
    """Re-arming drops the account's other signatures and keeps the confirmed one."""

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    watcher = discovery.SecretsJSONWatcher(
        AsyncMock(), paths=[tmp_path / "secrets.json"]
    )
    rearm = getattr(watcher, "rearm_account", None)
    assert rearm is not None

    watcher._settled_signatures.update(
        {"email:a:confirmed", "email:a:younger", "email:a:other", "email:b:x"}
    )
    watcher._retry_attempts["email:a:younger"] = 1

    assert rearm("email:a", keep_digest="confirmed") == 2
    assert watcher._settled_signatures == {"email:a:confirmed", "email:b:x"}
    assert watcher._retry_attempts == {"email:a:younger": 1}


# --- A confirmation imports the newest bundle of the account ------------------

_OLDER_TOKEN = "aas_et/" + "O" * 80
_YOUNGER_TOKEN = "aas_et/" + "Y" * 80


@pytest.fixture
async def two_watched_files(
    hass: HomeAssistant,
    configured_accounts: set[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[Path, Path, Any, set[str]]]:
    """Two watched files and a credential check that keeps the token apart.

    The stubbed check returns the bundle's own token as ``oauth_token``, so the
    entry tells which bundle was imported. Tokens in the yielded set fail the
    check with ``cannot_connect``.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415
    from custom_components.googlefindmy.const import (  # noqa: PLC0415
        CONF_GOOGLE_EMAIL,
        CONF_OAUTH_TOKEN,
    )

    failing: set[str] = set()

    async def _ingest(
        flow: Any,
        normalized: Any,
        *,
        existing_entry: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        token = normalized.secrets_bundle["aas_token"]
        if token in failing:
            raise config_flow.DiscoveryFlowError("cannot_connect")
        auth = {CONF_OAUTH_TOKEN: token, CONF_GOOGLE_EMAIL: normalized.email}
        updates = dict(auth) if normalized.email in configured_accounts else None
        return auth, updates

    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _ingest)
    _stub_entry_creation(hass, monkeypatch)
    first = tmp_path / "auth" / "secrets.json"
    second = tmp_path / "data" / "secrets.json"
    manager = await _start_watcher(
        hass, monkeypatch, [first, second, _third_watched_path(tmp_path)]
    )
    try:
        yield first, second, manager, failing
    finally:
        await manager.async_stop()


def _third_watched_path(tmp_path: Path) -> Path:
    """A third path the ``two_watched_files`` watcher observes; absent unless written."""

    return tmp_path / "extra" / "secrets.json"


def _imported_token(hass: HomeAssistant) -> Any:
    from custom_components.googlefindmy import config_flow  # noqa: PLC0415
    from custom_components.googlefindmy.const import (  # noqa: PLC0415
        CONF_OAUTH_TOKEN,
    )

    (entry,) = hass.config_entries.async_entries(config_flow.DOMAIN)
    return entry.data.get(CONF_OAUTH_TOKEN)


async def _card_for_older_bundle(hass: HomeAssistant, first: Path, manager: Any) -> str:
    _write_bundle(first, "O1O1", 1_000, token=_OLDER_TOKEN)
    await _scan(hass, manager)
    (card,) = _cards(hass, _ACCOUNT_A)
    return str(card["flow_id"])


@pytest.mark.asyncio
async def test_confirmation_imports_the_younger_of_two_bundles(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The card carries the older bundle; the younger one on disk is imported."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)

    caplog.set_level(logging.INFO, logger=config_flow.__name__)
    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _YOUNGER_TOKEN
    replaced = [
        record
        for record in caplog.records
        if "newer sign-in bundle replaced" in record.getMessage()
    ]
    assert len(replaced) == 1
    assert _ACCOUNT_A not in replaced[0].getMessage()


@pytest.mark.asyncio
async def test_an_older_bundle_on_disk_does_not_replace_the_confirmed_one(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """Only a bundle that is newer than the confirmed content counts."""

    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "Y1Y1", 500, token=_YOUNGER_TOKEN)

    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _OLDER_TOKEN


@pytest.mark.asyncio
async def test_accepting_the_overwrite_imports_a_bundle_written_meanwhile(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """A bundle written while the overwrite question is open is the one written."""

    configured_accounts.add(_ACCOUNT_A)
    _add_configured_entry(hass, _ACCOUNT_A)
    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    question = await hass.config_entries.flow.async_configure(card, {})
    assert question["step_id"] == "discovery_overwrite"

    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)
    result = await hass.config_entries.flow.async_configure(
        card, {"overwrite_credentials": True}
    )

    assert result["reason"] == "credentials_updated"
    assert _imported_token(hass) == _YOUNGER_TOKEN


@pytest.mark.asyncio
async def test_a_younger_bundle_that_fails_its_check_imports_nothing(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """The older bundle is not imported instead; the younger one comes back."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, manager, failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)
    failing.add(_YOUNGER_TOKEN)

    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"
    assert hass.config_entries.async_entries(config_flow.DOMAIN) == []

    failing.clear()
    await _scan(hass, manager)
    cards = _cards(hass, _ACCOUNT_A)
    assert len(cards) == 1
    assert _shared_key_of(hass, cards[0]["flow_id"]) == "Y1Y1"


@pytest.mark.asyncio
async def test_replacing_while_confirming_imports_once_without_error(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """New content and the confirmation in the same loop turn: one import, no card."""

    configured_accounts.add(_ACCOUNT_A)
    _add_configured_entry(hass, _ACCOUNT_A)
    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    await hass.config_entries.flow.async_configure(card, {})

    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)
    results = await asyncio.gather(
        _scan(hass, manager),
        hass.config_entries.flow.async_configure(card, {"overwrite_credentials": True}),
    )
    await hass.async_block_till_done()

    confirmed = results[1]
    assert confirmed["type"] == "abort"
    assert confirmed["reason"] == "credentials_updated"
    assert _imported_token(hass) == _YOUNGER_TOKEN
    assert _cards(hass, _ACCOUNT_A) == []


@pytest.mark.asyncio
async def test_a_card_for_the_bundle_just_answered_is_dropped(
    hass: HomeAssistant,
    configured_accounts: set[str],
) -> None:
    """The same bundle arriving right after the answer raises no new card.

    This is the deterministic half of the race above: whichever order the scan
    and the confirmation take, a card for the answered content must not stay.
    A different bundle of the account still raises its card.
    """

    configured_accounts.add(_ACCOUNT_A)
    card = await _submitted_card(hass, _ACCOUNT_A, "D1D1")
    result = await hass.config_entries.flow.async_configure(
        card["flow_id"], {"overwrite_credentials": False}
    )
    assert result["reason"] == "credentials_kept"

    again = await _discover(hass, _payload(_ACCOUNT_A, shared_key="D1D1"))

    assert again["type"] == "abort"
    assert again["reason"] == "already_configured"
    assert _cards(hass, _ACCOUNT_A) == []
    other = await _discover(hass, _payload(_ACCOUNT_A, shared_key="D2D2"))
    assert other["type"] == "form"


@pytest.mark.asyncio
async def test_a_younger_copy_of_the_confirmed_bundle_is_not_a_newer_one(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same content written again later is not reported as a replacement."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "O1O1", 3_000, token=_OLDER_TOKEN)

    caplog.set_level(logging.INFO, logger=config_flow.__name__)
    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _OLDER_TOKEN
    assert not [
        record
        for record in caplog.records
        if "newer sign-in bundle replaced" in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_an_older_bundle_between_two_copies_of_the_confirmed_one_is_not_newer(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The newest copy of the confirmed content is the yardstick, not the oldest."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)
    _write_bundle(_third_watched_path(tmp_path), "O1O1", 3_000, token=_OLDER_TOKEN)

    caplog.set_level(logging.INFO, logger=config_flow.__name__)
    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _OLDER_TOKEN
    assert not [
        record
        for record in caplog.records
        if "newer sign-in bundle replaced" in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_a_bundle_rewritten_in_place_while_the_card_validates_is_imported(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card's time boundary is taken before its credential check, not after."""

    import time  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, _second, manager, _failing = two_watched_files
    validate = config_flow._ingest_discovery_credentials
    rewrites: list[float] = []

    async def _rewrite_during_check(*args: Any, **kwargs: Any) -> Any:
        if not rewrites:
            now = time.time()
            rewrites.append(now)
            first.write_text(
                json.dumps(
                    {
                        "google_email": _ACCOUNT_A,
                        "aas_token": _YOUNGER_TOKEN,
                        "shared_key": "Y1Y1",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(first, (now, now))
        return await validate(*args, **kwargs)

    monkeypatch.setattr(
        config_flow, "_ingest_discovery_credentials", _rewrite_during_check
    )
    card = await _card_for_older_bundle(hass, first, manager)
    assert _shared_key_of(hass, card) == "O1O1"
    assert len(rewrites) == 1

    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _YOUNGER_TOKEN


def _rewrite_while_the_watcher_renders_the_title(
    monkeypatch: pytest.MonkeyPatch, path: Path, mtime: int
) -> list[int]:
    """Rewrite ``path`` in place after the watcher read it, before the flow starts.

    The watcher awaits the card title between reading a bundle and dispatching
    it; the login container can replace the file in exactly that gap.
    """

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    render = discovery.SecretsJSONWatcher._async_render_title
    rewrites: list[int] = []

    async def _render(self: Any, email: str, *, is_update: bool) -> Any:
        if not rewrites:
            rewrites.append(mtime)
            _write_bundle(path, "Y1Y1", mtime, token=_YOUNGER_TOKEN)
        return await render(self, email, is_update=is_update)

    monkeypatch.setattr(discovery.SecretsJSONWatcher, "_async_render_title", _render)
    return rewrites


@pytest.mark.asyncio
async def test_a_bundle_tied_with_a_confirmed_copy_on_disk_is_not_newer(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """An exact mtime tie with the confirmed content still on disk keeps it.

    Neither bundle is provably newer, so the card keeps what the user confirmed
    instead of following the scan order's digest tiebreak.
    """

    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "Y1Y1", 1_000, token=_YOUNGER_TOKEN)

    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _OLDER_TOKEN


@pytest.mark.asyncio
async def test_a_bundle_rewritten_between_the_watcher_read_and_the_card_is_imported(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card's boundary is the read bundle's mtime, not the step's start time."""

    import time  # noqa: PLC0415

    first, _second, manager, _failing = two_watched_files
    rewrites = _rewrite_while_the_watcher_renders_the_title(
        monkeypatch, first, int(time.time()) - 30
    )
    card = await _card_for_older_bundle(hass, first, manager)
    assert _shared_key_of(hass, card) == "O1O1"
    assert len(rewrites) == 1

    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _YOUNGER_TOKEN


@pytest.mark.asyncio
async def test_a_rewrite_with_the_same_mtime_as_the_card_bundle_is_imported(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coarse mtimes: the replacement of the card's file may carry the same stamp.

    The card's own content is excluded by its digest, so a different bundle of
    the account with the card's mtime has replaced it and is not older.
    """

    first, _second, manager, _failing = two_watched_files
    _rewrite_while_the_watcher_renders_the_title(monkeypatch, first, 1_000)
    card = await _card_for_older_bundle(hass, first, manager)
    assert _shared_key_of(hass, card) == "O1O1"

    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _YOUNGER_TOKEN


@pytest.mark.asyncio
async def test_after_a_swap_the_boundary_is_the_swapped_in_bundle(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
    configured_accounts: set[str],
    tmp_path: Path,
) -> None:
    """A bundle older than the one swapped in at confirmation is not newer later.

    The account is configured, so the confirmation swaps in the younger bundle
    and asks the overwrite question. When the swapped-in bundle has left the
    disk by then, a bundle written between the card's and the swapped-in
    bundle's mtime must not replace it.
    """

    configured_accounts.add(_ACCOUNT_A)
    _add_configured_entry(hass, _ACCOUNT_A)
    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(second, "Y1Y1", 3_000, token=_YOUNGER_TOKEN)

    result = await hass.config_entries.flow.async_configure(card, {})
    assert result["step_id"] == "discovery_overwrite"

    second.unlink()
    _write_bundle(_third_watched_path(tmp_path), "X1X1", 2_000, token=_TOKEN)
    result = await hass.config_entries.flow.async_configure(
        card, {"overwrite_credentials": True}
    )

    assert result["reason"] == "credentials_updated"
    assert _imported_token(hass) == _YOUNGER_TOKEN


@pytest.mark.asyncio
async def test_an_older_bundle_does_not_replace_a_confirmed_one_that_left_the_disk(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """Without the confirmed copy on disk, an older bundle still is not newer.

    The card carries the newest bundle of the account when it is raised, so a
    bundle written before that moment cannot be younger than the card.
    """

    import time  # noqa: PLC0415

    first, second, manager, _failing = two_watched_files
    _write_bundle(first, "O1O1", 1_000, token=_OLDER_TOKEN)
    _write_bundle(second, "Y1Y1", int(time.time()) - 5, token=_YOUNGER_TOKEN)
    await _scan(hass, manager)
    (card,) = _cards(hass, _ACCOUNT_A)
    assert _shared_key_of(hass, card["flow_id"]) == "Y1Y1"
    second.unlink()

    await hass.config_entries.flow.async_configure(card["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(card["flow_id"], {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _YOUNGER_TOKEN


@pytest.mark.asyncio
async def test_a_bundle_rewritten_in_place_after_the_card_is_imported(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """The confirmed file was overwritten after the card appeared: take the new one."""

    import time  # noqa: PLC0415

    first, _second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    _write_bundle(first, "Y1Y1", int(time.time()) + 60, token=_YOUNGER_TOKEN)

    await hass.config_entries.flow.async_configure(card, {})
    result = await hass.config_entries.flow.async_configure(card, {})

    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _YOUNGER_TOKEN


@pytest.mark.asyncio
async def test_the_same_bundle_dropped_again_later_raises_a_card(
    hass: HomeAssistant,
    configured_accounts: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record only covers the moment of the answer, not a later re-drop."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    configured_accounts.add(_ACCOUNT_A)
    card = await _submitted_card(hass, _ACCOUNT_A, "G1G1")
    await hass.config_entries.flow.async_configure(
        card["flow_id"], {"overwrite_credentials": False}
    )
    # raising=False: before the grace period existed, the test fails at the
    # assertion below rather than at a missing name.
    monkeypatch.setattr(
        config_flow, "ANSWERED_DISCOVERY_BUNDLE_GRACE", 0.0, raising=False
    )

    again = await _discover(hass, _payload(_ACCOUNT_A, shared_key="G1G1"))

    assert again["type"] == "form"


@pytest.mark.asyncio
async def test_without_a_confirmed_copy_or_a_boundary_nothing_counts_as_newer(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """With the confirmed content gone and no mtime boundary, no bundle is newer.

    The same bundle does count once the card carries a boundary below its
    mtime, so the ``None`` comes from the missing yardstick, not the bundle.
    """

    from custom_components.googlefindmy import config_flow, discovery  # noqa: PLC0415

    _first, second, _manager, _failing = two_watched_files
    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)
    ((_path, scanned),) = discovery.scan_secrets_bundles([second])

    without = await config_flow._async_freshest_watched_bundle(
        hass, stable_key=scanned.stable_key, digest="not-on-disk", not_before=None
    )
    with_boundary = await config_flow._async_freshest_watched_bundle(
        hass, stable_key=scanned.stable_key, digest="not-on-disk", not_before=1_500
    )

    assert without is None
    assert with_boundary is not None
    assert with_boundary.digest == scanned.digest


@pytest.mark.asyncio
async def test_a_younger_bundle_failing_its_check_at_the_overwrite_writes_nothing(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """Accepting the overwrite aborts when the newest bundle fails its check.

    The older bundle of the card is not written into the entry instead.
    """

    from custom_components.googlefindmy.const import (  # noqa: PLC0415
        CONF_OAUTH_TOKEN,
    )

    configured_accounts.add(_ACCOUNT_A)
    entry = _add_configured_entry(hass, _ACCOUNT_A)
    first, second, manager, failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    question = await hass.config_entries.flow.async_configure(card, {})
    assert question["step_id"] == "discovery_overwrite"

    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)
    failing.add(_YOUNGER_TOKEN)
    result = await hass.config_entries.flow.async_configure(
        card, {"overwrite_credentials": True}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"
    assert CONF_OAUTH_TOKEN not in entry.data


@pytest.mark.asyncio
async def test_the_import_cleanup_is_staged_at_most_once_per_flow(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both entry-creation sites may call the staging; only the first stages."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    staged: list[Any] = []
    monkeypatch.setattr(
        config_flow,
        "_async_stage_container_cleanup",
        lambda *args, **kwargs: staged.append(kwargs.get("job")),
    )
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]

    flow._async_stage_discovery_import_cleanup(None)
    flow._async_stage_discovery_import_cleanup(None)

    assert len(staged) == 1


def _normalized(email: str) -> Any:
    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    return config_flow._normalize_and_validate_discovery_payload(
        _payload(email, shared_key="S1S1")
    )


@pytest.mark.asyncio
async def test_the_freshest_bundle_helper_needs_a_data_mapping() -> None:
    """A Home Assistant object without a ``data`` mapping yields no bundle."""

    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    result = await config_flow._async_freshest_watched_bundle(
        SimpleNamespace(data=None), stable_key="key", digest="digest"
    )

    assert result is None


def test_an_answer_for_one_account_does_not_count_for_another(
    hass: HomeAssistant,
) -> None:
    """Only the answered account's bundle counts as already answered."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    answered, other = _normalized(_ACCOUNT_A), _normalized(_ACCOUNT_B)
    flow._async_remember_answered_bundle(answered)

    assert flow._bundle_already_answered(answered) is True
    assert flow._bundle_already_answered(other) is False


def test_an_answer_record_without_a_float_time_is_ignored(
    hass: HomeAssistant,
) -> None:
    """A record whose time is not a monotonic float does not count."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    payload = _normalized(_ACCOUNT_A)
    flow._async_remember_answered_bundle(payload)
    records = hass.data[config_flow.DOMAIN][config_flow.ANSWERED_DISCOVERY_BUNDLES_KEY]
    ((key, (digest, answered_at)),) = records.items()

    # Still inside the grace period, only no longer a float.
    records[key] = (digest, int(answered_at))

    assert flow._bundle_already_answered(payload) is False


def test_rearming_without_a_payload_leaves_the_watcher_alone(
    hass: HomeAssistant,
) -> None:
    """Without a payload there is no account, so nothing is re-armed."""

    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    calls: list[tuple[Any, Any]] = []
    manager = SimpleNamespace(
        rearm_account=lambda key, keep_digest=None: calls.append((key, keep_digest))
    )
    hass.data.setdefault(config_flow.DOMAIN, {})["discovery_manager"] = manager
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]

    flow._async_rearm_account_bundles(None)
    assert calls == []

    flow._async_rearm_account_bundles(_normalized(_ACCOUNT_A))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_bundle_swap_runs_without_a_config_entries_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host without ``config_entries`` checks the newer bundle as a new account.

    No existing entry is looked up then; the check still runs, and its failure
    still ends the swap with ``None``.
    """

    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    fresher = SimpleNamespace(
        email=_ACCOUNT_A,
        token=_TOKEN,
        bundle={"aas_token": _TOKEN, "shared_key": "S1S1"},
        stable_key="key",
        mtime=2_000.0,
    )

    async def _fresher(*_args: Any, **_kwargs: Any) -> Any:
        return fresher

    seen: list[Any] = []

    async def _ingest(_flow: Any, _payload: Any, *, existing_entry: Any) -> Any:
        seen.append(existing_entry)
        raise config_flow.DiscoveryFlowError("invalid_auth")

    monkeypatch.setattr(config_flow, "_async_freshest_watched_bundle", _fresher)
    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _ingest)
    flow = config_flow.ConfigFlow()
    flow.hass = SimpleNamespace(data={})  # type: ignore[assignment]

    result = await flow._async_take_freshest_bundle(_normalized(_ACCOUNT_A), {})

    assert result is None
    assert seen == [None]


# --- After an import: an older bundle of the account raises no card -----------
# F2 (NA-11 variant b): the entry remembers which bundle state it imported, and
# a later card whose bundle is not newer than that state is not offered. The
# delete-after-import hook stays at ``min`` and deletes nothing extra.

_IMPORTED_TOKEN = "aas_et/" + "I" * 80
_BETWEEN_TOKEN = "aas_et/" + "B" * 80
_NEWEST_TOKEN = "aas_et/" + "N" * 80


async def _import_newest(
    hass: HomeAssistant,
    configured_accounts: set[str],
    manager: Any,
) -> None:
    """Scan, confirm the single card and create the entry from it.

    Afterwards the account counts as configured, so the stubbed credential check
    reports an update for any later card, as the real check would.
    """

    await _scan(hass, manager)
    (card,) = _cards(hass, _ACCOUNT_A)
    await hass.config_entries.flow.async_configure(card["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(card["flow_id"], {})
    assert result["type"] == "create_entry"
    assert _imported_token(hass) == _IMPORTED_TOKEN
    configured_accounts.add(_ACCOUNT_A)


async def _run_staged_cleanup(hass: HomeAssistant) -> None:
    """Run the delete-after-import hook for the job the import staged.

    The entry setup is stubbed in this module, so the durability gate that
    normally runs the hook never fires; the test runs the hook itself with the
    very identity the import staged.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    tickets = hass.data[config_flow.DOMAIN][config_flow.PENDING_CONTAINER_CLEANUP_KEY]
    (job,) = [job for ticket in tickets for job in ticket.jobs]
    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=job.imported_stable_key,
        imported_digest=job.imported_digest,
    )


def _bundle_between_two_copies_of_the_import(
    first: Path, second: Path, third: Path
) -> None:
    _write_bundle(first, "I1I1", 1_000, token=_IMPORTED_TOKEN)
    _write_bundle(second, "B1B1", 2_000, token=_BETWEEN_TOKEN)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)


@pytest.mark.asyncio
async def test_an_older_bundle_left_by_the_cleanup_raises_no_card(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bundle between two copies of the import survives the hook, but no card.

    The hook keeps it because it is not older than *every* copy of the imported
    content (its documented, deliberately cautious rule). After the copies are
    gone it is the newest bundle of the account on disk, yet it is older than
    what the entry imported, so offering it would invite replacing newer
    credentials with older ones.
    """

    first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _bundle_between_two_copies_of_the_import(first, second, third)
    await _import_newest(hass, configured_accounts, manager)

    caplog.set_level(logging.INFO, logger="custom_components.googlefindmy.config_flow")
    await _run_staged_cleanup(hass)
    assert not first.exists()
    assert not third.exists()
    assert second.exists(), "the hook must keep the bundle it cannot prove stale"
    kept = [
        record.getMessage()
        for record in caplog.records
        if str(second) in record.getMessage()
    ]
    assert len(kept) == 1
    assert "it raises no discovery card" in kept[0]

    await _scan(hass, manager)

    assert _cards(hass, _ACCOUNT_A) == []
    assert _imported_token(hass) == _IMPORTED_TOKEN


@pytest.mark.asyncio
async def test_after_a_restart_an_older_bundle_still_raises_no_card(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The imported state lives in the entry, not in memory.

    A restart is reproduced by stopping the watcher, dropping everything the
    integration keeps in ``hass.data`` and starting a fresh watcher over the
    same paths. Only the config entry carries over, as it does across a real
    Home Assistant restart.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _bundle_between_two_copies_of_the_import(first, second, third)
    await _import_newest(hass, configured_accounts, manager)
    await _run_staged_cleanup(hass)

    await manager.async_stop()
    hass.data[config_flow.DOMAIN].clear()
    restarted = await _start_watcher(hass, monkeypatch, [first, second, third])
    try:
        await _scan(hass, restarted)
        assert _cards(hass, _ACCOUNT_A) == []
    finally:
        await restarted.async_stop()


@pytest.mark.asyncio
async def test_an_older_bundle_left_on_a_read_only_mount_raises_no_card(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
) -> None:
    """Where the hook cannot delete, an older bundle must not come back as a card.

    On a read-only mount the hook removes nothing. The watcher hides the older
    bundle only while the newer copy of the same account is on disk
    (``SecretsJSONWatcher._read_bundles``); once that copy goes away the older
    one is the newest left. The read-only mount is reproduced by not running the
    hook at all, which is exactly what it amounts to.
    """

    _first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _write_bundle(second, "B1B1", 2_000, token=_BETWEEN_TOKEN)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)

    third.unlink()
    await _scan(hass, manager)

    assert _cards(hass, _ACCOUNT_A) == []


# --- Controls: the recorded state turns away only what is not newer -----------


async def _import_then_leave_only(
    hass: HomeAssistant,
    configured_accounts: set[str],
    manager: Any,
    imported: Path,
    mtime: int,
) -> None:
    """Import the single bundle at ``imported``, then remove it like the hook would."""

    _write_bundle(imported, "I1I1", mtime, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)
    imported.unlink()
    await _scan(hass, manager)


@pytest.mark.asyncio
async def test_a_bundle_newer_than_the_import_still_raises_a_card(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
) -> None:
    _first, second, manager, _failing = two_watched_files
    await _import_then_leave_only(
        hass, configured_accounts, manager, _third_watched_path(tmp_path), 3_000
    )

    _write_bundle(second, "N1N1", 4_000, token=_NEWEST_TOKEN)
    await _scan(hass, manager)

    assert len(_cards(hass, _ACCOUNT_A)) == 1


@pytest.mark.asyncio
async def test_a_different_bundle_tied_with_the_import_raises_no_card(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
) -> None:
    """A tie is undecidable and counts as not newer, as for the confirmation."""

    _first, second, manager, _failing = two_watched_files
    await _import_then_leave_only(
        hass, configured_accounts, manager, _third_watched_path(tmp_path), 3_000
    )

    _write_bundle(second, "B1B1", 3_000, token=_BETWEEN_TOKEN)
    await _scan(hass, manager)

    assert _cards(hass, _ACCOUNT_A) == []


@pytest.mark.asyncio
async def test_an_import_stamped_in_the_future_does_not_hide_later_cards(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
) -> None:
    """The recorded time is capped at the clock of the import.

    The watcher accepts a future mtime. Recorded as is, it would turn away every
    later bundle of the account until that date; a bundle written a minute after
    the import must raise its card.
    """

    import time  # noqa: PLC0415

    _first, second, manager, _failing = two_watched_files
    ten_years = 10 * 365 * 24 * 3600
    await _import_then_leave_only(
        hass,
        configured_accounts,
        manager,
        _third_watched_path(tmp_path),
        int(time.time()) + ten_years,
    )

    _write_bundle(second, "N1N1", int(time.time()) + 60, token=_NEWEST_TOKEN)
    await _scan(hass, manager)

    assert len(_cards(hass, _ACCOUNT_A)) == 1


@pytest.mark.asyncio
async def test_a_turned_away_bundle_is_not_dispatched_again_on_every_scan(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One turn-away per bundle, not one per scan."""

    _first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _write_bundle(second, "B1B1", 2_000, token=_BETWEEN_TOKEN)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)
    third.unlink()

    caplog.set_level(logging.INFO, logger="custom_components.googlefindmy.config_flow")
    for _ in range(3):
        await _scan(hass, manager)

    turned_away = [
        record
        for record in caplog.records
        if "is not newer than the one the entry imported" in record.getMessage()
    ]
    assert _cards(hass, _ACCOUNT_A) == []
    assert len(turned_away) == 1


def test_the_watcher_does_not_retry_a_turned_away_bundle() -> None:
    """The harness above reports every dispatch as accepted; the real one classifies.

    A reason the verdict table does not know counts as transient, and the
    watcher would start the same bundle again on its next scan.
    """

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    verdict = discovery._classify_discovery_flow_result(
        {"type": "abort", "reason": "bundle_older_than_imported"}
    )

    assert verdict is discovery.CloudDiscoveryOutcome.ACCEPTED


# --- Every import path records the state ---------------------------------------


def _recorded_state(entry: Any) -> tuple[Any, Any]:
    from custom_components.googlefindmy.const import (  # noqa: PLC0415
        DATA_DISCOVERY_IMPORTED_DIGEST,
        DATA_DISCOVERY_IMPORTED_MTIME,
    )

    return (
        entry.data.get(DATA_DISCOVERY_IMPORTED_DIGEST),
        entry.data.get(DATA_DISCOVERY_IMPORTED_MTIME),
    )


@pytest.mark.asyncio
async def test_accepting_the_overwrite_records_the_written_bundle(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """The overwrite records the bundle it wrote, the newest copy's time."""

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    configured_accounts.add(_ACCOUNT_A)
    entry = _add_configured_entry(hass, _ACCOUNT_A)
    first, second, manager, _failing = two_watched_files
    card = await _card_for_older_bundle(hass, first, manager)
    await hass.config_entries.flow.async_configure(card, {})
    _write_bundle(second, "Y1Y1", 2_000, token=_YOUNGER_TOKEN)

    result = await hass.config_entries.flow.async_configure(
        card, {"overwrite_credentials": True}
    )

    assert result["reason"] == "credentials_updated"
    written = discovery.read_secrets_bundle(second)
    assert written is not None
    assert _recorded_state(entry) == (written.digest, 2_000.0)


@pytest.mark.asyncio
async def test_a_discovery_update_records_the_bundle_time_of_its_payload(
    hass: HomeAssistant,
    configured_accounts: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a watcher the payload's own bundle time is recorded."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    _stub_entry_creation(hass, monkeypatch)
    # The update path reloads the entry itself; the real reload would set the
    # integration and its dependencies up, which is not what this test is about.
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    configured_accounts.add(_ACCOUNT_A)
    entry = _add_configured_entry(hass, _ACCOUNT_A)
    payload = {
        **_payload(_ACCOUNT_A, shared_key="U1U1"),
        "discovery_bundle_mtime": 2_500.0,
    }

    await hass.config_entries.flow.async_init(
        config_flow.DOMAIN,
        context={"source": config_flow.DISCOVERY_UPDATE_SOURCE},
        data=payload,
    )
    await hass.async_block_till_done()

    digest, mtime = _recorded_state(entry)
    assert isinstance(digest, str)
    assert mtime == 2_500.0


# --- What the hook logs is what the next scan does; what stays offered --------


@pytest.mark.asyncio
async def test_a_bundle_newer_than_every_copy_is_announced_and_offered(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hook's "offered as a new discovery card" holds for a newer bundle."""

    _first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)
    _write_bundle(second, "N1N1", 4_000, token=_NEWEST_TOKEN)

    caplog.set_level(logging.INFO, logger="custom_components.googlefindmy.config_flow")
    await _run_staged_cleanup(hass)
    (kept,) = [r.getMessage() for r in caplog.records if str(second) in r.getMessage()]
    assert "it is offered as a new discovery card" in kept

    await _scan(hass, manager)
    assert len(_cards(hass, _ACCOUNT_A)) == 1


@pytest.mark.asyncio
async def test_without_a_copy_of_the_import_the_hook_keeps_and_the_state_decides(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No copy of the import left: nothing is provably stale, the entry decides.

    The copy was removed by someone else before the hook ran. The hook keeps the
    older bundle, as it always did, and says the recorded state decides; that
    state then turns its card away.
    """

    _first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _write_bundle(second, "B1B1", 2_000, token=_BETWEEN_TOKEN)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)
    third.unlink()

    caplog.set_level(logging.INFO, logger="custom_components.googlefindmy.config_flow")
    await _run_staged_cleanup(hass)
    assert second.exists()
    (kept,) = [r.getMessage() for r in caplog.records if str(second) in r.getMessage()]
    assert "only if it is newer than the import the entry recorded" in kept

    await _scan(hass, manager)
    assert _cards(hass, _ACCOUNT_A) == []


@pytest.mark.asyncio
async def test_after_a_restart_the_imported_content_is_still_offered(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same content is not turned away, however old its file.

    After a restart the watcher rediscovers the unchanged bundle; that card is
    what redeems the cleanup ticket of an overwrite (see
    ``async_step_discovery_overwrite``), so turning it away would leave the
    file on disk for good. The copy left behind carries an older time than the
    one recorded, which is exactly what the comparison must not look at.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _write_bundle(second, "I1I1", 1_000, token=_IMPORTED_TOKEN)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)
    third.unlink()

    await manager.async_stop()
    hass.data[config_flow.DOMAIN].clear()
    restarted = await _start_watcher(hass, monkeypatch, [first, second, third])
    try:
        await _scan(hass, restarted)
        assert len(_cards(hass, _ACCOUNT_A)) == 1
    finally:
        await restarted.async_stop()


@pytest.mark.asyncio
async def test_a_card_without_a_bundle_time_is_offered_and_says_so(
    hass: HomeAssistant,
    configured_accounts: set[str],
    two_watched_files: tuple[Path, Path, Any, set[str]],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a time there is nothing to compare; the card stays, with a log line."""

    _first, _second, manager, _failing = two_watched_files
    third = _third_watched_path(tmp_path)
    _write_bundle(third, "I1I1", 3_000, token=_IMPORTED_TOKEN)
    await _import_newest(hass, configured_accounts, manager)

    caplog.set_level(logging.INFO, logger="custom_components.googlefindmy.config_flow")
    await _discover(hass, _payload(_ACCOUNT_A, shared_key="T1T1"))

    assert len(_cards(hass, _ACCOUNT_A)) == 1
    assert any("carries no bundle time" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_no_state_is_recorded_without_imported_content() -> None:
    """No payload, or a payload without a bundle: nothing identifies the import."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    without_bundle = config_flow.CloudDiscoveryData(
        email=_ACCOUNT_A,
        unique_id=config_flow.unique_account_id(_ACCOUNT_A),
        candidates=(("candidate", _TOKEN),),
        secrets_bundle=None,
        bundle_mtime=3_000.0,
    )

    assert await config_flow._async_discovery_import_state(None, None) == {}
    assert await config_flow._async_discovery_import_state(None, without_bundle) == {}


@pytest.mark.asyncio
async def test_a_newer_bundle_of_another_account_is_not_newer_for_this_one(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """The watched scan is filtered by account before anything is compared.

    Account B's bundle is the newest file on disk. For account A it is neither
    a newer bundle to swap in at confirmation nor a copy whose time the import
    state records.
    """

    from custom_components.googlefindmy import config_flow, discovery  # noqa: PLC0415

    first, second, _manager, _failing = two_watched_files
    _write_bundle(first, "A1A1", 1_000)
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text(
        json.dumps(
            {"google_email": _ACCOUNT_B, "aas_token": _TOKEN, "shared_key": "B9B9"}
        ),
        encoding="utf-8",
    )
    os.utime(second, (2_000, 2_000))
    ((_path, own),) = discovery.scan_secrets_bundles([first])

    fresher = await config_flow._async_freshest_watched_bundle(
        hass, stable_key=own.stable_key, digest=own.digest
    )
    imported = config_flow.CloudDiscoveryData(
        email=_ACCOUNT_A,
        unique_id=config_flow.unique_account_id(_ACCOUNT_A),
        candidates=(("candidate", _TOKEN),),
        secrets_bundle=json.loads(first.read_text(encoding="utf-8")),
        bundle_mtime=500.0,
    )
    state = await config_flow._async_discovery_import_state(hass, imported)

    assert fresher is None
    assert state[config_flow.DATA_DISCOVERY_IMPORTED_MTIME] == 1_000


@pytest.mark.asyncio
async def test_the_import_state_records_the_imported_content_not_a_newer_one(
    hass: HomeAssistant,
    two_watched_files: tuple[Path, Path, Any, set[str]],
) -> None:
    """Only copies of the imported content set the recorded time.

    A newer bundle of the same account with other content is still on disk. Had
    its time been recorded, its own card would later count as a tie and be
    turned away.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    first, second, _manager, _failing = two_watched_files
    _write_bundle(first, "A1A1", 1_000)
    _write_bundle(second, "A2A2", 2_000, token=_YOUNGER_TOKEN)
    imported = config_flow.CloudDiscoveryData(
        email=_ACCOUNT_A,
        unique_id=config_flow.unique_account_id(_ACCOUNT_A),
        candidates=(("candidate", _TOKEN),),
        secrets_bundle=json.loads(first.read_text(encoding="utf-8")),
        bundle_mtime=500.0,
    )

    state = await config_flow._async_discovery_import_state(hass, imported)

    assert state[config_flow.DATA_DISCOVERY_IMPORTED_MTIME] == 1_000
