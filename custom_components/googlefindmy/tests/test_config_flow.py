# tests/test_config_flow.py
"""Tests for the Google Find My Device config flow.

This suite exercises the primary paths and edge cases for the custom
integration's configuration and options flows, including:

- Initial setup:
  * Secrets-only path (valid JSON and token fallbacks)
  * Manual-only path (field-level validation)
  * Online validation during device selection
  * Duplicate prevention via unique_id
- Reauthentication and credentials update:
  * Exactly-one-method checks (choose_one)
  * JSON syntax vs. content/format errors
  * Online validation via API call
- Options flow:
  * Credentials update success/fail paths
  * Visibility management (restore ignored devices)
- Error handling:
  * cannot_connect on API errors
  * no_devices when API returns none
"""

from __future__ import annotations

import json
from typing import Any, Dict
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

# Keep tests resilient: only use stable string keys where practical;
# otherwise import integration constants directly.
DOMAIN = "googlefindmy"

# Option keys (mirroring integration consts but decoupled for test stability)
OPT_TRACKED_DEVICES = "tracked_devices"
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


def _options_payload_defaults(dev_ids: list[str]) -> Dict[str, Any]:
    """Build a valid options payload for the device_selection step."""
    return {
        OPT_TRACKED_DEVICES: dev_ids,
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
    """Secrets-only: valid JSON and credentials flow through to create_entry."""
    secrets = {
        "username": "user@example.com",
        "oauth_token": "x" * 32,
    }

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        # Start the user flow
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert step["type"] == "form" and step["step_id"] == "user"

        # Choose the secrets_json method
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
        assert step["type"] == "form" and step["step_id"] == "secrets_json"

        # Submit secrets.json to advance to device_selection
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        # Submit the device selection/options → create_entry
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _options_payload_defaults(["dev1"])
        )
        assert step["type"] == "create_entry"
        assert step["title"] == "Google Find My Device"
        assert step["data"][CONF_GOOGLE_EMAIL] == "user@example.com"
        assert step["data"][CONF_OAUTH_TOKEN] == "x" * 32

        # Unique ID must be set to avoid duplicates
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.unique_id == f"{DOMAIN}:user@example.com"


@pytest.mark.asyncio
async def test_user_flow_manual_only_success(hass: HomeAssistant) -> None:
    """Manual-only: valid token+email advances to device_selection then creates entry."""
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

        # Provide manual credentials (field-level validation applies in this step)
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"],
            {CONF_OAUTH_TOKEN: "t" * 32, CONF_GOOGLE_EMAIL: "user@example.com"},
        )
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _options_payload_defaults(["dev1"])
        )
        assert step["type"] == "create_entry"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.data[CONF_OAUTH_TOKEN] == "t" * 32
        assert entry.data[CONF_GOOGLE_EMAIL] == "user@example.com"


@pytest.mark.asyncio
async def test_user_flow_secrets_only_missing_token_invalid_token(hass: HomeAssistant) -> None:
    """Secrets-only: missing token in JSON yields base error invalid_token."""
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
async def test_secrets_failover_access_token(hass: HomeAssistant) -> None:
    """Secrets-only: if oauth_token is missing, fall back to access_token."""
    secrets = {"username": "user@example.com", "access_token": "A" * 64}

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _options_payload_defaults(["dev1"])
        )
        assert step["type"] == "create_entry"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.data[CONF_OAUTH_TOKEN] == "A" * 64


@pytest.mark.asyncio
async def test_secrets_failover_aas_token(hass: HomeAssistant) -> None:
    """Secrets-only: if oauth/access token missing, accept aas_token."""
    secrets = {"username": "user@example.com", "aas_token": "aas_et/" + "B" * 64}

    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"secrets_json": json.dumps(secrets)}
        )
        assert step["type"] == "form" and step["step_id"] == "device_selection"

        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], _options_payload_defaults(["dev1"])
        )
        assert step["type"] == "create_entry"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert entry.data[CONF_OAUTH_TOKEN].startswith("aas_et/")


@pytest.mark.asyncio
async def test_secrets_only_invalid_json(hass: HomeAssistant) -> None:
    """Secrets-only: invalid JSON should flag the secrets field with invalid_json."""
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
    assert step["errors"]["secrets_json"] == "invalid_json"


@pytest.mark.asyncio
async def test_manual_field_level_errors(hass: HomeAssistant) -> None:
    """Manual: empty/invalid fields result in field-level errors (required/invalid_token)."""
    step = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    step = await hass.config_entries.flow.async_configure(
        step["flow_id"], {"auth_method": "individual_tokens"}
    )

    # Case 1: both empty → both required
    step1 = await hass.config_entries.flow.async_configure(
        step["flow_id"], {CONF_OAUTH_TOKEN: "", CONF_GOOGLE_EMAIL: ""}
    )
    assert step1["type"] == "form"
    assert step1["errors"][CONF_OAUTH_TOKEN] == "required"
    assert step1["errors"][CONF_GOOGLE_EMAIL] == "required"

    # Case 2: invalid token + invalid email
    step2 = await hass.config_entries.flow.async_configure(
        step["flow_id"], {CONF_OAUTH_TOKEN: "short", CONF_GOOGLE_EMAIL: "no-at"}
    )
    assert step2["type"] == "form"
    assert step2["errors"][CONF_OAUTH_TOKEN] == "invalid_token"
    assert step2["errors"][CONF_GOOGLE_EMAIL] == "invalid_token"


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
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
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
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
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

    # Start another flow with the same user via secrets path
    secrets = {"username": "user@example.com", "oauth_token": "N" * 32}
    with patch(
        "custom_components.googlefindmy.config_flow.GoogleFindMyAPI.async_get_basic_device_list",
        new=AsyncMock(return_value=_device_list()),
    ):
        step = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"auth_method": "secrets_json"}
        )
        # Submitting secrets should trigger unique_id set and immediate abort
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

        # Reauth ends with abort(reason="reauth_successful")
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
        options=_options_payload_defaults([]),
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
        step = await hass.config_entries.options.async_configure(
            step["flow_id"], "credentials"
        )
        assert step["type"] == "form" and step["step_id"] == "credentials"

        # Submit new manual credentials
        step = await hass.config_entries.options.async_configure(
            step["flow_id"],
            {"new_oauth_token": "Z" * 40, "new_google_email": "new@example.com"},
        )
        assert step["type"] == "abort" and step["reason"] == "reconfigure_successful"

        updated = hass.config_entries.async_get_entry(entry.entry_id)
        assert updated is not None
        assert updated.data[CONF_GOOGLE_EMAIL] == "new@example.com"
        assert updated.data[CONF_OAUTH_TOKEN] == "Z" * 40


@pytest.mark.asyncio
async def test_options_credentials_update_invalid_json(hass: HomeAssistant) -> None:
    """Options flow: invalid secrets JSON should produce invalid_json error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
        options=_options_payload_defaults([]),
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], "credentials"
    )
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], {"new_secrets_json": "{not json"}
    )
    assert step["type"] == "form"
    # In config_flow, invalid_json is raised on the form base (or field).
    # This test expects field-level error on secrets if implemented that way;
    # otherwise, accept base-level assertion. Prefer the field if present.
    assert step["errors"].get("new_secrets_json", step["errors"].get("base")) == "invalid_json"


@pytest.mark.asyncio
async def test_options_credentials_update_choose_one(hass: HomeAssistant) -> None:
    """Options flow: mixing secrets and manual fields should yield choose_one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "old@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:old@example.com",
        title="Google Find My Device",
        options=_options_payload_defaults([]),
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], "credentials"
    )
    step = await hass.config_entries.options.async_configure(
        step["flow_id"],
        {
            "new_secrets_json": json.dumps(
                {"username": "x@y", "oauth_token": "X" * 32}
            ),
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
        options={
            **_options_payload_defaults([]),
            OPT_IGNORED_DEVICES: ["devA", "devB", "devC"],
        },
    )
    entry.add_to_hass(hass)

    # Open options menu
    step = await hass.config_entries.options.async_init(entry.entry_id)
    assert step["type"] == "menu" and "visibility" in step["menu_options"]

    # Go to visibility form
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], "visibility"
    )
    assert step["type"] == "form" and step["step_id"] == "visibility"

    # Restore two devices; expect new options with only the remaining ignored device
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], {"unignore_devices": ["devA", "devB"]}
    )
    assert step["type"] == "create_entry"
    data = step["data"]
    assert data[OPT_IGNORED_DEVICES] == ["devC"]


@pytest.mark.asyncio
async def test_options_visibility_no_ignored_devices_abort(hass: HomeAssistant) -> None:
    """Options flow: visibility aborts when there are no ignored devices to restore."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "user@example.com", CONF_OAUTH_TOKEN: "O" * 32},
        unique_id=f"{DOMAIN}:user@example.com",
        title="Google Find My Device",
        options=_options_payload_defaults([]),
    )
    entry.add_to_hass(hass)

    step = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], "visibility"
    )
    assert step["type"] == "abort"
    assert step["reason"] == "no_ignored_devices"
