# tests/test_map_view_log_device_id.py
"""Request-supplied device ids never start a new log line (CWE-117).

``device_id`` is the decoded path segment of a public, unauthenticated route,
so ``%0A`` arrives as a real line break. The views log it at DEBUG; formatted
with ``%s`` it would let a caller forge extra log lines (one of the three call
sites runs before any token check). ``%r`` keeps the value on one line.
"""

from __future__ import annotations

import logging
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

_FORGED = "device123\n2026-10-11 ERROR forged line"


class _StubConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


class _StubHass:
    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {
            "core.uuid": "test-ha",
            "map_tiles": deque(["b" * 64], maxlen=2),
        }
        self.config_entries = _StubConfigEntries(entries)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _disabled_entry_and_token(hass_entries: list[Any]) -> tuple[Any, str]:
    entry = make_config_entry(
        entry_id="entry-id",
        runtime_data=None,
        options={OPT_MAP_VIEW_ENABLED: False},
    )
    hass_entries.append(entry)
    token = map_token_hex_digest(
        map_token_secret_seed("test-ha", entry.entry_id, False)
    )
    return entry, token


def _view_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == map_view._LOGGER.name
    ]


def _assert_single_line(messages: list[str], marker: str) -> None:
    hits = [message for message in messages if marker in message]
    assert hits, f"no log line containing {marker!r}: {messages!r}"
    for message in hits:
        assert "\n" not in message
        assert "\\n" in message


@pytest.mark.asyncio
async def test_token_mismatch_logs_device_id_on_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pre-auth mismatch branch keeps a forged line break escaped."""

    caplog.set_level(logging.DEBUG, logger=map_view._LOGGER.name)
    view = map_view.GoogleFindMyMapView(_StubHass([]))

    response = await view.get(
        SimpleNamespace(query={"token": "0" * 64}), device_id=_FORGED
    )

    assert response.status == 401
    _assert_single_line(_view_messages(caplog), "Map token mismatch")


@pytest.mark.asyncio
async def test_disabled_entry_logs_device_id_on_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disabled-entry refusal keeps a forged line break escaped."""

    caplog.set_level(logging.DEBUG, logger=map_view._LOGGER.name)
    entries: list[Any] = []
    _entry, token = _disabled_entry_and_token(entries)
    view = map_view.GoogleFindMyMapView(_StubHass(entries))

    response = await view.get(
        SimpleNamespace(query={"token": token}), device_id=_FORGED
    )

    assert response.status == 401
    _assert_single_line(_view_messages(caplog), "Map view disabled")


@pytest.mark.asyncio
async def test_redirect_logs_device_id_on_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The redirect view keeps a forged line break escaped."""

    caplog.set_level(logging.DEBUG, logger=map_view._LOGGER.name)
    view = map_view.GoogleFindMyMapRedirectView(_StubHass([]))

    with pytest.raises(map_view.web.HTTPFound):
        await view.get(SimpleNamespace(query={"token": "0" * 64}), device_id=_FORGED)

    _assert_single_line(_view_messages(caplog), "Relative redirect prepared")
