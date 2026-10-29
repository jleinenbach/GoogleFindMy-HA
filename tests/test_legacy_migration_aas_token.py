# tests/test_legacy_migration_aas_token.py
"""The legacy migration keeps the key material of an ``aas_et/`` import.

A ``secrets.json`` from GoogleFindMyTools carries an ``aas_et/`` token. The
config flow stores it as the entry's credential and puts the bundle next to
it. The first setup then runs the legacy migration (empty cache) and the
credential seed in the same call:

* the migration moves the bundle into the cache raw and removes it from the
  entry, but leaves ``aas_token`` in ``entry.data``;
* the seed sees an entry that still carries a credential but no bundle,
  reads that as "the user replaced the bundle" (Auth/AGENTS.md) and deletes
  the cached bundle before ``_async_save_secrets_data`` could derive
  ``owner_key``/``shared_key`` from it.

The OAuth import is not affected because the migration also removes
``CONF_OAUTH_TOKEN``; the entry then carries no credentials and the seed reads
the bundle back from the cache, which is the exception the seed contract
names. These tests pin both directions of that contract.

The harness replaces ``_async_save_secrets_data`` with a mock. The tests take
the real function before the first harness run and replay the bundle the seed
passed, so the cache shows what the next start would really hold (a mocked
store made an end-to-end test of PR #233 pass while the real path failed).
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
)
from tests.test_config_entry_startup import _run_credential_seed

EMAIL = "user@example.com"
AAS_TOKEN = "aas_et/MASTER_TOKEN_VALUE"
OAUTH_TOKEN = "oauth2_4/OAUTH_COOKIE_VALUE"


def _registration(token: str) -> dict[str, Any]:
    """Return a complete FCM registration carrying ``token``."""
    return {
        "gcm": {"android_id": "1234567890", "security_token": "9876543210"},
        "fcm": {"registration": {"token": token}},
    }


def _bundle() -> dict[str, Any]:
    """Return a secrets.json bundle as GoogleFindMyTools writes it."""
    return {
        "username": EMAIL,
        "owner_key": "OWNER_KEY_VALUE",
        "shared_key": "SHARED_KEY_VALUE",
        "aas_token": AAS_TOKEN,
        "fcm_credentials": _registration("bundle"),
    }


def _imported_entry(token: str) -> dict[str, Any]:
    """Return ``entry.data`` as the secrets.json import of the flow leaves it."""
    data: dict[str, Any] = {
        CONF_GOOGLE_EMAIL: EMAIL,
        CONF_OAUTH_TOKEN: token,
        DATA_AUTH_METHOD: "secrets_json",
        DATA_SECRET_BUNDLE: _bundle(),
    }
    if token.startswith("aas_et/"):
        data[DATA_AAS_TOKEN] = token
    return data


def _real_save_secrets_data() -> Callable[..., Any]:
    """Return the module's ``_async_save_secrets_data`` as currently bound."""
    integration = importlib.import_module("custom_components.googlefindmy")
    real_save: Callable[..., Any] = integration._async_save_secrets_data
    return real_save


async def _setup_and_replay(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
    real_save: Callable[..., Any],
    *,
    entry_data: dict[str, Any],
    cached: dict[str, Any],
) -> Any:
    """Run one setup, then replay a seeded bundle through the real save.

    Unlike the cache-branch tests, a setup that seeds no bundle is a valid
    outcome here (it is the defect), so the replay only runs when the seed
    called the save.
    """
    # A replay through the harness mock would assert nothing at all.
    assert not isinstance(real_save, Mock)
    harness = await _run_credential_seed(
        monkeypatch, stub_coordinator_factory, entry_data=entry_data, cached=cached
    )
    save = harness.integration._async_save_secrets_data
    if save.await_args is not None:
        await real_save(harness.cache, save.await_args.args[1])
    return harness


def _has_key_material(cache: dict[str, Any]) -> bool:
    """Return whether the cache holds the keys location decryption needs."""
    return bool(cache.get(f"owner_key_{EMAIL}") and cache.get("shared_key"))


@pytest.mark.asyncio
async def test_aas_import_keeps_the_bundle_and_derives_the_keys(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The first setup of an ``aas_et/`` import seeds the bundle it migrated."""
    real_save = _real_save_secrets_data()
    harness = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data=_imported_entry(AAS_TOKEN),
        cached={},
    )

    harness.integration._async_save_secrets_data.assert_awaited_once()
    cache = dict(harness.cache.values)
    assert cache.get(DATA_SECRET_BUNDLE), "the migrated bundle must stay cached"
    assert _has_key_material(cache), "owner_key/shared_key must be derived"


@pytest.mark.asyncio
async def test_aas_import_keeps_the_keys_on_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A second setup on the result of the first keeps the key material."""
    real_save = _real_save_secrets_data()
    first = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data=_imported_entry(AAS_TOKEN),
        cached={},
    )
    second = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data=dict(first.entry.data),
        cached=dict(first.cache.values),
    )

    cache = dict(second.cache.values)
    assert cache.get(DATA_SECRET_BUNDLE), "the bundle must survive the next start"
    assert _has_key_material(cache), "the keys must survive the next start"


@pytest.mark.asyncio
async def test_an_entry_with_a_new_aas_token_still_drops_the_old_bundle(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Replaced credentials keep the "no bundle any more" contract.

    Without a legacy payload the migration does not run. An entry that carries
    a new AAS token and no bundle means the account has no bundle any more;
    the old cached bundle must not come back.
    """
    real_save = _real_save_secrets_data()
    harness = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data={
            CONF_GOOGLE_EMAIL: EMAIL,
            DATA_AAS_TOKEN: "aas_et/NEW_TOKEN_VALUE",
            DATA_AUTH_METHOD: "secrets_json",
        },
        cached={
            DATA_SECRET_BUNDLE: {"owner_key": "STALE_OWNER_KEY"},
            DATA_AAS_TOKEN: "aas_et/OLD_TOKEN_VALUE",
        },
    )

    harness.integration._async_save_secrets_data.assert_not_awaited()
    assert DATA_SECRET_BUNDLE not in harness.cache.values


@pytest.mark.asyncio
async def test_oauth_import_keeps_the_bundle_and_derives_the_keys(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The OAuth import, which the migration already handles, stays intact."""
    real_save = _real_save_secrets_data()
    harness = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data=_imported_entry(OAUTH_TOKEN),
        cached={},
    )

    harness.integration._async_save_secrets_data.assert_awaited_once()
    cache = dict(harness.cache.values)
    assert cache.get(DATA_SECRET_BUNDLE)
    assert _has_key_material(cache)
    assert CONF_OAUTH_TOKEN not in harness.entry.data


@pytest.mark.asyncio
async def test_migration_moves_the_aas_token_from_the_entry_into_the_cache(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The migration takes every credential key out of the entry.

    That is the premise of the seed contract: a migrated entry carries no
    credentials, so the seed reads them back from the cache.
    """
    real_save = _real_save_secrets_data()
    harness = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data=_imported_entry(AAS_TOKEN),
        cached={},
    )

    assert DATA_AAS_TOKEN not in harness.entry.data
    assert harness.cache.values.get(DATA_AAS_TOKEN) == AAS_TOKEN


@pytest.mark.asyncio
async def test_migration_keeps_an_entry_token_the_bundle_lacks(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """An entry token survives the migration when the bundle carries none.

    The migration removes ``aas_token`` from the entry, so it must carry the
    entry's value into the cache itself; otherwise a bundle without its own
    ``aas_token`` would lose the account's only AAS token.
    """
    entry_data = _imported_entry(AAS_TOKEN)
    bundle = dict(entry_data[DATA_SECRET_BUNDLE])
    del bundle["aas_token"]
    entry_data[DATA_SECRET_BUNDLE] = bundle

    real_save = _real_save_secrets_data()
    harness = await _setup_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        real_save,
        entry_data=entry_data,
        cached={},
    )

    assert DATA_AAS_TOKEN not in harness.entry.data
    assert harness.cache.values.get(DATA_AAS_TOKEN) == AAS_TOKEN
    assert _has_key_material(dict(harness.cache.values))


@pytest.mark.asyncio
async def test_migration_prefers_the_entry_token_over_the_bundle_token(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The token the flow validated wins over a different one in the bundle.

    Discovery can store a candidate token that is not the bundle's own. Before
    the migration took the token out of the entry, the entry token reached the
    cache through the manual seed; now the migration must carry it there.
    """
    entry_token = "aas_et/VALIDATED_BY_THE_FLOW"
    entry_data = _imported_entry(AAS_TOKEN)
    entry_data[DATA_AAS_TOKEN] = entry_token

    # No replay here: the seed stores the AAS token right after saving the
    # bundle, so a replay of the save after the setup would put the bundle's
    # token back and invert the real order.
    harness = await _run_credential_seed(
        monkeypatch, stub_coordinator_factory, entry_data=entry_data, cached={}
    )

    harness.integration._async_save_secrets_data.assert_awaited_once()
    assert DATA_AAS_TOKEN not in harness.entry.data
    assert harness.cache.values.get(DATA_AAS_TOKEN) == entry_token


_INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "googlefindmy"
    / "__init__.py"
)


def _key_label(node: ast.expr) -> str:
    """Return a stable label for a key written as a name or a string constant."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return repr(node.value)
    raise AssertionError(
        f"unclassified key form at line {node.lineno}: {ast.dump(node)}"
    )


def test_guard_migration_pops_every_credential_key() -> None:
    """The legacy migration removes every credential key it knows from the entry.

    The seed contract (Auth/AGENTS.md, "HA vs standalone persistence"): a
    migrated entry carries no credentials. The guard reads
    ``async_setup_entry``: every key of the literal ``legacy_secrets_keys``
    tuple and ``DATA_AAS_TOKEN`` must be removed with
    ``updated_data.pop(<key>, None)``. A tuple that is not a literal, or a key
    written in another form, fails instead of passing unread.
    """
    tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"))
    setup = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    ]
    assert len(setup) == 1, "async_setup_entry not found exactly once"

    legacy_keys: set[str] | None = None
    popped: set[str] = set()
    for node in ast.walk(setup[0]):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "legacy_secrets_keys"
        ):
            assert legacy_keys is None, "legacy_secrets_keys bound more than once"
            assert isinstance(node.value, ast.Tuple), (
                "legacy_secrets_keys must stay a literal tuple"
            )
            legacy_keys = {_key_label(element) for element in node.value.elts}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "updated_data"
            and node.args
        ):
            popped.add(_key_label(node.args[0]))

    assert legacy_keys is not None, "legacy_secrets_keys not found"
    required = legacy_keys | {"DATA_AAS_TOKEN"}
    assert required <= popped, f"migration keeps {sorted(required - popped)}"
