# tests/test_log_hygiene_nova_account.py
"""Log hygiene for the account name in ``NovaApi/nova_request.py``.

``AGENTS.md`` (section 1, "Log hygiene for scanners", and section 5, "Never
log") forbids the account e-mail on every log level, and it forbids logging a
cache key name, because per-account keys embed the address
(``adm_token_<email>``). This module pins both for ``nova_request.py``:

* Static: no logger call passes ``self.username`` or a name that carries a key
  built from it (``key``, ``token_base``, ``issued_base``, ``probe_base``,
  ``normalized_user``, ``username``, ``user``, ``base``, ``cache_key``,
  ``token_key``), neither among the arguments after the format string nor as a
  keyword value such as ``extra``.
* Static: the two namespaced-mirror records in ``_get_initial_token_async``
  pass no ``exc_info`` and no ``extra`` and log only ``type(err).__name__``.
  The text of a cache backend's exception can carry the key (for example
  ``KeyError(cache_key)``), and ``exc_info`` prints it with the traceback.
  Both branches are excluded from coverage (``# pragma: no cover``), so this
  pin is their only check.
* Runtime: the AAS lifetime records of ``AsyncTTLPolicy`` and the cache read
  failure record of ``async_on_401`` name no account and no key.

Not covered: logger calls through a bound name (``log_fn(...)``), strings
formatted before the call, values passed on under a name that is not in the
list above, and every module other than ``nova_request.py``. The test can be
retired when a package-wide guard takes over the static predicate.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi import nova_request
from custom_components.googlefindmy.NovaApi.nova_request import AsyncTTLPolicy

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "googlefindmy"
    / "NovaApi"
    / "nova_request.py"
)
_LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_KEY_NAMES = {
    "key",
    "token_base",
    "issued_base",
    "probe_base",
    "normalized_user",
    "username",
    "user",
    "base",
    "cache_key",
    "token_key",
}
_SENTINEL = "owner.sentinel@example.com"
_NAMESPACE = "entry-account-hygiene"
_NOW = 1_800_000_000.0
_LIVED = "AAS lifetime before invalidation"
_UPDATED = "Updated learned AAS lifetime"
_PAST = "AAS age past learned threshold"
_INVALIDATING = "Invalidating the cached AAS entry"
_CACHE_READ = "Cache read failed for one ADM key variant"


def _tree() -> ast.Module:
    return ast.parse(_MODULE.read_text(encoding="utf-8"), str(_MODULE))


def _logger_calls(node: ast.AST) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _LEVELS
    ]


def _carries_account(value: ast.AST) -> bool:
    for sub in ast.walk(value):
        if isinstance(sub, ast.Attribute) and sub.attr == "username":
            return True
        if isinstance(sub, ast.Name) and sub.id in _KEY_NAMES:
            return True
    return False


def test_policy_logger_is_detected() -> None:
    """The walk finds the ``self.log`` calls of the policy (vacuum guard)."""

    calls = _logger_calls(_tree())
    policy_calls = [
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "log"
    ]
    assert policy_calls


def test_no_logger_call_passes_the_account_name() -> None:
    """No argument after the format string and no keyword value names the account."""

    offenders = [
        f"{call.lineno}: {ast.unparse(value)}"
        for call in _logger_calls(_tree())
        for value in call.args[1:] + [keyword.value for keyword in call.keywords]
        if _carries_account(value)
    ]
    assert offenders == []


def test_namespaced_mirror_records_log_only_the_exception_type() -> None:
    """Both records in ``_get_initial_token_async`` log the type name only."""

    functions = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_get_initial_token_async"
    ]
    assert len(functions) == 1
    calls = _logger_calls(functions[0])
    assert len(calls) == 2
    for call in calls:
        assert [keyword.arg for keyword in call.keywords] == []
        assert [ast.unparse(arg) for arg in call.args[1:]] == ["type(err).__name__"]


def _make_policy(
    logger_name: str, store: dict[str, Any]
) -> tuple[AsyncTTLPolicy, list[str]]:
    """Build an async policy for the sentinel account.

    Rebuilt rather than imported from ``tests/test_nova_ttl_threshold_refresh.py``,
    whose factory fixes the account name.
    """

    headers: list[str] = []

    async def _get(key: str) -> Any:
        return store.get(key)

    async def _set(key: str, value: Any) -> None:
        if value is None:
            store.pop(key, None)
        else:
            store[key] = value

    async def _refresh() -> str:
        return "fresh-token"

    policy = AsyncTTLPolicy(
        username=_SENTINEL,
        logger=logging.getLogger(logger_name),
        get_value=_get,
        set_value=_set,
        refresh_fn=_refresh,
        set_auth_header_fn=headers.append,
        ns_prefix=_NAMESPACE,
    )
    return policy, headers


def _assert_no_account(records: list[logging.LogRecord]) -> None:
    for record in records:
        assert _SENTINEL not in record.getMessage()
        assert _SENTINEL not in repr(record.args)


def _counts(
    records: list[logging.LogRecord],
) -> tuple[int, int, int, int]:
    def _count(prefix: str, level: int) -> int:
        return sum(
            1
            for record in records
            if record.getMessage().startswith(prefix) and record.levelno == level
        )

    return (
        _count(_LIVED, logging.INFO),
        _count(_UPDATED, logging.INFO),
        _count(_PAST, logging.INFO),
        _count(_INVALIDATING, logging.WARNING),
    )


@pytest.mark.asyncio
async def test_aas_lifetime_records_name_no_account(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four AAS lifetime records carry no account at runtime."""

    monkeypatch.setattr(nova_request.time, "time", lambda: _NOW)
    name = "test_log_hygiene_nova_account.aas"

    # Phase 1: learning from a token issued 48 hours ago, no learned lifetime yet.
    store: dict[str, Any] = {}
    policy, _ = _make_policy(name, store)
    store[policy.k_aas_issued] = _NOW - 48 * 3600
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=name):
        await policy._learn_aas_ttl(_NOW)
    assert _counts(caplog.records) == (1, 1, 0, 0)
    _assert_no_account(caplog.records)

    # Phase 2: issued seven days ago, learned lifetime one hour.
    store = {}
    policy, _ = _make_policy(name, store)
    store[policy.k_aas_issued] = _NOW - 7 * 24 * 3600
    store[policy.k_aas_bestttl] = 3600.0
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=name):
        assert await policy.async_check_aas_proactive_refresh() is False
    assert _counts(caplog.records) == (0, 0, 1, 0)
    _assert_no_account(caplog.records)

    # Phase 3: invalidation without an issuance timestamp, so learning returns early.
    store = {}
    policy, _ = _make_policy(name, store)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=name):
        await policy.async_invalidate_aas_token()
    assert _counts(caplog.records) == (0, 0, 0, 1)
    _assert_no_account(caplog.records)


@pytest.mark.asyncio
async def test_cache_read_failure_names_no_key(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing cache read in ``async_on_401`` logs the type name, not the key."""

    monkeypatch.setattr(nova_request.time, "time", lambda: _NOW)
    name = "test_log_hygiene_nova_account.on_401"
    store: dict[str, Any] = {}
    policy, headers = _make_policy(name, store)
    variants = policy._key_variants(f"adm_token_{_SENTINEL}")
    assert variants

    async def _get(key: str) -> Any:
        if key == policy.k_issued:
            return _NOW
        if key in variants:
            raise KeyError(key)
        return None

    policy._aget = _get

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=name):
        assert await policy.async_on_401() == "fresh-token"

    _assert_no_account(caplog.records)
    failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith(_CACHE_READ)
    ]
    assert len(failures) == len(variants)
    for record in failures:
        assert record.levelno == logging.DEBUG
        assert "KeyError" in record.getMessage()
    assert headers == ["Bearer fresh-token"]
