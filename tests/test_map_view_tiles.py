# tests/test_map_view_tiles.py
"""Map tiles through the Core ``map_tiles`` proxy, with OpenStreetMap as fallback.

Home Assistant Core >= 2026.9 ships ``map_tiles``, a system component that
proxies OpenStreetMap tiles with an application ``User-Agent``, a contact
address and a server-side cache, and that authenticates browser requests with
a short-lived query token rotated every 30 minutes. The map view renders the
tile layer in one of two ways:

* **Proxy branch:** when ``hass.data["map_tiles"]`` holds a token deque, the
  page loads ``/api/map_tiles/raster/{z}/{x}/{y}.png?token=...`` (root-relative,
  same origin, ``referrerPolicy: 'origin'`` as on the direct layer, so the
  page URL with its share token is never sent as ``Referer`` even if Core's
  page-wide ``Referrer-Policy: no-referrer`` header were to change) and
  refreshes the token through a
  share-token guarded endpoint when tiles start failing; the share token
  travels in the ``X-GoogleFindMy-Map-Token`` header, not in the URL.
* **Fallback branch:** on any other Core the page keeps loading tiles directly
  from ``tile.openstreetmap.org`` exactly as before, so a Core that renames or
  drops the component degrades to today's behaviour, never to an empty map.

These tests pin the helper's fail-open rule, the contract with the real Core
constants (as a subprocess, see ``tests/fixtures/map_tiles_probe``), and later
the two rendered branches and the token endpoint.
"""

from __future__ import annotations

import importlib
import json
import logging
import pathlib
import re
import secrets
import subprocess
import sys
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import map_view
from custom_components.googlefindmy.const import (
    DOMAIN,
    map_token_hex_digest,
    map_token_secret_seed,
)
from tests.helpers.config_entries_stub import make_config_entry

_MAP_TILES_PROBE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "map_tiles_probe"
    / "probe.py"
)

_OLD_TOKEN = "a" * 64
_NEW_TOKEN = "b" * 64


# ------------------------------ token helper ------------------------------


def test_map_tiles_access_token_returns_newest() -> None:
    """The newest token is the last element of the Core deque."""

    hass = SimpleNamespace(
        data={"map_tiles": deque([_OLD_TOKEN, _NEW_TOKEN], maxlen=2)}
    )

    assert map_view._map_tiles_access_token(hass) == _NEW_TOKEN


_FAIL_OPEN_HASS = (
    pytest.param(SimpleNamespace(), id="no-data-attribute"),
    pytest.param(SimpleNamespace(data={}), id="no-map-tiles-key"),
)

_FAIL_OPEN_VALUES = (
    pytest.param(deque(), id="empty-deque"),
    pytest.param([], id="empty-list"),
    pytest.param("abc", id="bare-string"),
    pytest.param([1, 2], id="non-string-element"),
    pytest.param([""], id="empty-token"),
    pytest.param(["</script><script>x</script>"], id="markup-token"),
    pytest.param(["é" * 64], id="unicode-alnum"),
    pytest.param(None, id="none"),
)


@pytest.mark.parametrize("hass", _FAIL_OPEN_HASS)
def test_map_tiles_access_token_fails_open_without_core_data(hass: Any) -> None:
    """No ``data`` or no key means no proxy, never an error."""

    assert map_view._map_tiles_access_token(hass) is None


@pytest.mark.parametrize("value", _FAIL_OPEN_VALUES)
def test_map_tiles_access_token_fails_open(value: Any) -> None:
    """Anything but a non-empty sequence of ASCII alphanumeric strings is ignored.

    The ``unicode-alnum`` case separates the ``isascii()`` arm from the
    ``isalnum()`` arm: ``"é"`` is alphanumeric in Unicode but not a Core
    token, and a token is embedded into the page verbatim.
    """

    hass = SimpleNamespace(data={"map_tiles": value})

    assert map_view._map_tiles_access_token(hass) is None


# --------------------------- contract with Core ---------------------------


def test_map_tiles_contract_matches_core() -> None:
    """The mirrored key, attribution, zoom limit and raster path match Core.

    Runs the probe as a clean child interpreter: ``tests/conftest.py`` stubs
    ``homeassistant.const`` without ``__version__``, which ``map_tiles`` imports,
    so an ``importorskip`` in this process would be skipped on every Core.
    Skipped only when the installed Core has no ``map_tiles`` at all.
    """

    completed = subprocess.run(
        [sys.executable, str(_MAP_TILES_PROBE)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        # Only a Core that has no map_tiles package at all is a skip; a Core
        # that renamed a constant or a module inside it is exactly the break
        # this test exists to report.
        if "No module named 'homeassistant.components.map_tiles'" in completed.stderr:
            pytest.skip("Core without map_tiles")
        pytest.fail(f"probe failed (rc={completed.returncode}):\n{completed.stderr}")

    core = json.loads(completed.stdout)

    assert core["key"] == map_view._MAP_TILES_DATA_KEY
    assert core["attribution"] == map_view._OSM_ATTRIBUTION
    assert core["raster_max_zoom"] == map_view._MAP_TILES_RASTER_MAX_NATIVE_ZOOM
    # Core's route carries aiohttp regex constraints (``{z:[0-9]+}``); the page
    # template uses the bare placeholders Leaflet substitutes.
    assert (
        re.sub(r":\[0-9\]\+", "", core["raster_url"])
        == map_view._MAP_TILES_RASTER_URL.split("?")[0]
    )
    # A real Core token passes the alphanumeric guard of the helper.
    hass = SimpleNamespace(
        data={"map_tiles": deque([secrets.token_hex(core["token_size"])])}
    )
    assert map_view._map_tiles_access_token(hass) is not None


# ------------------------------ rendered page ------------------------------

_OSM_FALLBACK_LINES = (
    "        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {\n",
    "            referrerPolicy: 'origin'\n        }).addTo(map);\n",
)


def _render_html(monkeypatch: pytest.MonkeyPatch, hass: Any) -> str:
    """Render real map HTML for the given ``hass`` with an empty Leaflet cache.

    The session fixture primes the real Leaflet assets; a fresh dict keeps the
    output short and never mutates the shared cache (see ``tests/conftest.py``).
    """

    monkeypatch.setattr(
        map_view, "_LEAFLET_CACHE", {"leaflet.css": "", "leaflet.js": ""}
    )
    view = map_view.GoogleFindMyMapView(hass)
    now = datetime(2024, 1, 1, tzinfo=UTC)
    return view._generate_map_html("MyPhone", [], "device-1", now, now, 0)


def _hass_with_proxy(*tokens: str) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(language="en"),
        data={"map_tiles": deque(tokens, maxlen=2)},
    )


def test_proxy_branch_uses_core_tiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a Core token the page loads tiles from this instance, newest token only."""

    html = _render_html(monkeypatch, _hass_with_proxy(_OLD_TOKEN, _NEW_TOKEN))

    assert "L.tileLayer('/api/map_tiles/raster/{z}/{x}/{y}.png?token={token}'" in html
    assert f'token: "{_NEW_TOKEN}"' in html
    assert _OLD_TOKEN not in html
    assert "tile.openstreetmap.org" not in html
    # Core serves the page with ``Referrer-Policy: no-referrer``; the tile layer
    # and the refresh fetch pin ``origin`` on top, so the page URL (with the
    # share token) is never sent as Referer regardless of that header
    assert html.count("referrerPolicy: 'origin'") == 2
    # Leaflet substitutes ``{token}`` from the options; the Core route rejects
    # zoom levels above 19.
    assert "maxNativeZoom: 19" in html
    # token refresh for pages that outlive the 30 min rotation
    assert "gfmyTileLayer.on('tileerror'" in html
    assert "GFMY_TILE_TOKEN_REFRESH_THROTTLE_MS = 30000" in html
    # share token in a header, never in the URL (access logs record the request line)
    assert "fetch('/api/googlefindmy/map_tiles_token', {" in html
    assert "headers: { 'X-GoogleFindMy-Map-Token': shareToken }" in html
    assert "map_tiles_token?token=" not in html
    assert "gfmyTileLayer.redraw()" in html
    # attribution: Core wording with the copyright link
    assert 'href="https://www.openstreetmap.org/copyright"' in html
    assert "OpenStreetMap</a> contributors" in html


_FALLBACK_HASS = (
    pytest.param(SimpleNamespace(config=SimpleNamespace(language="en")), id="no-data"),
    pytest.param(
        SimpleNamespace(config=SimpleNamespace(language="en"), data={}), id="no-key"
    ),
    *(
        pytest.param(
            SimpleNamespace(
                config=SimpleNamespace(language="en"), data={"map_tiles": value}
            ),
            id=f"value-{param.id}",
        )
        for param in _FAIL_OPEN_VALUES
        for value in param.values
    ),
)


@pytest.mark.parametrize("hass", _FALLBACK_HASS)
def test_fallback_branch_is_direct_osm(
    monkeypatch: pytest.MonkeyPatch, hass: Any
) -> None:
    """Without a usable Core token the page loads tiles directly from OSM.

    The ``referrerPolicy`` and ``addTo`` lines are pinned byte for byte. Two
    changes against the previous page are intended and pinned as well: the
    tile host is ``tile.openstreetmap.org`` without the ``{s}`` subdomain (the
    one hostname the OSMF tile usage policy names) and the attribution carries
    the copyright link in the Core wording.
    """

    html = _render_html(monkeypatch, hass)

    for line in _OSM_FALLBACK_LINES:
        assert line in html
    assert "api/map_tiles" not in html
    assert "tileerror" not in html
    assert "map_tiles_token" not in html
    assert 'href="https://www.openstreetmap.org/copyright"' in html
    assert "OpenStreetMap</a> contributors" in html


def test_core_token_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rendering with a Core token leaves the token out of every log line."""

    caplog.set_level(logging.DEBUG)

    html = _render_html(monkeypatch, _hass_with_proxy(_OLD_TOKEN, _NEW_TOKEN))

    assert _NEW_TOKEN in html
    assert _NEW_TOKEN not in caplog.text
    assert _OLD_TOKEN not in caplog.text


# ----------------------------- token endpoint -----------------------------


class _StubConfigEntries:
    """Minimal ``config_entries`` for ``_resolve_entry_by_token``."""

    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


def _endpoint_hass(
    *, entries: list[Any], map_tiles_tokens: list[str] | None
) -> SimpleNamespace:
    data: dict[str, Any] = {"core.uuid": "test-ha"}
    if map_tiles_tokens is not None:
        data["map_tiles"] = deque(map_tiles_tokens, maxlen=2)
    return SimpleNamespace(data=data, config_entries=_StubConfigEntries(entries))


def _share_token(hass: SimpleNamespace, entry: Any) -> str:
    return map_token_hex_digest(
        map_token_secret_seed(hass.data["core.uuid"], entry.entry_id, False)
    )


_HEADER = "X-GoogleFindMy-Map-Token"


async def _call_endpoint(
    hass: SimpleNamespace,
    headers: dict[str, str],
    query: dict[str, str] | None = None,
) -> Any:
    view = map_view.GoogleFindMyMapTilesTokenView(hass)
    return await view.get(SimpleNamespace(headers=headers, query=query or {}))


def test_share_token_header_name_is_pinned() -> None:
    """The header name is part of the page contract; the rendered JS uses the same one."""

    assert map_view._MAP_TILES_TOKEN_HEADER == _HEADER


@pytest.mark.asyncio
async def test_token_endpoint_returns_newest_core_token() -> None:
    """A valid share token yields the newest Core token as uncacheable JSON."""

    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=[_OLD_TOKEN, _NEW_TOKEN])

    response = await _call_endpoint(hass, {_HEADER: _share_token(hass, entry)})

    assert response.status == 200
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {"token": _NEW_TOKEN}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


@pytest.mark.asyncio
async def test_token_endpoint_ignores_share_token_in_query() -> None:
    """A share token in the URL no longer authenticates: URLs end up in access logs.

    The endpoint reads the header only, so a valid token passed the old way
    (``?token=``) is treated as absent and gets the same 401.
    """

    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=[_NEW_TOKEN])

    response = await _call_endpoint(
        hass, {}, query={"token": _share_token(hass, entry)}
    )

    assert response.status == 401
    assert response.body is None
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_token_endpoint_rejects_missing_share_token() -> None:
    """No share token: 401 with an empty body and the no-store headers."""

    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=[_NEW_TOKEN])

    response = await _call_endpoint(hass, {})

    assert response.status == 401
    assert response.body is None
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_token_endpoint_rejects_unknown_share_token() -> None:
    """A share token that matches no entry is a 401, not a lookup."""

    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=[_NEW_TOKEN])

    response = await _call_endpoint(hass, {_HEADER: "f" * 64})

    assert response.status == 401
    assert response.body is None


@pytest.mark.asyncio
async def test_token_endpoint_is_404_without_core_proxy() -> None:
    """A valid share token on a Core without ``map_tiles`` is a 404."""

    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=None)

    response = await _call_endpoint(hass, {_HEADER: _share_token(hass, entry)})

    assert response.status == 404
    assert response.body is None
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_token_endpoint_checks_auth_before_availability() -> None:
    """An invalid share token cannot learn whether the proxy exists (401, not 404)."""

    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=None)

    response = await _call_endpoint(hass, {_HEADER: "f" * 64})

    assert response.status == 401


@pytest.mark.asyncio
async def test_token_endpoint_does_not_log_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the Core token nor the share token reaches the log."""

    caplog.set_level(logging.DEBUG)
    entry = make_config_entry(entry_id="entry-id")
    hass = _endpoint_hass(entries=[entry], map_tiles_tokens=[_NEW_TOKEN])
    share = _share_token(hass, entry)

    response = await _call_endpoint(hass, {_HEADER: share})

    assert response.status == 200
    assert _NEW_TOKEN not in caplog.text
    assert share not in caplog.text


def test_token_view_is_registered_next_to_the_map_views() -> None:
    """The lazy import in the integration resolves the token view to the real class.

    Compared by module, name and route rather than by identity: other test
    modules reload ``map_view`` (see ``test_map_view_features``), so the
    integration may hold an earlier module object. A placeholder would fail
    all three checks (it has no ``url``).
    """

    integration = importlib.import_module("custom_components.googlefindmy")
    integration._ensure_runtime_imports()
    cls = integration.GoogleFindMyMapTilesTokenView

    assert cls.__module__ == "custom_components.googlefindmy.map_view"
    assert cls.__name__ == "GoogleFindMyMapTilesTokenView"
    assert cls.url == map_view._MAP_TILES_TOKEN_REFRESH_PATH
