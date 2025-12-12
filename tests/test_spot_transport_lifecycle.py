from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import custom_components.googlefindmy as integration_module


@pytest.mark.asyncio
async def test_close_only_when_bucket_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport closes only when the entries bucket is empty."""

    bucket: dict[str, object] = {"one": object()}
    transport = AsyncMock()
    monkeypatch.setattr(integration_module, "SPOT_GRPC_TRANSPORT", transport)

    await integration_module._maybe_close_spot_transport(bucket)
    transport.async_close.assert_not_awaited()

    bucket.clear()
    await integration_module._maybe_close_spot_transport(bucket)
    transport.async_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_skipped_when_entries_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing is skipped if another entry remains loaded."""

    bucket: dict[str, object] = {"one": object(), "two": object()}
    transport = AsyncMock()
    monkeypatch.setattr(integration_module, "SPOT_GRPC_TRANSPORT", transport)

    await integration_module._maybe_close_spot_transport(bucket)
    transport.async_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_entry_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removal path should close the transport after popping the entry."""

    bucket: dict[str, object] = {"only": object()}
    transport = AsyncMock()
    monkeypatch.setattr(integration_module, "SPOT_GRPC_TRANSPORT", transport)

    bucket.pop("only", None)
    await integration_module._maybe_close_spot_transport(bucket)

    transport.async_close.assert_awaited_once()
