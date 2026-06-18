# tests/test_encryption_key_status_sensor.py
"""Tests for the Encryption Key Status diagnostic sensor (AP-9 / AP-10).

Four concerns:

- **T9 wiring** -- ``note_decrypt_failure`` maps each decryption error type to
  the matching ``CryptoStatus`` on the coordinator, so the sensor and the reauth
  escalation are driven from a single source (D6).
- **T9 sensor** -- the entity renders that state, maps ``UNKNOWN`` to ``None``
  for Home Assistant's enum contract (``native_value`` must be one of
  ``options`` or ``None``), and every option is covered.
- **T10 rename non-breaking** -- the connectivity entity's identity
  (``key`` / ``translation_key``, from which ``entity_id`` and ``unique_id``
  derive) is independent of the translated name, so the AP-9 name change cannot
  alter existing ``entity_id``/``unique_id`` values or break automations.
- **T11 translation completeness** -- every shipped translation file defines the
  new sensor (name + four states) and the renamed connectivity name, so the
  diagnosis stays consistent across all locales.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.binary_sensor import CONNECTIVITY_DESC
from custom_components.googlefindmy.coordinator.helpers.stats import CryptoStatus
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    SharedKeyMismatchError,
    SharedKeyMissingError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.sensor import (
    ENCRYPTION_KEY_STATUS_OPTIONS,
    GoogleFindMyEncryptionKeyStatusSensor,
)

from .helpers.polling_mixin_stub import PollingStub

_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
_TRANSLATIONS_DIR = _ROOT / "translations"
_STRINGS_JSON = _ROOT / "strings.json"
_REQUIRED_STATES = {
    "ok",
    "shared_key_invalid",
    "shared_key_missing",
    "tracker_key_outdated",
}


def _make_stub() -> PollingStub:
    """Return a PollingStub seeded for decrypt-failure bookkeeping."""

    stub = PollingStub()
    stub._consecutive_decrypt_failures = 0
    stub._last_decrypt_reauth_monotonic = None
    stub._last_decrypt_error = None
    stub._monotonic = lambda: 1_000_000.0
    stub._set_auth_state = MagicMock()
    return stub


def _build_sensor(coordinator: Any) -> GoogleFindMyEncryptionKeyStatusSensor:
    """Instantiate the sensor without running the HA entity __init__ chain."""

    sensor = GoogleFindMyEncryptionKeyStatusSensor.__new__(
        GoogleFindMyEncryptionKeyStatusSensor
    )
    sensor.coordinator = coordinator
    sensor._attr_unique_id = "googlefindmy_entry_service_encryption_key_status"
    return sensor


def _all_translation_files() -> list[Path]:
    return sorted(_TRANSLATIONS_DIR.glob("*.json")) + [_STRINGS_JSON]


# --------------------------------------------------------------------------- #
# T9 wiring: decryption error type -> CryptoStatus (single source, D6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stale", "error", "expected"),
    [
        (False, SharedKeyMismatchError("bad tag"), CryptoStatus.SHARED_KEY_INVALID),
        (False, SharedKeyMissingError("empty"), CryptoStatus.SHARED_KEY_MISSING),
        (False, DecryptionError("own reports failed"), CryptoStatus.SHARED_KEY_INVALID),
        (True, StaleOwnerKeyError("v2 != v3"), CryptoStatus.TRACKER_KEY_OUTDATED),
    ],
)
def test_t9_note_decrypt_failure_maps_to_crypto_status(
    stale: bool, error: Exception, expected: str
) -> None:
    """Each error type lands as the matching diagnostic state on the coordinator.

    This is the mutation-sharp assertion: swapping the mapping in
    ``note_decrypt_failure`` flips the expected state and turns this red.
    """

    stub = _make_stub()
    stub.note_decrypt_failure(stale=stale, error=error, device="dev")
    assert stub.crypto_status.state == expected


def test_t9_crypto_status_ok_and_churn_guard() -> None:
    """``_set_crypto_status`` updates the snapshot and no-ops on unchanged state."""

    stub = _make_stub()
    stub.note_decrypt_failure(
        stale=False, error=SharedKeyMismatchError("x"), device="d"
    )
    assert stub.crypto_status.state == CryptoStatus.SHARED_KEY_INVALID

    stub._set_crypto_status(CryptoStatus.OK)
    assert stub.crypto_status.state == CryptoStatus.OK

    # Churn guard: setting the same state again is a no-op (early return), the
    # snapshot stays stable and no exception escapes.
    stub._set_crypto_status(CryptoStatus.OK)
    assert stub.crypto_status.state == CryptoStatus.OK


def test_t9_crypto_status_survives_listener_failure() -> None:
    """A listener failure during _set_crypto_status is swallowed; state persists.

    Mirrors the api/fcm status setters: a very early ``async_set_updated_data``
    failure (listeners not ready) must not lose the state transition.
    """

    stub = _make_stub()
    stub.async_set_updated_data = MagicMock(side_effect=RuntimeError("early"))
    stub._set_crypto_status(CryptoStatus.SHARED_KEY_MISSING, reason="startup")
    assert stub.crypto_status.state == CryptoStatus.SHARED_KEY_MISSING
    assert stub.crypto_status.reason == "startup"


# --------------------------------------------------------------------------- #
# T9 sensor: native_value rendering + HA enum contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "expected_native"),
    [
        (CryptoStatus.UNKNOWN, None),
        (CryptoStatus.OK, "ok"),
        (CryptoStatus.SHARED_KEY_INVALID, "shared_key_invalid"),
        (CryptoStatus.SHARED_KEY_MISSING, "shared_key_missing"),
        (CryptoStatus.TRACKER_KEY_OUTDATED, "tracker_key_outdated"),
    ],
)
def test_t9_sensor_native_value(state: str, expected_native: str | None) -> None:
    """The sensor renders the coordinator state; UNKNOWN -> None (HA "unknown")."""

    coordinator = SimpleNamespace(
        crypto_status=SimpleNamespace(state=state, changed_at=1_700_000_000.0),
        _consecutive_decrypt_failures=0,
        _last_decrypt_error=None,
    )
    sensor = _build_sensor(coordinator)

    assert sensor.native_value == expected_native
    # HA enum contract: a non-None native_value MUST be a declared option.
    if sensor.native_value is not None:
        assert sensor.native_value in ENCRYPTION_KEY_STATUS_OPTIONS


def test_t9_native_value_handles_missing_snapshot() -> None:
    """A coordinator without a usable crypto_status renders as unknown (None)."""

    sensor = _build_sensor(SimpleNamespace(crypto_status=None))
    assert sensor.native_value is None


def test_t9_sensor_attaches_to_service_device() -> None:
    """The sensor groups under the per-entry service device (account-wide)."""

    from tests.helpers.config_entries_stub import make_config_entry

    sensor = GoogleFindMyEncryptionKeyStatusSensor.__new__(
        GoogleFindMyEncryptionKeyStatusSensor
    )
    sensor.coordinator = SimpleNamespace(
        config_entry=make_config_entry(entry_id="entry")
    )
    sensor._subentry_identifier = "service"
    sensor._subentry_key = "service"

    info = sensor.device_info
    assert info is not None
    assert getattr(info, "identifiers", None)


def test_t9_options_cover_all_states_except_unknown() -> None:
    """``options`` lists exactly the four concrete states; UNKNOWN is excluded."""

    assert set(ENCRYPTION_KEY_STATUS_OPTIONS) == {
        CryptoStatus.OK,
        CryptoStatus.SHARED_KEY_INVALID,
        CryptoStatus.SHARED_KEY_MISSING,
        CryptoStatus.TRACKER_KEY_OUTDATED,
    }
    assert CryptoStatus.UNKNOWN not in ENCRYPTION_KEY_STATUS_OPTIONS


def test_t9_extra_state_attributes_show_escalation_progress() -> None:
    """Attributes expose escalation progress from the same coordinator fields."""

    coordinator = SimpleNamespace(
        crypto_status=SimpleNamespace(
            state=CryptoStatus.SHARED_KEY_INVALID, changed_at=1_700_000_000.0
        ),
        _consecutive_decrypt_failures=2,
        _last_decrypt_error="SharedKeyMismatchError: bad tag",
    )
    sensor = _build_sensor(coordinator)

    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["consecutive_decrypt_failures"] == 2
    assert attrs["last_decrypt_error_class"] == "SharedKeyMismatchError"
    assert attrs["changed_at"] == 1_700_000_000.0


def test_t9_extra_state_attributes_empty_when_pristine() -> None:
    """No attributes are emitted before any decryption activity (returns None)."""

    coordinator = SimpleNamespace(
        crypto_status=SimpleNamespace(state=CryptoStatus.UNKNOWN, changed_at=None),
        _consecutive_decrypt_failures=None,
        _last_decrypt_error=None,
    )
    sensor = _build_sensor(coordinator)
    assert sensor.extra_state_attributes is None


# --------------------------------------------------------------------------- #
# T10 rename non-breaking: identity is independent of the translated name
# --------------------------------------------------------------------------- #


def test_t10_connectivity_identity_independent_of_translated_name() -> None:
    """AP-9 changes only the translated name; the entity identity is untouched.

    ``entity_id``/``unique_id`` derive from ``key``/``translation_key`` and the
    unique-id suffix, never from the (now FCM-qualified) display name -- so the
    rename cannot duplicate registry entries or break existing automations.
    """

    assert CONNECTIVITY_DESC.key == "connectivity"
    assert CONNECTIVITY_DESC.translation_key == "connectivity"

    en = json.loads((_TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    assert (
        en["entity"]["binary_sensor"]["connectivity"]["name"]
        == "FCM Connection Status"
    )


# --------------------------------------------------------------------------- #
# T11 translation completeness across every shipped locale + strings.json
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path", _all_translation_files(), ids=lambda p: p.name
)
def test_t11_encryption_key_status_present_and_complete(path: Path) -> None:
    """Every translation file defines the sensor name and all four states."""

    data = json.loads(path.read_text(encoding="utf-8"))
    sensor = data["entity"]["sensor"]
    assert "encryption_key_status" in sensor, f"{path.name} misses the sensor"

    block = sensor["encryption_key_status"]
    name = block.get("name")
    assert isinstance(name, str) and name.strip(), f"{path.name} empty name"

    states = set(block.get("state", {}).keys())
    assert states == _REQUIRED_STATES, f"{path.name}: {states} != {_REQUIRED_STATES}"
    for key, value in block["state"].items():
        assert isinstance(value, str) and value.strip(), f"{path.name} {key} empty"


@pytest.mark.parametrize(
    "path", _all_translation_files(), ids=lambda p: p.name
)
def test_t11_connectivity_rename_present(path: Path) -> None:
    """Every translation file carries the FCM-qualified connectivity name."""

    data = json.loads(path.read_text(encoding="utf-8"))
    name = data["entity"]["binary_sensor"]["connectivity"]["name"]
    assert "FCM" in name, f"{path.name} connectivity name {name!r} lacks FCM"
