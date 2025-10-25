# tests/test_token_cache_secrets.py
"""Regression tests ensuring secrets bundles retain FCM identifiers."""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from types import ModuleType
from typing import Any


class _CapturingCache:
    """Minimal cache stub recording values written by the secrets migrator."""

    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        self.saved[name] = value


def test_async_save_secrets_data_preserves_gcm_identifiers() -> None:
    """android_id and security_token remain available after secrets migration."""

    if "homeassistant.loader" not in sys.modules:
        loader_module = ModuleType("homeassistant.loader")
        loader_module.async_get_integration = lambda *_args, **_kwargs: None
        sys.modules["homeassistant.loader"] = loader_module

    integration_init = import_module("custom_components.googlefindmy.__init__")

    cache = _CapturingCache()
    secrets_bundle = {
        "fcm_credentials": {
            "gcm": {
                "android_id": "1234567890",
                "security_token": "9876543210",
            },
            "fcm": {
                "registration": {"token": "cached-token"},
            },
        },
        "username": "user@example.com",
    }

    asyncio.run(integration_init._async_save_secrets_data(cache, secrets_bundle))

    assert "fcm_credentials" in cache.saved
    stored = cache.saved["fcm_credentials"]
    assert isinstance(stored, dict)
    gcm_block = stored["gcm"]
    assert gcm_block["android_id"] == "1234567890"
    assert gcm_block["security_token"] == "9876543210"
