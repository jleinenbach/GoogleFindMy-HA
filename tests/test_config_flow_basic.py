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


def test_subentry_factories_return_configured_flows() -> None:
    """Ensure subentry mapping returns factories that instantiate configured flows."""

    import custom_components.googlefindmy.config_flow as config_flow  # noqa: PLC0415

    entry = SimpleNamespace(
        entry_id="entry-test",
        data={},
        options={},
        subentries={},
    )

    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(entry)  # type: ignore[arg-type]

    assert mapping, "Expected supported subentry mapping to include at least service/tracker flows"

    for key, factory in mapping.items():
        assert callable(factory), f"Factory for subentry '{key}' should be callable"
        first = factory()
        second = factory()
        assert isinstance(first, config_flow.ConfigSubentryFlow)
        assert isinstance(second, config_flow.ConfigSubentryFlow)
        assert first is not second, f"Factory for subentry '{key}' must yield a new flow instance per call"
        assert getattr(first, "config_entry", None) is entry
        assert getattr(second, "config_entry", None) is entry


def test_subentry_update_constructor_allows_config_entry_and_subentry() -> None:
    """Update flows must accept both the config entry and an existing subentry."""

    import custom_components.googlefindmy.config_flow as config_flow  # noqa: PLC0415

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
