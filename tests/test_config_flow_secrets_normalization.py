# tests/test_config_flow_secrets_normalization.py
"""Tests for whitespace normalization of pasted secrets.json bundles.

Copy/paste of a ``secrets.json`` into the config flow can inject stray
whitespace into credential values. A single space inside ``owner_key`` breaks
AES-GCM owner-key decryption ("Owner key decryption failed"), and the user had
to delete that space manually. ``normalize_secrets_bundle`` removes whitespace
from credential values while preserving interior spaces in free-text fields.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
)
from custom_components.googlefindmy.shared_helpers import normalize_secrets_bundle
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
    set_config_flow_unique_id,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AABBCC", "AABBCC"),  # already clean -> unchanged
        (" AABBCC", "AABBCC"),  # leading space
        ("AABBCC ", "AABBCC"),  # trailing space
        ("AA BB CC", "AABBCC"),  # interior spaces (wrapped paste)
        ("AA\nBB\tCC", "AABBCC"),  # interior newline/tab
        ("  AA BB  ", "AABB"),  # combined leading/trailing/interior
    ],
)
def test_credential_value_all_whitespace_removed(raw: str, expected: str) -> None:
    """Whitelisted credential keys lose ALL whitespace, including interior."""
    assert normalize_secrets_bundle({"owner_key": raw})["owner_key"] == expected
    assert normalize_secrets_bundle({"shared_key": raw})["shared_key"] == expected


def test_non_credential_value_only_edge_trimmed() -> None:
    """Free-text fields keep interior spaces and are only edge-trimmed."""
    result = normalize_secrets_bundle(
        {"username": "  Jane Doe  ", "Email": " a b@x.io "}
    )
    assert result["username"] == "Jane Doe"
    assert result["Email"] == "a b@x.io"


def test_nested_fcm_token_normalized() -> None:
    """Whitelisted keys nested under fcm_credentials are normalized too."""
    bundle = {
        "fcm_credentials": {
            "installation": {"token": " inst alled "},
            "fcm": {"registration": {"token": "reg\nistration"}},
        }
    }
    result = normalize_secrets_bundle(bundle)
    assert result["fcm_credentials"]["installation"]["token"] == "installed"
    assert result["fcm_credentials"]["fcm"]["registration"]["token"] == "registration"


def test_nested_gcm_identity_normalized() -> None:
    """The numeric GCM identity (android_id / security_token under
    fcm_credentials.gcm) loses ALL whitespace: both are fed to int() on the FCM
    login path, so an interior space from a wrapped paste would break push
    registration (Codex finding on PR #182)."""
    bundle = {
        "fcm_credentials": {
            "gcm": {
                "android_id": "1234 5678 9012 3456",
                "security_token": " 9876\n543210 ",
                "app_id": "app-1",
            }
        }
    }
    result = normalize_secrets_bundle(bundle)
    gcm = result["fcm_credentials"]["gcm"]
    assert gcm["android_id"] == "1234567890123456"
    assert gcm["security_token"] == "9876543210"
    # app_id is a structured identifier (not numeric/token credential material)
    # and is not in the whitelist: only edge-trimmed, interior preserved.
    assert gcm["app_id"] == "app-1"


def test_idempotent() -> None:
    """Applying the normalization twice yields the same result."""
    bundle = {
        "owner_key": " AA BB ",
        "username": "  Jane Doe  ",
        "fcm_credentials": {"installation": {"token": " t ok "}},
    }
    once = normalize_secrets_bundle(bundle)
    assert normalize_secrets_bundle(once) == once


def test_input_not_mutated() -> None:
    """The input mapping is never mutated (safe for MappingProxyType)."""
    bundle = {"owner_key": " AA BB ", "nested": {"aas_token": " x y "}}
    normalize_secrets_bundle(bundle)
    assert bundle["owner_key"] == " AA BB "
    assert bundle["nested"]["aas_token"] == " x y "


def test_list_values_recursed() -> None:
    """Lists are walked element-wise, dicts inside them are normalized."""
    bundle = {"items": [{"token": " a b "}, {"username": "  keep me  "}]}
    result = normalize_secrets_bundle(bundle)
    assert result["items"][0]["token"] == "ab"
    assert result["items"][1]["username"] == "keep me"


def test_non_string_scalars_unchanged() -> None:
    """Non-string scalar values pass through unchanged."""
    bundle = {"count": 3, "enabled": True, "missing": None, "owner_key": " A B "}
    result = normalize_secrets_bundle(bundle)
    assert result["count"] == 3
    assert result["enabled"] is True
    assert result["missing"] is None
    assert result["owner_key"] == "AB"


def test_non_mapping_input_returned_unchanged() -> None:
    """A non-mapping top-level value is returned unchanged."""
    assert normalize_secrets_bundle("plain") == "plain"
    assert normalize_secrets_bundle(None) is None


@pytest.mark.asyncio
async def test_config_flow_round_trip_strips_owner_key_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a pasted owner_key with a stray space is persisted clean.

    This is the regression guard for the reported bug: the user pasted a valid
    secrets.json but a copy/paste space inside owner_key broke decryption. The
    persisted secrets bundle must contain the whitespace-free owner_key.
    """
    secrets_payload = {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "owner_key": "AA BB\nCC ",
        "shared_key": " DD EE ",
        "username": "  Keep Me  ",
    }

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        # The bundle handed downstream is already normalized.
        assert secrets_bundle is not None
        assert secrets_bundle["owner_key"] == "AABBCC"
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    captured: dict[str, Any] = {}

    async def _create_entry(
        *,
        title: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured["result"] = {"title": title, "data": data, "options": options}
        return {"type": "create_entry", "title": title, "data": data}

    hass = _FlowHass()
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
    flow.async_create_entry = _create_entry  # type: ignore[assignment]

    first = await flow.async_step_secrets_json(
        {"secrets_json": json.dumps(secrets_payload)}
    )
    if inspect.isawaitable(first):
        first = await first
    assert isinstance(first, dict)
    assert first.get("type") == "form"

    final = await flow.async_step_device_selection({})
    if inspect.isawaitable(final):
        final = await final
    assert isinstance(final, dict)
    assert final.get("type") == "create_entry"

    payload = captured.get("result")
    assert payload, "Expected config entry payload to be captured"
    data = payload["data"]
    bundle = data[DATA_SECRET_BUNDLE]
    assert bundle["owner_key"] == "AABBCC"
    assert bundle["shared_key"] == "DDEE"
    # Free-text field keeps interior spaces, only edge-trimmed.
    assert bundle["username"] == "Keep Me"
    # Token persisted from the (clean) aas_token.
    assert data[CONF_OAUTH_TOKEN] == "aas_et/FROM_SECRETS"
    assert data[CONF_GOOGLE_EMAIL] == "user@example.com"


# ---------------------------------------------------------------------------
# RED characterization tests for the D1 gap and the D2 understated warning.
#
# Design decision (single-key inversion, v5): the presence of ``shared_key`` is
# the only gate that decides whether a pasted secrets bundle is importable. An
# owner-only bundle without a ``shared_key`` is a non-renewable dead end and must
# be blocked; conversely a bundle that carries a ``shared_key`` but lacks (or has
# a stale) ``owner_key`` is tolerated because the integration can fetch/refresh
# the owner key itself.
#
# Today's code does NOT enforce that rule, so these tests assert the *target*
# behavior and are therefore expected to fail. They are marked
# ``xfail(strict=True)`` so that once AP3/AP4 land the fix, the now-passing tests
# turn into ``xpassed`` -> ``failed`` under strict mode, forcing the GREEN flip
# to be made explicit.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D1 gap: _interpret_credentials_choice currently accepts a secrets "
        "bundle that has a valid email and token but no shared_key. Under the "
        "v5 single-key rule both owner-missing and owner-present inputs must be "
        "blocked with the 'keys_missing' error. Flips to xpass once AP3/AP4 add "
        "the shared_key gate."
    ),
)
def test_d1_shared_missing_currently_accepted_xfail() -> None:
    """Nail down the D1 gap: shared_key-less bundles must block under v5.

    ``_interpret_credentials_choice`` is a pure function, so no ConfigEntry or
    hass stub is required. Two input classes are pinned, both of which carry a
    valid email and a plausible token and must block once the single-key rule
    is enforced:

    * (a) shared_key missing AND owner_key missing.
    * (b) shared_key missing BUT owner_key present.
    """
    # Class (a): neither shared_key nor owner_key present.
    bundle_no_owner = {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
    }
    # Class (b): owner_key present, shared_key still missing.
    bundle_with_owner = {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "owner_key": "AABBCC",
    }

    for bundle in (bundle_no_owner, bundle_with_owner):
        _method, _email, _cands, err = config_flow._interpret_credentials_choice(
            {"secrets_json": json.dumps(bundle)},
            secrets_field="secrets_json",
            token_field=CONF_OAUTH_TOKEN,
            email_field=CONF_GOOGLE_EMAIL,
        )
        # Target (v5) behavior: a missing shared_key blocks the import.
        assert err == "keys_missing", (
            "shared_key-less bundle must be rejected with 'keys_missing' "
            f"(owner_key present={'owner_key' in bundle})"
        )


class _CapturingCache:
    """Minimal cache stub recording values written by the secrets migrator.

    Mirrors the ``_CapturingCache`` pattern in
    ``tests/test_token_cache_secrets.py`` so the seeding helper can be exercised
    without a real ``TokenCache`` or ConfigEntry.
    """

    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        self.saved[name] = value


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "D2 understatement: when shared_key is missing, _async_save_secrets_data "
        "warns only about FMDN crowdsourced reports and omits the rotation-driven "
        "late outage. This test pins the current incomplete wording; it flips to "
        "xpass once AP3/AP4 broaden the warning to cover the rotation outage."
    ),
)
async def test_d2_seeding_warning_understates_outage_xfail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nail down the D2 understatement in the seeding warning text.

    The current warning only claims an FMDN ("crowdsourced") restriction and
    does not mention the rotation-driven late outage that follows once the
    existing key rotates. We pin the present incomplete wording so the GREEN
    flip becomes visible once the message is broadened.
    """
    from custom_components.googlefindmy import __init__ as integration_init

    cache = _CapturingCache()
    secrets_bundle = {
        "google_email": "user@example.com",
        "username": "user@example.com",
        "owner_key": "AABBCC",
        # shared_key intentionally absent -> triggers the seeding warning.
    }

    with caplog.at_level("WARNING"):
        await integration_init._async_save_secrets_data(cache, secrets_bundle)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "shared_key" in record.getMessage()
    ]
    assert warnings, "Expected a missing-shared_key warning to be emitted"
    message = warnings[0]
    # The current text understates the outage: it only mentions the FMDN
    # restriction and does not surface the rotation-driven late outage.
    assert "FMDN network (crowdsourced) location reports will fail to decrypt" in message
    assert "rotat" not in message.lower(), (
        "Today's warning omits the rotation outage; once it is added this "
        "assertion fails and the strict xfail flips to a forced GREEN."
    )
