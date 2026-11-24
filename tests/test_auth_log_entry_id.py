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


_FAILURE_METHODS = {"error", "warning", "warn", "exception", "critical"}
_FAILURE_LEVELS = {"WARNING", "WARN", "ERROR", "CRITICAL", "FATAL"}


def _is_logger(receiver: ast.expr) -> bool:
    """A name containing ``logger`` (``_LOGGER``, ``logger``) or ``getLogger(...)``."""

    if isinstance(receiver, ast.Name):
        return "logger" in receiver.id.lower()
    return (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Attribute)
        and receiver.func.attr == "getLogger"
    )


def _is_failure_level(level: ast.expr) -> bool:
    """``logging.WARNING`` and up, a number from 30 up, or a level not known statically."""

    if isinstance(level, ast.Attribute):
        return level.attr in _FAILURE_LEVELS
    if isinstance(level, ast.Constant) and isinstance(level.value, int):
        return level.value >= logging.WARNING
    return True


def _failure_calls(path: Path) -> list[tuple[ast.Call, int]]:
    """Failure-level logger calls with the index of their format argument.

    Blind spots, named on purpose: a logger bound to a name without ``logger``
    in it (``log = _LOGGER``), a bound method stored first and called later
    (``warn = _LOGGER.warning``), and calls built through ``getattr``. None of
    these forms occurs in the three modules today.
    """

    calls = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_logger(node.func.value)
        ):
            continue
        if node.func.attr in _FAILURE_METHODS:
            calls.append((node, 0))
        elif node.func.attr == "log" and node.args and _is_failure_level(node.args[0]):
            calls.append((node, 1))
    return calls


def _starts_with_entry_id(call: ast.Call, at: int) -> bool:
    if len(call.args) < at + 2:
        return False
    fmt, label = call.args[at], call.args[at + 1]
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
        for call, at in found
        if not _starts_with_entry_id(call, at)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ('_LOGGER.error("x")', 1),
        ('_LOGGER.critical("x")', 1),
        ('_LOGGER.warn("x")', 1),
        ('logger.warning("x")', 1),
        ('logging.getLogger(__name__).error("x")', 1),
        ('_LOGGER.log(logging.ERROR, "x")', 1),
        ('_LOGGER.log(40, "x")', 1),
        ('_LOGGER.log(level, "x")', 1),
        ('_LOGGER.log(logging.INFO, "x")', 0),
        ('_LOGGER.info("x")', 0),
        ('_LOGGER.debug("x")', 0),
    ],
)
def test_the_failure_call_detector_covers_its_forms(
    tmp_path: Path, source: str, found: int
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source + "\n", encoding="utf-8")
    assert len(_failure_calls(probe)) == found


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
