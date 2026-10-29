# tests/test_fcm_credentials_entry_copy.py
"""The FCM registration lives in the bundle and the cache, never on top of the entry.

The config flows used to store the registration twice: inside the secrets
bundle (``entry.data["secrets_data"]["fcm_credentials"]``) and as a second copy
at the top level (``entry.data["fcm_credentials"]``). Nothing reads the second
copy; the seed reads the bundle, and the receiver renews the bundle member and
the cache. The copy only went stale and kept one more exemplar of the device
credential in the entry (data minimisation).

These tests pin the rule from ``Auth/AGENTS.md``:

* no flow writes the top-level copy (discovery, paste/preflight helper,
  ``async_step_migrate``), checked by behaviour and by an AST guard over
  ``config_flow.py``;
* a legacy entry ends its first setup without it (the repair runs before the
  legacy migration);
* a setup-time repair removes a copy an earlier version stored, and leaves an
  entry without a copy untouched.
"""

from __future__ import annotations

import ast
import asyncio
import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
)
from tests.test_config_entry_startup import _run_credential_seed
from tests.test_hass_data_layout import _prepare_async_setup_entry_harness

EMAIL = "user@example.com"
AAS_TOKEN = "aas_et/MASTER_TOKEN_VALUE"
COPY_KEY = "fcm_credentials"
_CONFIG_FLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "googlefindmy"
    / "config_flow.py"
)


def _registration(token: str) -> dict[str, Any]:
    """Return a complete FCM registration carrying ``token``."""
    return {
        "gcm": {"android_id": "1234567890", "security_token": "9876543210"},
        "fcm": {"registration": {"token": token}},
    }


def _bundle() -> dict[str, Any]:
    """Return a secrets.json bundle with key material and a registration."""
    return {
        "username": EMAIL,
        "owner_key": "OWNER_KEY_VALUE",
        "shared_key": "SHARED_KEY_VALUE",
        "aas_token": AAS_TOKEN,
        COPY_KEY: _registration("bundle"),
    }


def test_persist_helper_returns_no_top_level_copy() -> None:
    """The shared persist helper (paste, preflight, reauth, options) stores the
    registration in the bundle only."""
    bundle = _bundle()
    persisted = config_flow._persist_secrets_bundle(bundle, AAS_TOKEN, email=EMAIL)

    assert COPY_KEY not in persisted
    assert persisted[DATA_SECRET_BUNDLE][COPY_KEY] == _registration("bundle")


@pytest.mark.asyncio
async def test_discovery_ingest_writes_no_top_level_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovered bundle reaches the entry with its registration inside it."""

    async def _fake_pick(*_args: Any, **_kwargs: Any) -> str:
        return AAS_TOKEN

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _Flow:
        hass = object()

    discovery = config_flow._normalize_and_validate_discovery_payload(
        {
            CONF_GOOGLE_EMAIL: EMAIL,
            "candidate_tokens": [AAS_TOKEN],
            "secrets_json": _bundle(),
        }
    )
    auth_data, updates = await config_flow._ingest_discovery_credentials(
        _Flow(),  # type: ignore[arg-type]
        discovery,
    )

    assert updates is None
    assert COPY_KEY not in auth_data
    assert auth_data[DATA_SECRET_BUNDLE][COPY_KEY] == _registration("bundle")


@pytest.mark.asyncio
async def test_a_legacy_entry_loses_the_top_level_copy_on_its_first_setup(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A legacy entry ends its first setup without the copy.

    The setup repair runs before the legacy migration, so the migration never
    sees the copy and needs no removal of its own.
    """
    harness = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={
            CONF_GOOGLE_EMAIL: EMAIL,
            CONF_OAUTH_TOKEN: AAS_TOKEN,
            DATA_AUTH_METHOD: "secrets_json",
            DATA_SECRET_BUNDLE: _bundle(),
            COPY_KEY: _registration("copy"),
        },
        cached={},
    )

    assert COPY_KEY not in harness.entry.data


@pytest.mark.asyncio
async def test_setup_repair_removes_a_stored_copy(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """An entry an earlier version wrote loses the copy on the next setup.

    The cache is primed, so the legacy migration does not run; only the
    repair can remove the copy. The bundle and its registration stay as they
    are.
    """
    harness = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={
            CONF_GOOGLE_EMAIL: EMAIL,
            CONF_OAUTH_TOKEN: AAS_TOKEN,
            DATA_AUTH_METHOD: "secrets_json",
            DATA_SECRET_BUNDLE: _bundle(),
            COPY_KEY: _registration("copy"),
        },
        cached={DATA_SECRET_BUNDLE: _bundle(), "username": EMAIL},
    )

    assert COPY_KEY not in harness.entry.data
    assert harness.entry.data[DATA_SECRET_BUNDLE][COPY_KEY] == _registration("bundle")


@pytest.mark.asyncio
async def test_setup_repair_leaves_an_entry_without_copy_alone(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The repair writes only when it has something to remove."""
    loop = asyncio.get_running_loop()
    factory = functools.partial(stub_coordinator_factory, data=[])
    harness = _prepare_async_setup_entry_harness(monkeypatch, factory, loop)
    harness.entry.data = {
        CONF_GOOGLE_EMAIL: EMAIL,
        CONF_OAUTH_TOKEN: AAS_TOKEN,
        DATA_AUTH_METHOD: "secrets_json",
        DATA_SECRET_BUNDLE: _bundle(),
    }
    calls: list[dict[str, Any]] = []

    def _record(entry: Any, **kwargs: Any) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr(harness.hass.config_entries, "async_update_entry", _record)

    harness.integration._async_strip_entry_fcm_credentials_copy(
        harness.hass, harness.entry
    )

    assert calls == []


def _constant_key(node: ast.AST) -> bool:
    """Return whether ``node`` is the string constant of the copy's key."""
    return isinstance(node, ast.Constant) and node.value == COPY_KEY


def _copy_key_uses(source: str) -> list[int]:
    """Return the lines where ``source`` names the copy's key other than to read it.

    Allowed: a subscript load (``data["fcm_credentials"]``) and the first
    argument of ``.get(...)``. Any other string constant of the key (subscript
    store, dict display key, tuple of keys to keep, call argument) and any
    keyword argument of that name (``dict(base, fcm_credentials=x)``,
    ``data.update(fcm_credentials=x)``) is reported. Not covered: a key built at
    runtime, such as ``"fcm_" + "credentials"``.
    """
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _constant_key(node.slice)
        ):
            allowed.add(id(node.slice))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _constant_key(node.args[0])
        ):
            allowed.add(id(node.args[0]))

    lines = [
        node.lineno
        for node in ast.walk(tree)
        if _constant_key(node) and id(node) not in allowed
    ]
    lines.extend(
        node.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == COPY_KEY
    )
    return sorted(lines)


@pytest.mark.parametrize(
    "snippet",
    [
        'data["fcm_credentials"] = reg',
        'payload = {"fcm_credentials": reg}',
        'keys = ("secrets_data", "fcm_credentials")',
        "payload = dict(base, fcm_credentials=reg)",
        "data.update(fcm_credentials=reg)",
    ],
)
def test_guard_reports_every_write_form(snippet: str) -> None:
    """The guard itself: each write form of the key is reported."""
    assert _copy_key_uses(snippet) == [1]


@pytest.mark.parametrize(
    "snippet",
    [
        'reg = data["fcm_credentials"]',
        'reg = data.get("fcm_credentials")',
        'def f():\n    """Mentions fcm_credentials in prose."""',
    ],
)
def test_guard_lets_reads_pass(snippet: str) -> None:
    """The guard itself: reads and prose are not reported."""
    assert _copy_key_uses(snippet) == []


def test_guard_config_flow_never_stores_the_top_level_copy() -> None:
    """``config_flow.py`` names the key only to read it (see ``_copy_key_uses``)."""
    offending = _copy_key_uses(_CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    assert offending == [], f"config_flow.py stores the copy at lines {offending}"
