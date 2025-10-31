# tests/test_config_flow_basic.py
"""Basic config flow import and initialization coverage."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from custom_components.googlefindmy.const import DOMAIN


def test_flow_module_import_and_handler_registry() -> None:
    """Import the config flow module and verify handler registration."""

    import custom_components.googlefindmy.config_flow as config_flow  # noqa: PLC0415
    from homeassistant import config_entries as config_entries_module

    assert hasattr(config_flow, "ConfigFlow"), "ConfigFlow class missing after import"

    handler_registry = getattr(config_entries_module, "HANDLERS", None)
    assert handler_registry is not None, "ConfigEntries module did not expose HANDLERS"

    handler = handler_registry.get(DOMAIN)
    assert handler is config_flow.ConfigFlow
    assert handler.__name__ == "ConfigFlow"
    assert getattr(handler, "domain", None) == DOMAIN


@pytest.fixture(name="hass")
def hass_fixture() -> SimpleNamespace:
    """Return a minimal Home Assistant stub with a flow manager."""

    import custom_components.googlefindmy.config_flow as config_flow  # noqa: PLC0415

    hass = SimpleNamespace(data={})

    class _FlowManager:
        def __init__(self, hass_obj: SimpleNamespace) -> None:
            self._hass = hass_obj

        async def async_init(
            self,
            domain: str,
            *,
            context: Mapping[str, Any] | None = None,
        ) -> Mapping[str, Any]:
            assert domain == DOMAIN

            flow = config_flow.ConfigFlow()
            flow.hass = self._hass  # type: ignore[assignment]
            flow.context = dict(context or {})
            result = await flow.async_step_user(None)
            if inspect.isawaitable(result):
                result = await result
            return result

    hass.config_entries = SimpleNamespace(flow=_FlowManager(hass))
    return hass


@pytest.mark.asyncio
async def test_flow_can_init_user(hass: SimpleNamespace) -> None:
    """Ensure the user step initializes without invalid handler errors."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    assert result["type"] in {"form", "abort"}
