# tests/test_map_view_tiles.py
"""Map tiles through the Core ``map_tiles`` proxy, with OpenStreetMap as fallback.

Home Assistant Core >= 2026.9 ships ``map_tiles``, a system component that
proxies OpenStreetMap tiles with an application ``User-Agent``, a contact
address and a server-side cache, and that authenticates browser requests with
a short-lived query token rotated every 30 minutes. The map view renders the
tile layer in one of two ways:

* **Proxy branch:** when ``hass.data["map_tiles"]`` holds a token deque, the
  page loads ``/api/map_tiles/raster/{z}/{x}/{y}.png?token=...`` (root-relative,
  same origin, no ``referrerPolicy``) and refreshes the token through a
  share-token guarded endpoint when tiles start failing.
* **Fallback branch:** on any other Core the page keeps loading tiles directly
  from ``tile.openstreetmap.org`` exactly as before, so a Core that renames or
  drops the component degrades to today's behaviour, never to an empty map.

These tests pin the helper's fail-open rule, the contract with the real Core
constants (as a subprocess, see ``tests/fixtures/map_tiles_probe``), and later
the two rendered branches and the token endpoint.
"""

from __future__ import annotations

import json
import pathlib
import re
import secrets
import subprocess
import sys
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import map_view

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
