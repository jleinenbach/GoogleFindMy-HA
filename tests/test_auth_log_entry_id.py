# tests/test_auth_log_entry_id.py
"""Failure lines of the auth modules name their config entry.

With several accounts the same failure text appears once per entry, and
without the entry id nobody can tell which account failed. Every error and
warning line in the three token modules therefore starts with
``entry_id=<id>:``; caches without an entry log ``-``.

A stored value that already has the AAS form is reused without any exchange,
so the routine must not report it as freshly generated.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import (
    aas_token_retrieval,
    adm_token_retrieval,
    log_safety,
)
from custom_components.googlefindmy.const import CONF_OAUTH_TOKEN, DATA_AAS_TOKEN

_AUTH_DIR = Path(aas_token_retrieval.__file__).resolve().parent
_MODULES = ("aas_token_retrieval.py", "adm_token_retrieval.py", "token_retrieval.py")
_PREFIX = "entry_id=%s: "
_USER = "entry.user@example.com"


def _failure_calls(path: Path) -> list[ast.Call]:
    calls = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"error", "warning", "exception"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_LOGGER"
        ):
            calls.append(node)
    return calls


def _starts_with_entry_id(call: ast.Call) -> bool:
    if len(call.args) < 2:
        return False
    fmt, label = call.args[0], call.args[1]
    return (
        isinstance(fmt, ast.Constant)
        and isinstance(fmt.value, str)
        and fmt.value.startswith(_PREFIX)
        and isinstance(label, ast.Call)
        and isinstance(label.func, ast.Name)
        and label.func.id == "entry_label_for_log"
    )


def test_every_auth_error_line_starts_with_the_entry_id() -> None:
    calls = {name: _failure_calls(_AUTH_DIR / name) for name in _MODULES}
    assert sum(len(found) for found in calls.values()) >= 20
    offenders = [
        f"{name}:{call.lineno}"
        for name, found in calls.items()
        for call in found
        if not _starts_with_entry_id(call)
    ]
    assert offenders == []


class _Cache:
    """Entry-scoped cache double with the TokenCache methods the code uses."""

    def __init__(self, entry_id: str = "", **values: Any) -> None:
        self.entry_id = entry_id
        self.values: dict[str, Any] = dict(values)

    async def get(self, name: str) -> Any:
        return self.values.get(name)

    async def set(self, name: str, value: Any) -> None:
        if value is None:
            self.values.pop(name, None)
        else:
            self.values[name] = value

    async def all(self) -> dict[str, Any]:
        return dict(self.values)

    async def get_or_set(self, name: str, generator: Any) -> Any:
        if name in self.values:
            return self.values[name]
        result = generator()
        if asyncio.iscoroutine(result):
            result = await result
        await self.set(name, result)
        return result


@pytest.mark.asyncio
async def test_entry_label_names_the_token_cache_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The label is the cache's entry id, or ``-`` where there is none."""

    label = getattr(log_safety, "entry_label_for_log", None)
    assert label is not None
    assert label(SimpleNamespace(entry_id="e1")) == "e1"
    assert label(SimpleNamespace(entry_id="")) == "-"
    assert label(object()) == "-"

    with caplog.at_level(logging.WARNING):
        await adm_token_retrieval._resolve_android_id_for_entry(
            _USER, cache=_Cache(entry_id="e1")
        )
    assert [r.getMessage() for r in caplog.records if "android_id" in r.getMessage()]
    assert all(
        record.getMessage().startswith("entry_id=e1: ")
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_reusing_a_stored_aas_value_reports_no_generation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An AAS-form value is reused: no 'generated' line, no new issue time."""

    stored = "aas_et/" + "R" * 80
    cache = _Cache(entry_id="e2", **{CONF_OAUTH_TOKEN: stored})
    await cache.set(aas_token_retrieval.username_string, _USER)

    with caplog.at_level(logging.DEBUG, logger=aas_token_retrieval.__name__):
        token = await aas_token_retrieval.async_get_aas_token(cache=cache)

    messages = [record.getMessage() for record in caplog.records]
    assert token == stored
    assert cache.values[DATA_AAS_TOKEN] == stored
    assert not [m for m in messages if "successfully generated" in m]
    assert not [m for m in messages if "generating new token" in m]
    assert not [key for key in cache.values if key.startswith("aas_token_issued_at_")]


@pytest.mark.asyncio
async def test_a_failed_exchange_names_the_entry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The exchange has no cache of its own; the caller's entry reaches its log."""

    def _no_result(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"Error": "BadAuthentication"}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", _no_result)
    cache = _Cache(entry_id="e3", **{CONF_OAUTH_TOKEN: "valid_oauth_token"})
    await cache.set(aas_token_retrieval.username_string, _USER)

    with (
        caplog.at_level(logging.WARNING, logger=aas_token_retrieval.__name__),
        pytest.raises(RuntimeError),
    ):
        await aas_token_retrieval._generate_aas_token(cache=cache)  # type: ignore[arg-type]

    missing = [
        record.getMessage()
        for record in caplog.records
        if "missing the AAS result field" in record.getMessage()
    ]
    assert len(missing) == 1
    assert missing[0].startswith("entry_id=e3: ")
