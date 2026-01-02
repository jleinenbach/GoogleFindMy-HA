"""FMDN Finder - Upload location reports to Google FMDN Network.

This module implements the "Finder" role in Google's Find My Device Network (FMDN).
It receives BLE beacons from FMDN devices (via Bermuda integration), encrypts their
location, and uploads reports to Google's backend.

Architecture:
- bermuda_listener.py: Listens to Bermuda FMDN beacon detections
- location_uploader.py: Encrypts location data using existing crypto primitives
- google_uploader.py: Handles HTTP uploads to Google FMDN backend

Privacy & Security:
- End-to-end encryption (only device owner can decrypt)
- Upload throttling per FMDN specification
- Opt-in only (respects user privacy)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

__all__ = ["async_setup_fmdn_finder"]


async def async_setup_fmdn_finder(hass: HomeAssistant) -> bool:
    """Initialize FMDN Finder functionality.

    Args:
        hass: Home Assistant instance

    Returns:
        True if setup successful, False otherwise
    """
    from .bermuda_listener import async_setup_bermuda_listener  # noqa: PLC0415

    _LOGGER.info("Setting up FMDN Finder integration")

    try:
        # Setup Bermuda event listener
        await async_setup_bermuda_listener(hass)
        _LOGGER.info("FMDN Finder setup complete")
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Failed to setup FMDN Finder: %s", err, exc_info=True)
        return False


async def async_unload_fmdn_finder(hass: HomeAssistant) -> bool:
    """Unload FMDN Finder functionality.

    Args:
        hass: Home Assistant instance

    Returns:
        True if unload successful
    """
    from .bermuda_listener import async_unload_bermuda_listener  # noqa: PLC0415

    _LOGGER.info("Unloading FMDN Finder integration")

    try:
        await async_unload_bermuda_listener(hass)
        _LOGGER.info("FMDN Finder unloaded successfully")
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Error during FMDN Finder unload: %s", err)
        return False
