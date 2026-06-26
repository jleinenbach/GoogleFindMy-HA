# tests/test_diagnostics_crypto.py
"""Diagnostics ``crypto`` block: ECDSA acceleration backend + installed versions.

``async_setup`` caches ``get_ecdsa_acceleration_info()`` under
``hass.data[DOMAIN]["ecdsa_acceleration_info"]``; the diagnostics dump reads it
synchronously and exposes it as an optional ``crypto`` block (present once
async_setup has materialized it, mirroring the other optional blocks). These
tests use ``@pytest.mark.asyncio`` and ``await`` directly (tests/AGENTS.md:
never ``asyncio.run`` in tests).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.diagnostics import _crypto_block
from tests.helpers.config_entries_stub import make_config_entry

# Each async test below carries an explicit ``@pytest.mark.asyncio`` marker
# (required by tests/test_guard_async_test_marker.py); the one sync test stays
# unmarked, so no module-level ``pytestmark`` is used.

_SENTINEL_INFO: dict[str, str | None] = {
    "ecdsa_acceleration": "gmpy2",
    "gmpy2_version": "9.9.9-test",
    "gmpy_version": None,
    "ecdsa_version": "0.19.1",
}


def _patch_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize loader and registry lookups so the dump builds in isolation."""

    async def _fake_get_integration(_hass: Any, _domain: str) -> SimpleNamespace:
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )


def _make_entry_and_hass(
    domain_bucket: dict[str, Any],
) -> tuple[SimpleNamespace, SimpleNamespace]:
    # Canonical config-entry stub (tests/AGENTS.md: new tests MUST use
    # make_config_entry, never ad-hoc SimpleNamespace entries). It guarantees
    # data/options are real dicts; the hass object stays a local SimpleNamespace.
    entry = make_config_entry(
        domain=DOMAIN,
        runtime_data=SimpleNamespace(coordinator=None),
    )
    hass = SimpleNamespace(data={DOMAIN: domain_bucket})
    return entry, hass


@pytest.mark.asyncio
async def test_crypto_block_present_when_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loader(monkeypatch)
    entry, hass = _make_entry_and_hass(
        {"ecdsa_acceleration_info": dict(_SENTINEL_INFO)}
    )
    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    crypto = payload["crypto"]
    assert crypto["ecdsa_acceleration"] in {"gmpy2", "gmpy", "pure-python"}
    assert set(crypto) == {
        "ecdsa_acceleration",
        "gmpy2_version",
        "gmpy_version",
        "ecdsa_version",
    }
    assert crypto["gmpy2_version"] == "9.9.9-test"
    # Mutation counter-check: dropping a version key in _crypto_block makes the
    # set-equality assertion red.


@pytest.mark.asyncio
async def test_crypto_block_absent_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_loader(monkeypatch)
    entry, hass = _make_entry_and_hass({})
    payload = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    # Optional block: absent until async_setup has materialized the cache.
    assert "crypto" not in payload


def test_crypto_block_is_shallow_copy() -> None:
    """``_crypto_block`` must copy, never return the cached dict itself."""
    src: dict[str, str | None] = dict(_SENTINEL_INFO)
    out = _crypto_block(src)
    assert out == src
    assert out is not src
    out["ecdsa_acceleration"] = "mutated"
    assert src["ecdsa_acceleration"] == "gmpy2"
