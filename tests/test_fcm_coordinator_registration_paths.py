# tests/test_fcm_coordinator_registration_paths.py
"""Edge paths of coordinator (un)registration and the credential callback.

These functions are touched by the entry write-back of the FCM registration;
the paths pinned here were not exercised by any test before:

* ``register_coordinator`` registers a coordinator once, returns early without
  a config entry, logs (without the text) when flushing or loading routing
  tokens fails, skips persisted tokens that are not non-empty strings, and
  does not fail when mirroring the registration to the cache raises.
* ``unregister_coordinator`` hands the entry's cache over to another
  coordinator of the same entry instead of tombstoning the entry, and ignores
  a coordinator without a config entry.
* ``_on_credentials_updated_for_entry`` decodes a registration that arrives as
  a JSON string and drops one that is not JSON.
* ``_invalidate_fcm_tokens`` leaves an entry without a registration alone.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from tests.helpers.config_entries_stub import make_config_entry

ENTRY_ID = "entry-coordinator-paths"


class _Cache:
    """Token cache double: ``get`` returns or raises, ``set`` records."""

    def __init__(
        self, stored: dict[str, Any] | None = None, get_error: Exception | None = None
    ) -> None:
        self.entry_id = ENTRY_ID
        self.stored = dict(stored or {})
        self.get_error = get_error

    async def get(self, name: str) -> Any:
        if self.get_error is not None:
            raise self.get_error
        return self.stored.get(name)

    # Not run: no test of this file reaches it; only
    # test_invalidating_an_entry_without_a_registration_changes_nothing
    # checks that nothing was stored.
    async def set(self, name: str, value: Any) -> None:  # pragma: no cover
        self.stored[name] = value


class _SetRaises(_Cache):
    """Cache whose ``set`` fails before a coroutine exists."""

    def set(self, name: str, value: Any) -> Any:  # type: ignore[override]
        raise TypeError("set-text")


def _coordinator(cache: Any, entry_id: str | None = ENTRY_ID) -> Any:
    entry = None if entry_id is None else make_config_entry(entry_id=entry_id)
    return SimpleNamespace(config_entry=entry, cache=cache)


def _receiver(monkeypatch: pytest.MonkeyPatch) -> FcmReceiverHA:
    receiver = FcmReceiverHA()

    async def _no_supervisor(entry_id: str, cache: Any) -> None:
        return None

    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", _no_supervisor)
    return receiver


async def _settle(receiver: FcmReceiverHA) -> None:
    await asyncio.gather(*list(receiver._active_tasks))


@pytest.mark.asyncio
async def test_a_coordinator_is_registered_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _receiver(monkeypatch)
    coordinator = _coordinator(_Cache())

    receiver.register_coordinator(coordinator)
    receiver.register_coordinator(coordinator)
    await _settle(receiver)

    assert receiver.coordinators == [coordinator]


@pytest.mark.asyncio
async def test_a_coordinator_without_entry_is_kept_but_not_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _receiver(monkeypatch)
    coordinator = _coordinator(_Cache(), entry_id=None)

    receiver.register_coordinator(coordinator)

    assert receiver.coordinators == [coordinator]
    assert receiver._entry_caches == {}
    assert receiver._active_tasks == set()


@pytest.mark.asyncio
async def test_failing_routing_token_reads_are_logged_without_their_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Both the flush of pending tokens and the load of persisted ones fail."""

    receiver = _receiver(monkeypatch)
    receiver._pending_routing_tokens[ENTRY_ID] = {"pending-token"}

    with caplog.at_level(logging.DEBUG):
        receiver.register_coordinator(
            _coordinator(_Cache(get_error=RuntimeError("get-text")))
        )
        await _settle(receiver)

    messages = [r.getMessage() for r in caplog.records]
    assert any("flush pending routing" in m for m in messages)
    assert any("load persisted routing" in m for m in messages)
    assert not any("get-text" in m for m in messages)
    assert receiver._entry_to_tokens[ENTRY_ID] == {"pending-token"}


@pytest.mark.asyncio
async def test_only_non_empty_string_routing_tokens_are_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observable result; ``_update_token_routing`` filters the same way."""

    receiver = _receiver(monkeypatch)
    cache = _Cache({"fcm_routing_tokens": ["valid-token", "", 7]})

    receiver.register_coordinator(_coordinator(cache))
    await _settle(receiver)

    assert receiver._token_to_entries == {"valid-token": {ENTRY_ID}}


@pytest.mark.asyncio
async def test_a_failing_mirror_of_the_registration_does_not_fail_registration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = _receiver(monkeypatch)
    receiver.creds[ENTRY_ID] = {"gcm": {"android_id": "1"}}
    cache = _SetRaises()

    with caplog.at_level(logging.DEBUG):
        receiver.register_coordinator(_coordinator(cache))
        await _settle(receiver)

    assert receiver._entry_caches[ENTRY_ID] is cache
    messages = [r.getMessage() for r in caplog.records]
    assert any("persistence of the FCM registration skipped" in m for m in messages)
    assert not any("set-text" in m for m in messages)


@pytest.mark.asyncio
async def test_unregistering_hands_the_cache_to_another_coordinator_of_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _receiver(monkeypatch)
    first, second = _Cache(), _Cache()
    leaving = _coordinator(first)
    receiver.register_coordinator(leaving)
    receiver.register_coordinator(_coordinator(_Cache(), entry_id="other-entry"))
    receiver.register_coordinator(_coordinator(second))
    await _settle(receiver)

    receiver.unregister_coordinator(leaving)

    assert receiver._entry_caches[ENTRY_ID] is second
    assert ENTRY_ID not in receiver._unregistered


def test_unregistering_a_coordinator_without_entry_touches_no_entry() -> None:
    receiver = FcmReceiverHA()
    coordinator = _coordinator(_Cache(), entry_id=None)
    receiver.coordinators.append(coordinator)

    receiver.unregister_coordinator(coordinator)

    assert receiver.coordinators == []
    assert receiver._unregistered == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrived", "kept"),
    [('{"gcm": {"android_id": "1"}}', {"gcm": {"android_id": "1"}}), ("{", None)],
    ids=["json", "not-json"],
)
async def test_a_registration_arriving_as_a_string_is_decoded_or_dropped(
    arrived: str, kept: Any
) -> None:
    receiver = FcmReceiverHA()

    receiver._on_credentials_updated_for_entry(ENTRY_ID, arrived)
    await asyncio.gather(*list(receiver._active_tasks))

    assert receiver.creds[ENTRY_ID] == kept


@pytest.mark.asyncio
async def test_invalidating_an_entry_without_a_registration_changes_nothing() -> None:
    receiver = FcmReceiverHA()
    receiver.creds[ENTRY_ID] = None
    cache = _Cache()
    receiver._entry_caches[ENTRY_ID] = cache  # type: ignore[assignment]

    await receiver._invalidate_fcm_tokens(ENTRY_ID)

    assert receiver.creds[ENTRY_ID] is None
    assert cache.stored == {}
