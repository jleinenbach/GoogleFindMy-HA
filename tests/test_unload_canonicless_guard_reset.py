# tests/test_unload_canonicless_guard_reset.py
"""Unload re-arms the canonicless-device warning guard for the unloaded entry.

The decoder's canonicless-device warning guard is process-wide and is not cleared by
a Home Assistant config-entry reload (the module stays imported across a reload).
``async_unload_entry`` must therefore clear the guard for the scope being unloaded, so
a reload of a still-affected entry surfaces the default-visible WARNING again instead
of silently suppressing it. The guard key is scoped by the parent entry's token-cache
id, so a subentry unload clears the parent scope, not the subentry id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.googlefindmy as gfmy
from custom_components.googlefindmy.ProtoDecoders import decoder

pytestmark = pytest.mark.asyncio


async def test_unload_parent_entry_resets_guard_for_its_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unloading a parent entry clears the canonicless guard for that entry id."""
    calls: list[str | None] = []
    monkeypatch.setattr(
        decoder,
        "_reset_canonicless_warning_state",
        lambda entry_id=None: calls.append(entry_id),
    )

    async def _fake_parent(hass: object, entry: object) -> bool:
        return True

    monkeypatch.setattr(gfmy, "_async_unload_parent_entry", _fake_parent)

    entry = SimpleNamespace(entry_id="entry-P", parent_entry_id=None)
    result = await gfmy.async_unload_entry(object(), entry)

    assert result is True
    assert calls == ["entry-P"]


async def test_unload_subentry_resets_parent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unloading a subentry clears the parent scope, not the subentry id.

    The guard key uses the parent entry's token-cache id, so re-arming must target the
    parent scope; clearing the subentry id would be a silent no-op.
    """
    calls: list[str | None] = []
    monkeypatch.setattr(
        decoder,
        "_reset_canonicless_warning_state",
        lambda entry_id=None: calls.append(entry_id),
    )

    async def _fake_sub(hass: object, entry: object) -> bool:
        return True

    monkeypatch.setattr(gfmy, "_async_unload_subentry", _fake_sub)

    entry = SimpleNamespace(entry_id="sub-1", parent_entry_id="entry-P")
    result = await gfmy.async_unload_entry(object(), entry)

    assert result is True
    assert calls == ["entry-P"]
