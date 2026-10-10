# tests/test_map_view_blocking_io.py
"""Regression coverage: the map page must not read Leaflet in the event loop.

Home Assistant reported ``Detected blocking call to read_text`` from
``map_view._leaflet_asset`` (``homeassistant/util/loop.py``): the first map
request of a process filled the asset cache from inside the request handler,
which runs in the loop. The assets are now read in the executor before the HTML
is built.

The decisive test measures the *thread* the read happens on, not that some
helper was called: a helper called from the loop would satisfy a call counter
while reproducing the exact defect.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
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

DEVICE_ID = "device123"


class _StubConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


class _StubRegistryEntry:
    def __init__(self, *, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id
        self.device_id: str | None = None
        self.platform = DOMAIN


class _StubEntityRegistry:
    def __init__(self, entries: list[_StubRegistryEntry]) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        for entry in self.entities.values():
            if (
                entry.platform == platform
                and entry.unique_id == unique_id
                and entry.entity_id.startswith(f"{domain}.")
            ):
                return entry.entity_id
        return None

    def async_get(self, entity_id: str) -> _StubRegistryEntry | None:
        return self.entities.get(entity_id)


class _ThreadingHass:
    """Hass stand-in whose executor really leaves the event loop thread."""

    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {"core.uuid": "test-ha"}
        self.config_entries = _StubConfigEntries(entries)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)


def _make_request_context(
    monkeypatch: pytest.MonkeyPatch,
    language: str | None = None,
) -> tuple[_ThreadingHass, SimpleNamespace]:
    """Return a hass stand-in and an authorized request for the map view.

    ``language`` attaches a ``hass.config`` carrying that UI language. Left at
    ``None`` the stand-in has no ``config`` at all, which is the degraded shape
    ``_resolve_language`` has to survive, so the existing cases keep exercising
    the English fallback.
    """

    coordinator = SimpleNamespace(data=[{"id": DEVICE_ID, "name": "Test Device"}])
    entry = make_config_entry(entry_id="entry-id", runtime_data=coordinator)
    hass = _ThreadingHass([entry])
    if language is not None:
        hass.config = SimpleNamespace(language=language)

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", lambda: SimpleNamespace)

    registry = _StubEntityRegistry(
        [
            _StubRegistryEntry(
                entity_id=f"device_tracker.{DEVICE_ID}",
                unique_id=f"{entry.entry_id}:{DEVICE_ID}",
                config_entry_id=entry.entry_id,
            )
        ]
    )
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    secret = map_token_secret_seed(hass.data["core.uuid"], entry.entry_id, False)
    request = SimpleNamespace(query={"token": map_token_hex_digest(secret)})
    return hass, request


def test_leaflet_asset_raises_on_a_cold_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miss must fail loudly instead of reading through to the disk.

    A read-through fallback would restore the reported defect and hide it at the
    same time: it fires once per process, so the warning would be rare enough to
    look fixed.
    """

    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {})

    with pytest.raises(RuntimeError, match="leaflet.css"):
        map_view._leaflet_asset("leaflet.css")


@pytest.mark.asyncio
async def test_map_request_reads_the_assets_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every vendored asset is read from a worker thread, never from the loop."""

    hass, request = _make_request_context(monkeypatch)
    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {})

    loop_thread = threading.get_ident()
    # (file name, reading thread) per read, so an unrelated ``read_text``
    # elsewhere in the request path can neither satisfy nor break this
    # assertion by mere count.
    reads: list[tuple[str, int]] = []
    original_read_text = Path.read_text

    def _recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append((self.name, threading.get_ident()))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _recording_read_text)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()

    leaflet_reads = [entry for entry in reads if entry[0] in map_view._LEAFLET_ASSETS]
    # Every vendored asset was read exactly once ...
    assert sorted(name for name, _thread in leaflet_reads) == sorted(
        map_view._LEAFLET_ASSETS
    )
    # ... and none of those reads happened on the event loop thread.
    assert all(thread != loop_thread for _name, thread in leaflet_reads)


@pytest.mark.asyncio
async def test_second_map_request_does_not_read_the_assets_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm cache serves the page without touching the disk at all."""

    hass, request = _make_request_context(monkeypatch)
    monkeypatch.setattr(
        map_view, "_LEAFLET_CACHE", dict(map_view._read_leaflet_assets())
    )

    reads: list[str] = []
    original_read_text = Path.read_text

    def _recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _recording_read_text)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 200
    assert reads == []


@pytest.mark.asyncio
async def test_a_partially_filled_cache_still_reads_the_missing_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache holding only one asset must not count as primed.

    This is the shape of the original defect: the CSS was cached on the first
    request while the JS was still read later, from the loop. The guard checks
    every asset, not whether the dict is non-empty.
    """

    hass, request = _make_request_context(monkeypatch)
    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {"leaflet.css": "/* cached */"})

    loop_thread = threading.get_ident()
    reads: list[tuple[str, int]] = []
    original_read_text = Path.read_text

    def _recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append((self.name, threading.get_ident()))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _recording_read_text)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 200
    leaflet_reads = [entry for entry in reads if entry[0] in map_view._LEAFLET_ASSETS]
    assert [name for name, _thread in leaflet_reads] == list(map_view._LEAFLET_ASSETS)
    assert all(thread != loop_thread for _name, thread in leaflet_reads)


@pytest.mark.asyncio
async def test_a_missing_vendor_directory_is_answered_from_the_real_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the real reader against a real failure, not a stubbed raiser.

    The parametrized cases below replace ``_read_leaflet_assets`` and therefore
    only prove the ``except`` clause. This one points the module at an empty
    directory, so the failure travels the production path: ``read_text`` raises,
    the executor propagates it, and the handler turns it into a page.
    """

    hass, request = _make_request_context(monkeypatch)
    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {})
    monkeypatch.setattr(map_view, "_LEAFLET_DIR", tmp_path)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 500
    assert "Map unavailable" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        # Vendor directory stripped or half-written by an interrupted update.
        FileNotFoundError("vendor/leaflet/leaflet.css"),
        # Wrong ownership after a container/user change.
        PermissionError("vendor/leaflet/leaflet.css"),
        # The file exists but its bytes are corrupted. This one is a
        # ``ValueError``, not an ``OSError``, and used to escape the handler.
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["missing", "unreadable", "corrupted"],
)
async def test_unreadable_assets_answer_with_a_page_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A half-written install yields HTTP 500 with a message, not an exception."""

    hass, request = _make_request_context(monkeypatch)
    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {})

    def _raise() -> dict[str, str]:
        raise failure

    monkeypatch.setattr(map_view, "_read_leaflet_assets", _raise)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 500
    assert "Map unavailable" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected_title", "expected_body_fragment", "expected_html_tag"),
    [
        # Plain locale: German text, German lang attribute, no direction flip.
        (
            "de",
            "Karte nicht verfügbar",
            "Installiere die Integration neu",
            '<html lang="de">',
        ),
        # Region-qualified: resolves to the base locale, attribute keeps the raw
        # request (same rule as ``_generate_map_html``).
        (
            "de-DE",
            "Karte nicht verfügbar",
            "Installiere die Integration neu",
            '<html lang="de-DE">',
        ),
        # RTL locale: the page must also be laid out right-to-left.
        (
            "he",
            "המפה אינה זמינה",
            "התקן מחדש את השילוב",
            '<html lang="he" dir="rtl">',
        ),
        # Unknown locale: English text, attribute still mirrors the request.
        (
            "xx",
            "Map unavailable",
            "Reinstall the integration",
            '<html lang="xx">',
        ),
    ],
    ids=["german", "german-region", "hebrew-rtl", "unknown-falls-back"],
)
async def test_the_asset_failure_page_is_localized(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    expected_title: str,
    expected_body_fragment: str,
    expected_html_tag: str,
) -> None:
    """The failure page speaks the configured UI language, not English.

    This page shows up precisely when the install is already broken, so an
    English-only wording would hit non-English users at their worst moment. The
    assertion is deliberately on the rendered text and the ``<html>`` tag rather
    than on a call to ``resolve_map_labels``: a mock would stay green even if the
    handler dropped the resolved labels on the floor again.
    """

    hass, request = _make_request_context(monkeypatch, language=language)
    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {})

    def _raise() -> dict[str, str]:
        raise FileNotFoundError("vendor/leaflet/leaflet.css")

    monkeypatch.setattr(map_view, "_read_leaflet_assets", _raise)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 500
    assert expected_title in response.text
    # The message, not just the heading: pinning the title alone would stay
    # green if the body fell back to the English literal.
    assert expected_body_fragment in response.text
    assert expected_html_tag in response.text


@pytest.mark.asyncio
async def test_a_hass_without_config_still_answers_in_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded hass keeps the page English and says so in the markup.

    ``_resolve_language`` returns ``None`` when ``hass.config`` is missing. The
    wording then falls back to English, and the page declares that language the
    same way ``_generate_map_html`` does, rather than shipping an empty
    ``lang=""`` or no language at all.
    """

    hass, request = _make_request_context(monkeypatch)
    assert not hasattr(hass, "config")
    monkeypatch.setattr(map_view, "_LEAFLET_CACHE", {})

    def _raise() -> dict[str, str]:
        raise FileNotFoundError("vendor/leaflet/leaflet.css")

    monkeypatch.setattr(map_view, "_read_leaflet_assets", _raise)

    response = await map_view.GoogleFindMyMapView(hass).get(
        request, device_id=DEVICE_ID
    )

    assert response.status == 500
    assert '<html lang="en">' in response.text
    assert 'lang=""' not in response.text
    assert "Map unavailable" in response.text
    assert "Reinstall the integration" in response.text


@pytest.mark.parametrize(
    ("title", "body", "language", "must_not_appear"),
    [
        # Markup in the message must not become markup in the page.
        ("<b>t</b>", "x", None, "<b>t</b>"),
        ("t", "<script>alert(1)</script>", None, "<script>"),
        # ``hass.config.language`` is a configuration value that lands inside a
        # double-quoted attribute; a quote in it must not open a new one.
        ("t", "b", 'de" onload="alert(1)', 'onload="alert(1)"'),
    ],
    ids=["title-markup", "body-markup", "language-attribute-break-out"],
)
def test_html_response_escapes_everything_it_interpolates(
    title: str, body: str, language: str | None, must_not_appear: str
) -> None:
    """Nothing handed to ``_html_response`` may reach the page as markup.

    Today every caller passes a literal or a catalog value, so this test guards
    a property rather than a live bug: without it, dropping ``escape()`` again
    would leave the whole suite green.
    """

    response = map_view._html_response(title, body, status=500, language=language)

    assert must_not_appear not in response.text
    assert response.status == 500
