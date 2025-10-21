# tests/test_fcm_receiver_shim.py
"""Regression tests for the legacy FcmReceiver shim token-cache resolution."""

from __future__ import annotations

import copy

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver, token_cache


class _StubCache:
    """Minimal TokenCache stand-in exposing the `_data` attribute."""

    def __init__(self, entry_id: str, initial: dict | None = None) -> None:
        self.entry_id = entry_id
        self._data = copy.deepcopy(initial) if initial is not None else {}


@pytest.fixture
def multi_cache_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, _StubCache]:
    """Provide a registry with two TokenCache stand-ins for the shim."""

    cache_one = _StubCache(
        "entry-one",
        {
            "fcm_credentials": {
                "fcm": {"registration": {"token": "token-one"}},
                "gcm": {"android_id": "0x1"},
            }
        },
    )
    cache_two = _StubCache(
        "entry-two",
        {
            "fcm_credentials": {
                "fcm": {"registration": {"token": "token-two"}},
                "gcm": {"android_id": "0x2"},
            }
        },
    )
    registry: dict[str, _StubCache] = {
        cache_one.entry_id: cache_one,
        cache_two.entry_id: cache_two,
    }
    monkeypatch.setattr(token_cache, "_INSTANCES", registry, raising=False)
    return registry


def test_shim_prefers_explicit_entry_cache(
    multi_cache_registry: dict[str, _StubCache],
) -> None:
    """Entry-specific resolution avoids `_get_default_cache` ambiguity."""

    receiver = fcm_receiver.FcmReceiver(entry_id="entry-two")
    assert receiver.get_fcm_token() == "token-two"
    assert receiver.get_android_id() == "0x2"

    updated_creds = {
        "fcm": {"registration": {"token": "token-two-updated"}},
        "gcm": {"android_id": "0x2"},
    }
    receiver._on_credentials_updated(updated_creds)
    assert multi_cache_registry["entry-two"]._data["fcm_credentials"] == updated_creds

    # A fresh receiver should observe the newly written credentials from the same cache.
    refreshed = fcm_receiver.FcmReceiver(entry_id="entry-two")
    assert refreshed.get_fcm_token() == "token-two-updated"


def test_shim_rejects_unknown_entry_id(
    multi_cache_registry: dict[str, _StubCache],
) -> None:
    """Explicit entry lookups must not mutate arbitrary caches."""

    with pytest.raises(ValueError) as err:
        fcm_receiver.FcmReceiver(entry_id="missing-entry")

    assert "unknown entry_id" in str(err.value)
    # Caches should remain untouched when resolution fails.
    assert (
        multi_cache_registry["entry-one"]._data["fcm_credentials"]["fcm"][
            "registration"
        ]["token"]
        == "token-one"
    )
    assert (
        multi_cache_registry["entry-two"]._data["fcm_credentials"]["fcm"][
            "registration"
        ]["token"]
        == "token-two"
    )


def test_shim_requires_explicit_entry_when_multiple_caches(
    multi_cache_registry: dict[str, _StubCache]
) -> None:
    """Multiple caches require explicit entry selection to avoid ambiguity."""

    with pytest.raises(ValueError) as err:
        fcm_receiver.FcmReceiver()

    assert "cannot auto-select" in str(err.value)
