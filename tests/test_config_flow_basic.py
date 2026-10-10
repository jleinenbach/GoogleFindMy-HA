# tests/test_config_flow_basic.py
"""Basic config flow import and initialization coverage."""

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


def test_flow_module_import_and_handler_registry() -> None:
    """Import the config flow module and verify handler registration."""

    from homeassistant import config_entries as config_entries_module

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    assert hasattr(config_flow, "ConfigFlow"), "ConfigFlow class missing after import"

    handler_registry = getattr(config_entries_module, "HANDLERS", None)
    assert handler_registry is not None, "ConfigEntries module did not expose HANDLERS"

    handler = handler_registry.get(DOMAIN)
    assert handler is config_flow.ConfigFlow
    assert handler.__name__ == "ConfigFlow"
    assert getattr(handler, "domain", None) == DOMAIN


def test_supported_subentry_types_returns_empty_to_hide_ui() -> None:
    """Config flow should return empty dict to hide subentry UI buttons."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    entry = SimpleNamespace(
        entry_id="entry-test",
        data={},
        options={},
        subentries={},
    )

    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(entry)  # type: ignore[arg-type]

    # Must return empty dict to hide "Add hub feature group" and
    # "Add service feature group" buttons in the HA config entry UI.
    # Subentries are provisioned programmatically, not manually by users.
    assert mapping == {}, "UI should not expose manual subentry buttons"


def test_subentry_update_constructor_allows_config_entry_and_subentry() -> None:
    """Update flows must accept both the config entry and an existing subentry."""

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    config_subentry_cls = getattr(config_flow, "ConfigSubentry", None)
    if config_subentry_cls is None:
        pytest.skip("Config subentry helpers unavailable in this environment")

    entry = SimpleNamespace(
        entry_id="entry-update",
        data={},
        options={},
        subentries={},
    )

    try:
        subentry = config_subentry_cls(
            data={"group_key": config_flow.SERVICE_SUBENTRY_KEY},
            subentry_type=config_flow.SUBENTRY_TYPE_SERVICE,
            title="Service",
            unique_id="update-service",
            subentry_id="service-subentry-id",
        )
    except TypeError as exc:  # pragma: no cover - legacy constructor signature
        pytest.skip(f"Config subentry constructor unavailable: {exc}")

    entry.subentries[subentry.subentry_id] = subentry

    flow = config_flow.ServiceSubentryFlowHandler(entry, subentry)

    assert getattr(flow, "config_entry", None) is entry
    assert getattr(flow, "subentry", None) is subentry


@pytest.fixture(name="hass")
async def hass_fixture(
    hass_executor_stub: Callable[..., SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> SimpleNamespace:
    """Return a minimal Home Assistant stub with a flow manager.

    Built through ``hass_executor_stub`` because ``async_step_user`` runs the
    local-bundle preflight, which reads the watched paths through
    ``hass.async_add_executor_job``; a double without that method would make the
    step silently skip the scan and this smoke test would stop covering it.

    The watch paths are pinned to an empty ``tmp_path`` so the scan stays
    hermetic: unpinned it would read the real installation paths, and
    ``docker-login/data/secrets.json`` is exactly where a developer's own
    credentials sit.
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
async def test_flow_can_init_user(hass: SimpleNamespace) -> None:
    """Ensure the user step initializes without invalid handler errors."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    assert result["type"] in {"form", "abort"}
