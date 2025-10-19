# tests/test_config_flow.py
"""Tests for the Google Find My Device config/option flows (Platinum-ready).

Covers:
- Initial setup:
  * Secrets-only path (JSON parsing, token candidate discovery, online validation)
  * Manual-only path (format checks, online validation during device selection)
  * Duplicate prevention via unique_id
- Token failover:
  * Preferred order and try-next-on-failure semantics
- Reauth and options credentials:
  * Exactly-one-method enforcement (choose_one)
  * JSON vs. format errors (invalid_json / invalid_token)
  * Online validation before storing
- Options flow:
  * Settings update (no tracked_devices UI anymore)
  * Credentials update (manual + secrets)
  * Visibility management (restore ignored devices)
- Error handling:
  * cannot_connect on API/network errors
  * no_devices when API returns none
"""
from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

# Prefer the core MockConfigEntry when running in HA-Core; fall back to the
# pytest plugin for custom components when running outside.
try:  # Home Assistant core test environment
    from tests.common import MockConfigEntry  # type: ignore
except Exception:  # pytest-homeassistant-custom-component environment
    from pytest_homeassistant_custom_component.common import (  # type: ignore
        MockConfigEntry,
    )

DOMAIN = "googlefindmy"

# Option keys (keep string-literals here for test stability)
OPT_LOCATION_POLL_INTERVAL = "location_poll_interval"
OPT_DEVICE_POLL_DELAY = "device_poll_delay"
OPT_MIN_ACCURACY_THRESHOLD = "min_accuracy_threshold"
OPT_MOVEMENT_THRESHOLD = "movement_threshold"
OPT_GOOGLE_HOME_FILTER_ENABLED = "google_home_filter_enabled"
OPT_GOOGLE_HOME_FILTER_KEYWORDS = "google_home_filter_keywords"
OPT_ENABLE_STATS_ENTITIES = "enable_stats_entities"
OPT_MAP_VIEW_TOKEN_EXPIRATION = "map_view_token_expiration"

CONF_OAUTH_TOKEN = "oauth_token"
CONF_GOOGLE_EMAIL = "google_email"

# Import ignored-devices option name from the integration to stay exact
from custom_components.googlefindmy.const import OPT_IGNORED_DEVICES  # noqa: E402


def _device_list() -> list[Dict[str, Any]]:
    """Return a minimal device list payload like the API would."""
    return [{"name": "Pixel 8", "id": "dev1"}]


def _device_selection_options_payload() -> Dict[str, Any]:
    """Build a valid options payload for the device_selection step (no tracked_devices)."""
    return {
        OPT_LOCATION_POLL_INTERVAL: 120,
        OPT_DEVICE_POLL_DELAY: 2,
        OPT_MIN_ACCURACY_THRESHOLD: 50,
        OPT_MOVEMENT_THRESHOLD: 20,
        OPT_GOOGLE_HOME_FILTER_ENABLED: False,
        OPT_GOOGLE_HOME_FILTER_KEYWORDS: "",
        OPT_ENABLE_STATS_ENTITIES: False,
        OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
    }


@pytest.mark.asyncio
async def test_user_flow_secrets_only_success(hass: HomeAssistant) -> None:
    """Secrets-only: valid JSON leads to device selection and create_entry."""
    secrets = {"username": "user@example.com", "oauth_token": "x" * 32}

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert step["type"] == "form" and step["step_id"] == "user"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
        assert step["type"] == "form" and step["step_id"] == "secrets_json"

        # Submit secrets.json -> online validation succeeds -> device selection
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        # Finish setup (no tracked_devices in payload)
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _device_selection_options_payload()
        )
        assert step["type"] == "create_entry"
        assert step["title"] == "Google Find My Device"
        assert step["data"][CONF_GOOGLE_EMAIL] == "user@example.com"
        assert step["data"][CONF_OAUTH_TOKEN] == "x" * 32

        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == f"{DOMAIN}:user@example.com"


@pytest.mark.asyncio
async def test_user_flow_manual_only_success(hass: HomeAssistant) -> None:
    """Manual-only: valid token+email advances to device_selection and creates entry."""
    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "individual_tokens"}
        )
        assert step["type"] == "form" and step["step_id"] == "individual_tokens"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {CONF_OAUTH_TOKEN: "t" * 32, CONF_GOOGLE_EMAIL: "user@example.com"}
        )
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _device_selection_options_payload()
        )
        assert step["type"] == "create_entry"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.data[CONF_OAUTH_TOKEN] == "t" * 32
        assert entry.data[CONF_GOOGLE_EMAIL] == "user@example.com"


@pytest.mark.asyncio
async def test_user_flow_secrets_only_missing_token_invalid_token(hass: HomeAssistant) -> None:
    """Secrets-only: missing token in JSON yields base error invalid_token (no candidates)."""
    secrets = {"username": "user@example.com"}  # no oauth/access/aas token

    step = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    step = await hass.config_entries.flow.async_configure(
        step["flow_id"], {"auth_method": "secrets_json"}
    )
    step = await hass.config_entries.flow.async_configure(
        step["flow_id"], {"secrets_json": json.dumps(secrets)}
    )
    assert step["type"] == "form"
    assert step["step_id"] == "secrets_json"
    assert step["errors"]["base"] == "invalid_token"


@pytest.mark.asyncio
async def test_secrets_only_invalid_json(hass: HomeAssistant) -> None:
    """Secrets-only: invalid JSON flags invalid_json (field or base accepted)."""
    step = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    step = await hass.config_entries.flow.async_configure(
        step["flow_id"], {"auth_method": "secrets_json"}
    )
    step = await hass.config_entries.flow.async_configure(
        step["flow_id"], {"secrets_json": "{not json"}
    )
    assert step["type"] == "form"
    # Accept either field-level or base-level, depending on implementation detail
    assert step["errors"].get("secrets_json", step["errors"].get("base")) == "invalid_json"


@pytest.mark.asyncio
async def test_token_failover_prefers_first_working_candidate(hass: HomeAssistant) -> None:
    """Failover: if first candidate fails online validation, try next in order."""
    # Provide aas_token (bad), installation.token (good)
    secrets = {
        "username": "user@example.com",
        "aas_token": "aas_et/FAILFIRST",
        "fcm_credentials": {"installation": {"token": "GOODTOKEN"}},
    }

    # Build a fake API that fails for 'FAILFIRST' but succeeds for 'GOODTOKEN'
    class _FakeAPI:
        def __init__(self, *, oauth_token: str, google_email: str, **_: Any) -> None:
            self.token = oauth_token
            self.email = google_email

        async def async_get_basic_device_list(self, _: str) -> list[Dict[str, Any]]:
            if self.token == "aas_et/FAILFIRST":
                raise Exception("unauthorized")
            return _device_list()

    with patch("custom_components.googlefindmy.config_flow.GoogleFindMyAPI", _FakeAPI):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(step["flow_id"], {"auth_method": "secrets_json"})
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        # We should reach device_selection because second candidate worked
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _device_selection_options_payload()
        )
        assert step["type"] == "create_entry"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        # Stored token must be the working one
        assert entry.data[CONF_OAUTH_TOKEN] == "GOODTOKEN"


@pytest.mark.asyncio
async def test_device_selection_no_devices(hass: HomeAssistant) -> None:
    """If API returns an empty list, device_selection shows base error no_devices."""
    secrets = {"username": "user@example.com", "oauth_token": "x" * 32}

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=[]),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(step["flow_id"], {"auth_method": "secrets_json"})
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "form"
        assert step["step_id"] == "device_selection"
        assert step["errors"]["base"] == "no_devices"


@pytest.mark.asyncio
async def test_device_selection_cannot_connect(hass: HomeAssistant) -> None:
    """If API raises during device fetch, device_selection shows base error cannot_connect."""
    secrets = {"username": "user@example.com", "oauth_token": "x" * 32}

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(side_effect=Exception("boom")),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(step["flow_id"], {"auth_method": "secrets_json"})
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "form"
        assert step["step_id"] == "device_selection"
        assert step["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_unique_id_prevents_duplicate_setup(hass: HomeAssistant) -> None:
    """Second setup with the same email should abort as already_configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "user@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:user@example.com",
        title="Google Find My Device",
    )
    existing.add_to_hass(hass)

    secrets = {"username": "user@example.com", "oauth_token": "N" * 32}
    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(step["flow_id"], {"auth_method": "secrets_json"})
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "abort"
        assert step["reason"] == "already_configured"


# -------------------------
# Reauthentication (reauth)
# -------------------------


@pytest.mark.asyncio
async def test_reauth_secrets_success(hass: HomeAssistant) -> None:
    """Reauth: secrets-only validation succeeds and updates the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert step["type"] == "form" and step["step_id"] == "reauth_confirm"

        new_secrets = {"username": "user@example.com", "oauth_token": "N" * 48}
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(new_secrets)}
        )
        assert step["type"] == "abort" and step["reason"] == "reauth_successful"

        updated = hass.config_entries.async_get_entry(entry.entry_id)
        assert updated is not None
        assert updated.data[CONF_GOOGLE_EMAIL] == "user@example.com"
        assert updated.data[CONF_OAUTH_TOKEN] == "N" * 48


@pytest.mark.asyncio
async def test_reauth_partial_manual_choose_one(hass: HomeAssistant) -> None:
    """Reauth: providing only token or only email should yield base choose_one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert step["type"] == "form" and step["step_id"] == "reauth_confirm"

    # Only token, no email → choose_one (incomplete/manual)
    step = await hass.config_entries.flow.async_configure(
        step["flow_id"], {CONF_OAUTH_TOKEN: "Z" * 40}
    )
    assert step["type"] == "form"
    assert step["errors"]["base"] == "choose_one"


@pytest.mark.asyncio
async def test_reauth_mixed_input_choose_one(hass: HomeAssistant) -> None:
    """Reauth: mixing secrets_json and manual fields should yield base choose_one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert step["type"] == "form" and step["step_id"] == "reauth_confirm"

    step = await hass.config_entries.flow.async_configure(
        step["flow_id"],
        {
            "secrets_json": json.dumps({"username": "x@y", "oauth_token": "X" * 32}),
            CONF_OAUTH_TOKEN: "Y" * 32,
            CONF_GOOGLE_EMAIL: "user@example.com",
        },
    )
    assert step["type"] == "form"
    assert step["errors"]["base"] == "choose_one"


# -------------------------
# Options flow (post-setup)
# -------------------------


@pytest.mark.asyncio
async def test_options_credentials_update_manual_success(hass: HomeAssistant) -> None:
    """Options flow: manual credentials update stores new values and reloads."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
        options=_device_selection_options_payload(),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        # Enter options menu
        step = await hass.config_entries.options.async_init(entry.entry_id)
        assert step["type"] == "menu" and "credentials" in step["menu_options"]

        # Navigate to credentials step
        step = await hass.config_entries.options.async_configure(step["flow_id"], "credentials")
        assert step["type"] == "form" and step["step_id"] == "credentials"

        # Submit new manual credentials
        step = await hass.config_entries.options.async_configure(
            step["flow_id"], {"new_oauth_token": "Z" * 40, "new_google_email": "new@example.com"}
        )
        assert step["type"] == "abort" and step["reason"] == "reconfigure_successful"

        updated = hass.config_entries.async_get_entry(entry.entry_id)
        assert updated is not None
        assert updated.data[CONF_GOOGLE_EMAIL] == "new@example.com"
        assert updated.data[CONF_OAUTH_TOKEN] == "Z" * 40


@pytest.mark.asyncio
async def test_options_credentials_update_invalid_json(hass: HomeAssistant) -> None:
    """Options flow: invalid secrets JSON should produce invalid_json (field or base)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
        options=_device_selection_options_payload(),
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(step["flow_id"], "credentials")
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], {"new_secrets_json": "{not json"}
    )
    assert step["type"] == "form"
    assert step["errors"].get("new_secrets_json", step["errors"].get("base")) == "invalid_json"


@pytest.mark.asyncio
async def test_options_credentials_update_choose_one(hass: HomeAssistant) -> None:
    """Options: mixing secrets and manual fields should yield choose_one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
        options=_device_selection_options_payload(),
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(step["flow_id"], "credentials")
    step = await hass.config_entries.options.async_configure(
        step["flow_id"],
        {
            "new_secrets_json": json.dumps({"username": "x@y", "oauth_token": "X" * 32}),
            "new_oauth_token": "Y" * 32,
            "new_google_email": "user@example.com",
        },
    )
    assert step["type"] == "form"
    assert step["errors"]["base"] == "choose_one"


@pytest.mark.asyncio
async def test_options_visibility_restore_devices_success(hass: HomeAssistant) -> None:
    """Options flow: visibility step should restore selected ignored devices."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "user@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:user@example.com",
        title="Google Find My Device",
        # Old list form is accepted and migrated internally to a mapping
        options={**_device_selection_options_payload(), OPT_IGNORED_DEVICES: ["devA", "devB", "devC"]},
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    assert step["type"] == "menu" and "visibility" in step["menu_options"]

    step = await hass.config_entries.options.async_configure(step["flow_id"], "visibility")
    assert step["type"] == "form" and step["step_id"] == "visibility"

    step = await hass.config_entries.options.async_configure(
        step["flow_id"], {"unignore_devices": ["devA", "devB"]}
    )
    assert step["type"] == "create_entry"
    data = step["data"]
    # New format is a mapping; ensure only devC remains
    assert isinstance(data[OPT_IGNORED_DEVICES], dict)
    assert "devC" in data[OPT_IGNORED_DEVICES]
    assert "devA" not in data[OPT_IGNORED_DEVICES]
    assert "devB" not in data[OPT_IGNORED_DEVICES]


@pytest.mark.asyncio
async def test_options_visibility_no_ignored_devices_abort(hass: HomeAssistant) -> None:
    """Options flow: visibility aborts when there are no ignored devices to restore."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "user@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:user@example.com",
        title="Google Find My Device",
        options=_device_selection_options_payload(),
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(step["flow_id"], "visibility")
    assert step["type"] == "abort"
    assert step["reason"] == "no_ignored_devices"
