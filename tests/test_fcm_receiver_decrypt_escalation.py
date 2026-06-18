# tests/test_fcm_receiver_decrypt_escalation.py
"""Background-push decrypt-failure escalation in :class:`FcmReceiverHA`.

A push-only setup must escalate to a reauth flow identically to the poll path.
Because the background decode runs outside a coordinator update cycle, the
receiver cannot raise ``ConfigEntryAuthFailed``; instead it starts the entry's
reauth flow directly when the shared counter (driven through the coordinator's
``note_decrypt_failure``) reports that the threshold was reached.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    StaleOwnerKeyError,
)


def _receiver_with_coordinator(*, escalate: bool) -> tuple[FcmReceiverHA, MagicMock, MagicMock]:
    receiver = FcmReceiverHA()
    hass = MagicMock()
    receiver._hass = hass
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.config_entry = entry
    coordinator.note_decrypt_failure = MagicMock(return_value=escalate)
    receiver.coordinators = [coordinator]
    return receiver, coordinator, entry


def test_push_decrypt_failure_starts_reauth_when_threshold_reached() -> None:
    """When the coordinator reports escalation, the entry's reauth flow is started
    (the only way to surface a reauth from the background push path)."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=True)
    err = DecryptionError("stale shared key")

    receiver._note_decrypt_failure_for_entry("entry-1", stale=False, error=err)

    coordinator.note_decrypt_failure.assert_called_once_with(stale=False, error=err)
    entry.async_start_reauth.assert_called_once_with(receiver._hass)


def test_push_decrypt_failure_below_threshold_does_not_start_reauth() -> None:
    """Below threshold (escalate=False), the counter is fed but no reauth is fired.
    A per-tracker stale key (stale=True) likewise never starts an account reauth."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=False)

    receiver._note_decrypt_failure_for_entry(
        "entry-1", stale=True, error=StaleOwnerKeyError("tracker outdated")
    )

    coordinator.note_decrypt_failure.assert_called_once()
    entry.async_start_reauth.assert_not_called()


def test_push_decrypt_failure_swallows_coordinator_errors() -> None:
    """The push path must never break: a misbehaving coordinator is swallowed."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=True)
    coordinator.note_decrypt_failure = MagicMock(side_effect=RuntimeError("boom"))

    # Must not raise.
    receiver._note_decrypt_failure_for_entry(
        "entry-1", stale=False, error=DecryptionError("x")
    )
    entry.async_start_reauth.assert_not_called()


def test_push_decrypt_failure_no_coordinator_for_entry_is_noop() -> None:
    """Unknown entry id: nothing to feed, no crash."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=True)

    receiver._note_decrypt_failure_for_entry(
        "other-entry", stale=False, error=DecryptionError("x")
    )

    coordinator.note_decrypt_failure.assert_not_called()
    entry.async_start_reauth.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expect_reauth"),
    [
        (DecryptionError("stale shared key"), True),
        (StaleOwnerKeyError("tracker outdated"), False),
    ],
)
async def test_decode_background_location_feeds_counter_on_decrypt_error(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, expect_reauth: bool
) -> None:
    """The background decode path must funnel decryption failures into the shared
    counter (and start reauth when the account-wide threshold is reached) instead
    of silently returning an empty result."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=True)
    receiver._entry_caches["entry-1"] = MagicMock()
    # Model the real hook contract: a per-tracker stale key never escalates.
    coordinator.note_decrypt_failure = MagicMock(
        side_effect=lambda *, stale, error: not stale
    )

    # Parse step returns a harmless object; the decrypt step raises.
    monkeypatch.setattr(
        fcm_receiver_ha.decoder_module,
        "parse_device_update_protobuf",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        fcm_receiver_ha,
        "async_decrypt_location_response_locations",
        AsyncMock(side_effect=exc),
    )

    result = await receiver._decode_background_location_async("entry-1", "deadbeef")

    assert result == {}
    coordinator.note_decrypt_failure.assert_called_once()
    assert entry.async_start_reauth.called is expect_reauth


@pytest.mark.asyncio
async def test_decode_background_location_clears_counter_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful background decrypt must clear the shared reauth budget.

    The background path feeds decrypt *failures* into the coordinator's
    account-wide counter, so a successful background decrypt has to clear it too
    (symmetry with the poll cycle). Otherwise non-consecutive push failures
    interleaved with healthy pushes could accumulate to the reauth threshold and
    trip a spurious reauth for a perfectly healthy account.
    """
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=False)
    receiver._entry_caches["entry-1"] = MagicMock()

    monkeypatch.setattr(
        fcm_receiver_ha.decoder_module,
        "parse_device_update_protobuf",
        MagicMock(return_value=MagicMock()),
    )
    # Decrypt succeeds and yields one usable record (positive proof of the key).
    monkeypatch.setattr(
        fcm_receiver_ha,
        "async_decrypt_location_response_locations",
        AsyncMock(
            return_value=[{"last_seen": 123.0, "latitude": 1.0, "longitude": 2.0}]
        ),
    )

    result = await receiver._decode_background_location_async("entry-1", "deadbeef")

    assert result.get("last_seen") == 123.0
    coordinator.note_decrypt_success.assert_called_once_with()
    coordinator.note_decrypt_failure.assert_not_called()
    entry.async_start_reauth.assert_not_called()


@pytest.mark.asyncio
async def test_decode_background_location_metadata_only_does_not_clear_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata_only decode result must NOT clear the shared reauth budget.

    The decode layer returns a single ``metadata_only`` sentinel row (key material
    from the secrets bundle, no authenticated decrypt) when no encrypted report
    decrypts. That row is non-empty, but it is no positive proof the shared key
    works, so report-less pushes interleaved with real failures must not keep a
    stale key below the reauth threshold (Codex finding on PR #182). The metadata
    must still pass through downstream.
    """
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=False)
    receiver._entry_caches["entry-1"] = MagicMock()

    monkeypatch.setattr(
        fcm_receiver_ha.decoder_module,
        "parse_device_update_protobuf",
        MagicMock(return_value=MagicMock()),
    )
    # Decrypt yields only a metadata_only sentinel (no usable location report).
    monkeypatch.setattr(
        fcm_receiver_ha,
        "async_decrypt_location_response_locations",
        AsyncMock(
            return_value=[{"metadata_only": True, "owner_key_version": 7}]
        ),
    )

    result = await receiver._decode_background_location_async("entry-1", "deadbeef")

    # Metadata still flows downstream ...
    assert result.get("metadata_only") is True
    # ... but the budget is NOT cleared (no authenticated decrypt happened).
    coordinator.note_decrypt_success.assert_not_called()
    coordinator.note_decrypt_failure.assert_not_called()
    entry.async_start_reauth.assert_not_called()


def test_note_decrypt_success_for_entry_swallows_coordinator_errors() -> None:
    """The push success path must never break: a misbehaving coordinator is
    swallowed exactly like the failure path."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=False)
    coordinator.note_decrypt_success = MagicMock(side_effect=RuntimeError("boom"))

    # Must not raise.
    receiver._note_decrypt_success_for_entry("entry-1")

    coordinator.note_decrypt_success.assert_called_once_with()


def test_note_decrypt_success_for_entry_unknown_entry_is_noop() -> None:
    """Unknown entry id: nothing to clear, no crash."""
    receiver, coordinator, entry = _receiver_with_coordinator(escalate=False)

    receiver._note_decrypt_success_for_entry("other-entry")

    coordinator.note_decrypt_success.assert_not_called()
