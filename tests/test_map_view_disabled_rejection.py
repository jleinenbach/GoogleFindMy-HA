# tests/test_map_view_disabled_rejection.py
"""A disabled entry's valid share token is refused exactly like an invalid one.

``map_view_enabled=False`` must not turn the share-token endpoints into a
validity oracle: whoever holds a token of a disabled entry may not learn from
the response that the token is genuine. The views stay registered process-wide
(Home Assistant has no ``unregister_view``), so the refusal is decided per
request. This module compares the full response of every view that resolves
a share token (status, body and every header except the transport-added
``Date`` and ``Server``) and pins that the redirect view, which resolves no
entry at all, answers both tokens the same way.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
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

_TRANSPORT_HEADERS = frozenset({"date", "server"})
_TILES_TOKEN = "b" * 64
_DEVICE_ID = "device123"


class _StubConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


class _StubHass:
    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {
            "core.uuid": "test-ha",
            "map_tiles": deque([_TILES_TOKEN], maxlen=2),
        }
        self.config_entries = _StubConfigEntries(entries)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _entry(*, enabled: bool) -> Any:
    return make_config_entry(
        entry_id="entry-id",
        runtime_data=None,
        options={OPT_MAP_VIEW_ENABLED: enabled},
    )


def _share_token(hass: _StubHass, entry: Any) -> str:
    return map_token_hex_digest(
        map_token_secret_seed(hass.data["core.uuid"], entry.entry_id, False)
    )


def _comparable(response: Any) -> tuple[int, Any, dict[str, str]]:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _TRANSPORT_HEADERS
    }
    return response.status, response.body, headers


@pytest.mark.asyncio
async def test_map_view_refuses_a_disabled_token_like_an_invalid_one() -> None:
    """The map page answers a disabled entry's valid token like a wrong one."""

    entry = _entry(enabled=False)
    hass = _StubHass([entry])
    view = map_view.GoogleFindMyMapView(hass)

    disabled = await view.get(
        SimpleNamespace(query={"token": _share_token(hass, entry)}),
        device_id=_DEVICE_ID,
    )
    invalid = await view.get(
        SimpleNamespace(query={"token": "0" * 64}), device_id=_DEVICE_ID
    )

    assert disabled.status == 401
    assert _comparable(disabled) == _comparable(invalid)
    # Control: the refused token is genuine for the entry, so the identical
    # 401 comes from the toggle, not from a token the view could not resolve.
    resolved, _accepted = map_view._resolve_entry_by_token(
        hass, _share_token(hass, entry)
    )
    assert resolved is entry


@pytest.mark.asyncio
async def test_tiles_token_view_refuses_a_disabled_token_like_an_invalid_one() -> None:
    """The tile-token endpoint answers a disabled entry's token like a wrong one.

    The enabled control shows the comparison can tell the cases apart: the same
    token on an enabled entry is served.
    """

    header = map_view._MAP_TILES_TOKEN_HEADER
    disabled_entry = _entry(enabled=False)
    hass = _StubHass([disabled_entry])
    view = map_view.GoogleFindMyMapTilesTokenView(hass)

    disabled = await view.get(
        SimpleNamespace(headers={header: _share_token(hass, disabled_entry)}, query={})
    )
    invalid = await view.get(SimpleNamespace(headers={header: "0" * 64}, query={}))

    assert disabled.status == 401
    assert _comparable(disabled) == _comparable(invalid)

    enabled_entry = _entry(enabled=True)
    enabled_hass = _StubHass([enabled_entry])
    served = await map_view.GoogleFindMyMapTilesTokenView(enabled_hass).get(
        SimpleNamespace(
            headers={header: _share_token(enabled_hass, enabled_entry)}, query={}
        )
    )
    assert served.status == 200


@pytest.mark.asyncio
async def test_redirect_view_answers_a_disabled_and_an_invalid_token_alike() -> None:
    """The redirect resolves no entry, so both tokens get the same 302.

    Only the forwarded token differs in ``Location``; every other header is the
    same, so the redirect cannot reveal which token is genuine either.
    """

    entry = _entry(enabled=False)
    hass = _StubHass([entry])
    view = map_view.GoogleFindMyMapRedirectView(hass)
    valid = _share_token(hass, entry)
    invalid = "0" * 64

    with pytest.raises(map_view.web.HTTPFound) as disabled_ctx:
        await view.get(SimpleNamespace(query={"token": valid}), device_id=_DEVICE_ID)
    with pytest.raises(map_view.web.HTTPFound) as invalid_ctx:
        await view.get(SimpleNamespace(query={"token": invalid}), device_id=_DEVICE_ID)

    disabled_resp, invalid_resp = disabled_ctx.value, invalid_ctx.value
    assert disabled_resp.status == invalid_resp.status == 302
    assert disabled_resp.location.replace(valid, "<token>") == (
        invalid_resp.location.replace(invalid, "<token>")
    )
    strip = _TRANSPORT_HEADERS | {"location"}
    assert {
        k: v for k, v in disabled_resp.headers.items() if k.lower() not in strip
    } == {k: v for k, v in invalid_resp.headers.items() if k.lower() not in strip}
