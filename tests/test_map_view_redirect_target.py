# tests/test_map_view_redirect_target.py
"""Redirect target of ``GoogleFindMyMapRedirectView`` stays a same-origin map path.

The handler is called directly with already decoded ids. aiohttp matches the
route on the percent-encoded path and then decodes the parameter, so a request
for ``redirect_map/%2F%2Fevil.example`` reaches the handler as ``//evil.example``
(measured with aiohttp 3.14.3). The handler itself must therefore keep the
target below the map path whatever the device id contains.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

from custom_components.googlefindmy import map_view

_PREFIX = "/api/googlefindmy/map/"

_HOSTILE_IDS = [
    "//evil.example",
    "..%2F..%2Fadmin",
    "\\\\host\\share",
    "https://evil.example/x",
    "a?token=stolen#frag",
    "id with space",
]


async def _location(device_id: str, query: dict[str, str]) -> str:
    view = map_view.GoogleFindMyMapRedirectView(SimpleNamespace())
    with pytest.raises(map_view.web.HTTPFound) as ctx:
        await view.get(SimpleNamespace(query=query), device_id=device_id)
    return ctx.value.location


@pytest.mark.asyncio
@pytest.mark.parametrize("device_id", _HOSTILE_IDS)
async def test_hostile_device_id_stays_one_segment_below_map_path(
    device_id: str,
) -> None:
    """Any device id becomes exactly one encoded segment below the map path."""

    location = await _location(device_id, {"token": "abc"})
    parts = urlsplit(location)

    assert not parts.scheme
    assert not parts.netloc
    assert not location.startswith("//")
    assert parts.path.startswith(_PREFIX)
    segment = parts.path[len(_PREFIX) :]
    assert "/" not in segment
    assert "\\" not in segment
    assert unquote(segment) == device_id
    assert not parts.fragment
    assert parse_qs(parts.query) == {"token": ["abc"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("device_id", [".", ".."])
async def test_dot_segment_device_id_is_rejected(device_id: str) -> None:
    """Dot segments would leave the map path once the browser resolves them."""

    view = map_view.GoogleFindMyMapRedirectView(SimpleNamespace())
    response = await view.get(
        SimpleNamespace(query={"token": "abc"}), device_id=device_id
    )

    assert response.status == 400


def test_redirect_prefix_matches_map_view_route() -> None:
    """The redirect target is the map view's own route, not a second literal."""

    assert map_view.GoogleFindMyMapView.url == _PREFIX + "{device_id}"


@pytest.mark.asyncio
async def test_plain_device_id_and_full_query_are_preserved() -> None:
    """A normal id passes unchanged and every query parameter is kept."""

    query = {
        "token": "abc",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-02T00:00:00Z",
        "accuracy": "50",
    }
    location = await _location("device123", query)
    parts = urlsplit(location)

    assert parts.path == _PREFIX + quote("device123", safe="")
    assert parts.path == "/api/googlefindmy/map/device123"
    assert {key: values[0] for key, values in parse_qs(parts.query).items()} == query
