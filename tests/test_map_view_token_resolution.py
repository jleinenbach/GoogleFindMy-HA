# tests/test_map_view_token_resolution.py
"""Resolving a share token compares it against every config entry.

``_resolve_entry_by_token`` must not return at the first matching entry: the
time it spends would then tell which entry a token belongs to and whether it
matched any.
The tests count the comparisons for every position of the match and for no
match at all.
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


def _entries(count: int) -> list[Any]:
    return [
        make_config_entry(
            entry_id=f"entry-{index}",
            runtime_data=None,
            options={OPT_MAP_VIEW_ENABLED: index != 1},
        )
        for index in range(count)
    ]


def _token_for(hass: _StubHass, entry: Any) -> str:
    return map_token_hex_digest(
        map_token_secret_seed(hass.data["core.uuid"], entry.entry_id, False)
    )


@pytest.mark.parametrize("position", [0, 1, 2, None], ids=["1st", "2nd", "3rd", "none"])
def test_every_entry_is_compared_wherever_the_match_is(
    monkeypatch: pytest.MonkeyPatch, position: int | None
) -> None:
    entries = _entries(3)
    hass = _StubHass(entries)
    token = "0" * 64 if position is None else _token_for(hass, entries[position])

    calls: list[set[str]] = []
    real = map_view._token_matches

    def _counting(auth_token: str, accepted: set[str]) -> bool:
        calls.append(accepted)
        return real(auth_token, accepted)

    monkeypatch.setattr(map_view, "_token_matches", _counting)
    resolved, accepted = map_view._resolve_entry_by_token(hass, token)  # type: ignore[arg-type]

    assert len(calls) == len(entries)
    if position is None:
        assert (resolved, accepted) == (None, None)
    else:
        # The entry with Map View off (position 1) resolves as well; refusing
        # it is the caller's job.
        assert resolved is entries[position]
        assert accepted is not None and token in accepted


def test_the_first_match_wins_when_two_entries_accept_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _entries(3)
    hass = _StubHass(entries)
    shared = {"f" * 64}
    monkeypatch.setattr(
        map_view,
        "_entry_accept_tokens",
        lambda _hass, entry_id, _exp: shared if entry_id != "entry-0" else {"e" * 64},
    )

    resolved, accepted = map_view._resolve_entry_by_token(hass, "f" * 64)  # type: ignore[arg-type]

    assert resolved is entries[1]
    assert accepted is shared
