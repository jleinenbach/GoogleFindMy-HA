# tests/test_map_view_token_compare.py
"""The share token is compared in constant time and never raises.

``_resolve_entry_by_token`` decides who may read a map. The token is taken
from the query string, so the comparison must not leak through its timing how
much of a guess matched, and no token, however odd, may turn the refusal into
a server error. ``hmac.compare_digest`` raises ``TypeError`` for a ``str`` with
non-ASCII characters, so the comparison runs on bytes; these tests pin both.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from custom_components.googlefindmy import map_view
from custom_components.googlefindmy.const import (
    DOMAIN,
    OPT_MAP_VIEW_ENABLED,
    map_token_hex_digest,
    map_token_secret_seed,
)
from tests.helpers.config_entries_stub import make_config_entry


class _StubConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


class _StubHass:
    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {
            "core.uuid": "test-ha",
            "map_tiles": deque(maxlen=2),
        }
        self.config_entries = _StubConfigEntries(entries)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _setup() -> tuple[_StubHass, Any, str]:
    entry = make_config_entry(
        entry_id="entry-id",
        runtime_data=None,
        options={OPT_MAP_VIEW_ENABLED: True},
    )
    hass = _StubHass([entry])
    token = map_token_hex_digest(
        map_token_secret_seed(hass.data["core.uuid"], entry.entry_id, False)
    )
    return hass, entry, token


def test_valid_token_resolves_its_entry_through_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, entry, token = _setup()
    calls: list[tuple[bytes, bytes]] = []
    real = map_view.hmac.compare_digest

    def _recording(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(map_view.hmac, "compare_digest", _recording)
    resolved, accepted = map_view._resolve_entry_by_token(hass, token)
    assert resolved is entry
    assert accepted is not None and token in accepted
    assert (token.encode("utf-8"), token.encode("utf-8")) in calls


@pytest.mark.parametrize(
    "guess",
    ["", "0" * 64, "\u00e4" * 64, "token\udcff", "\u2028"],
    ids=["empty", "wrong-hex", "non-ascii", "lone-surrogate", "line-separator"],
)
def test_any_wrong_token_is_refused_without_raising(guess: str) -> None:
    hass, _entry, _token = _setup()
    assert map_view._resolve_entry_by_token(hass, guess) == (None, None)


def test_every_accepted_token_goes_through_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = map_view.hmac.compare_digest

    def _recording(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(map_view.hmac, "compare_digest", _recording)
    accepted = {"a" * 64, "b" * 64}

    assert map_view._token_matches("a" * 64, accepted) is True
    # No early exit: the second candidate is compared although the first matched.
    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


@pytest.mark.asyncio
async def test_map_endpoint_refuses_a_non_ascii_token_with_401() -> None:
    hass, _entry, _token = _setup()
    view = map_view.GoogleFindMyMapView(hass)

    class _Request:
        query = {"token": "\u00e4" * 64}

    response = await view.get(_Request(), "device123")  # type: ignore[arg-type]
    assert response.status == 401
