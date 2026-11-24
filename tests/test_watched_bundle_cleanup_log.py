# tests/test_watched_bundle_cleanup_log.py
"""Log lines of the delete-after-import cleanup of watched bundles.

The cleanup runs in the background after an entry was stored, so a failure
can only be read from the log. These tests pin that the warning names the
entry and tells the user what to do, and that a bundle kept because it is not
older says what happens to it next.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow, discovery
from custom_components.googlefindmy.const import DOMAIN

_EMAIL = "user@example.com"


class _Hass:
    """Just enough of Home Assistant for the cleanup path."""

    def __init__(self, paths: list[Path]) -> None:
        self.data: dict[str, Any] = {
            DOMAIN: {"discovery_manager": SimpleNamespace(watch_paths=tuple(paths))}
        }

    async def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)


def _bundle(token: str) -> dict[str, Any]:
    return {"google_email": _EMAIL, "aas_token": token}


def _write(path: Path, token: str, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_bundle(token)), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _job(token: str) -> Any:
    return config_flow.PendingContainerCleanup(
        imported_stable_key=discovery._cloud_discovery_stable_key(_EMAIL, None, None),
        imported_digest=discovery.secrets_bundle_digest(_bundle(token)),
    )


@pytest.fixture
def persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The storage proof succeeds at once; this is not what is tested here."""

    async def _probe(
        _hass: Any, _entry_id: str, *, min_modified_at: Any = None
    ) -> bool:
        return True

    monkeypatch.setattr(config_flow, "_async_config_entry_is_persisted", _probe)


@pytest.mark.asyncio
async def test_a_failed_delete_names_the_entry_and_the_remedy(
    persisted: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning with the entry id and the hand step; the file stays."""

    watched = tmp_path / "data" / "secrets.json"
    _write(watched, "aas_et/RO", 1_000)

    def _refuse(path: str) -> None:
        raise PermissionError(path)

    monkeypatch.setattr(config_flow.os, "remove", _refuse)

    with caplog.at_level(logging.WARNING, logger=config_flow.__name__):
        await config_flow._async_run_container_cleanup_when_persisted(
            _Hass([watched]), "entry-4711", [_job("aas_et/RO")]
        )

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "Failed to remove watched bundle file" in record.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "entry-4711" in message
    assert "delete the file by hand" in message
    assert watched.exists()


@pytest.mark.asyncio
async def test_after_import_no_bundle_of_that_account_is_left(
    persisted: None, tmp_path: Path
) -> None:
    """The imported copy and an older copy of the account are both removed."""

    imported = tmp_path / "data" / "secrets.json"
    older = tmp_path / "auth" / "secrets.json"
    _write(imported, "aas_et/NEW", 2_000)
    _write(older, "aas_et/OLD", 1_000)

    await config_flow._async_run_container_cleanup_when_persisted(
        _Hass([imported, older]), "entry-1", [_job("aas_et/NEW")]
    )

    assert not imported.exists()
    assert not older.exists()


@pytest.mark.asyncio
async def test_a_kept_newer_bundle_says_it_becomes_a_new_card(
    persisted: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bundle that is not older is kept, and the log says it is offered again."""

    imported = tmp_path / "data" / "secrets.json"
    newer = tmp_path / "auth" / "secrets.json"
    _write(imported, "aas_et/OLD", 1_000)
    _write(newer, "aas_et/NEW", 2_000)

    with caplog.at_level(logging.INFO, logger=config_flow.__name__):
        await config_flow._async_run_container_cleanup_when_persisted(
            _Hass([imported, newer]), "entry-1", [_job("aas_et/OLD")]
        )

    assert newer.exists()
    kept = [
        record.getMessage()
        for record in caplog.records
        if "Keeping watched secrets bundle: it is not older" in record.getMessage()
    ]
    assert len(kept) == 1
    assert "offered as a new discovery card" in kept[0]
