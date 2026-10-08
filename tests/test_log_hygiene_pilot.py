# tests/test_log_hygiene_pilot.py
"""Log hygiene for ``TokenCache``: cache key names never reach the log.

Per-account cache keys embed the Google account e-mail
(``owner_key_<email>``, ``shared_key_<email>``), so a key name is personal
data. The error paths for non-JSON-serializable values therefore log the
value type instead of the key, and no logger call in the module passes a
key name. Format strings avoid credential wording in front of a ``%s``
placeholder, so the Semgrep rule ``python-logger-credential-disclosure``
(format-string regex below) has nothing to report; the static checks walk
every logger call in the module, so a reworded message cannot slip back.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth import token_cache as tc
from custom_components.googlefindmy.Auth.token_cache import TokenCache

_EMAIL = "pilot.user@example.com"
_KEY = f"owner_key_{_EMAIL}"


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-global registry/state around each test."""
    monkeypatch.setattr(tc, "_INSTANCES", {}, raising=True)
    monkeypatch.setattr(
        tc,
        "_STATE",
        {"legacy_migration_done": False, "default_entry_id": None},
        raising=True,
    )


def _real_cache(monkeypatch: pytest.MonkeyPatch) -> TokenCache:
    """Build a real TokenCache with the Store patched out (no disk I/O)."""
    monkeypatch.setattr(tc, "Store", Mock())
    hass = SimpleNamespace(loop=asyncio.get_event_loop(), data={})
    cache = TokenCache(hass, "e1")
    cache._store = Mock()
    return cache


@pytest.mark.asyncio
async def test_set_non_jsonable_logs_type_not_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache = _real_cache(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=tc._LOGGER.name):
        await cache.set(_KEY, object())

    assert _KEY not in cache._data
    assert caplog.records, "the skip must still be reported"
    assert all(_EMAIL not in r.getMessage() for r in caplog.records)
    assert any(r.args == ("object",) for r in caplog.records)


def test_snapshot_check_logs_type_not_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache = _real_cache(monkeypatch)
    cache._data[_KEY] = {1, 2}
    with caplog.at_level(logging.ERROR, logger=tc._LOGGER.name):
        assert cache._is_valid_snapshot() is False

    assert caplog.records
    assert all(_EMAIL not in r.getMessage() for r in caplog.records)
    assert any(r.args == ("set",) for r in caplog.records)


_MODULE = Path(tc.__file__)
# Format-string regex of the Semgrep rule, applied here to the whole literal
# (stricter than Semgrep, which only looks at its first source line).
_CREDENTIAL_WORDING = re.compile(
    r"(?i).*(api.key|secret|credential|token|password).*%s.*"
)
_KEY_NAMES = frozenset({"name", "key", "cache_key"})


def _logger_calls() -> list[ast.Call]:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_LOGGER"
    ]


def test_module_has_logger_calls() -> None:
    assert len(_logger_calls()) >= 10


def test_no_credential_wording_before_placeholder() -> None:
    offenders = [
        (call.lineno, call.args[0].value)
        for call in _logger_calls()
        if call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and _CREDENTIAL_WORDING.match(call.args[0].value.replace("\n", " "))
    ]
    assert offenders == []


def test_no_cache_key_name_passed_to_logger() -> None:
    offenders = [
        (call.lineno, arg.id)
        for call in _logger_calls()
        for arg in call.args[1:]
        if isinstance(arg, ast.Name) and arg.id in _KEY_NAMES
    ]
    assert offenders == []
