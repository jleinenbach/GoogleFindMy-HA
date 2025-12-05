import logging
from collections.abc import Iterable

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.googlefindmy.const import DOMAIN


@pytest.mark.asyncio
async def test_async_setup_does_not_log_blocking_imports(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    enable_custom_integrations: None,
) -> None:
    caplog.set_level(logging.WARNING)
    initial_record_count = len(caplog.records)

    hass.config.components.add("http")

    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})

    new_records: Iterable[logging.LogRecord] = caplog.records[initial_record_count:]
    blocking_warnings = [
        record
        for record in new_records
        if record.levelno >= logging.WARNING
        and (
            "blocking call" in record.getMessage().lower()
            or "import_module" in record.getMessage().lower()
        )
    ]

    assert not blocking_warnings, blocking_warnings
