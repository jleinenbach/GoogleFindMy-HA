# tests/test_config_flow_registration.py
"""Smoke tests ensuring the config flow registers correctly."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import DOMAIN
from tests.helpers.config_flow import (
    config_entries_flow_stub,
    prepare_flow_hass_config_entries,
)


def test_config_flow_import_and_registers_handler() -> None:
    """Import the config flow module and assert the handler metadata."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    assert hasattr(config_flow, "ConfigFlow"), "ConfigFlow class is missing"
    assert getattr(config_flow.ConfigFlow, "domain", None) == DOMAIN


@pytest.fixture(name="hass")
async def hass_fixture(
    hass_executor_stub: Callable[..., SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> SimpleNamespace:
    """Return a minimal hass stub with a flow manager.

    ``async_step_user`` scans the default watch paths through
    ``hass.async_add_executor_job``, so the double has to provide that method;
    otherwise the preflight is skipped and this smoke test no longer exercises
    it.

    The scan itself is pinned to an empty ``tmp_path``: unpinned it reads the
    real installation paths, and this smoke test asserts nothing about what it
    would find there.
    """

    from custom_components.googlefindmy import (
        config_flow,  # noqa: PLC0415
        discovery,  # noqa: PLC0415
    )

    monkeypatch.setattr(
        discovery,
        "_default_watch_paths",
        lambda: [tmp_path / "no-bundle" / "secrets.json"],
        raising=True,
    )

    hass = hass_executor_stub(data={})

    async def _async_init(
        domain: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        assert domain == DOMAIN

        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = dict(context or {})
        result = await flow.async_step_user(None)
        if inspect.isawaitable(result):
            result = await result
        return result

    prepare_flow_hass_config_entries(
        hass,
        lambda: config_entries_flow_stub(result=_async_init),
    )
    return hass


@pytest.mark.asyncio
async def test_flow_init_user(hass) -> None:
    """The user step should initialize without raising errors."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    assert result["type"] in {"form", "abort"}
