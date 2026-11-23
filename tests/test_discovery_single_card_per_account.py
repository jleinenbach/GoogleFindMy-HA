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
  stubbed ``homeassistant.loader`` behind. Core's handler lookup
  (``_async_get_flow_handler``) is the only part of ``async_init`` that touches
  the loader and the handler registry, and which module instance it sees depends
  on which tests ran earlier in the same worker. The fixture replaces exactly
  that lookup, in the module the manager itself runs from, with one that returns
  this integration's ``ConfigFlow``. Everything after it -- flow progress,
  aborts, ``async_configure`` -- stays Home Assistant's own code.
* ``_ingest_discovery_credentials`` is replaced: it validates tokens against
  Google, which is not what these tests are about. Payload normalisation and the
  single-key gate stay real.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

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
