# tests/test_unattended_routine_log_levels.py
"""Pin the log level of routine records that repeat unattended.

Two kinds of routine records wrote INFO lines on every background cycle
although none of them targets the user:

* ADM token renewal: one record before and one after every renewal, about 550
  each in three and a half days on a live instance, plus the proactive-refresh
  record of the TTL policy (about 510). The record for the
  ``individual_tokens`` auth method also claimed a fresh AAS token although its
  provider returns the cached one; it now says what happens.
* Location age on the poll path: one record per poll for every device whose
  last report is older than 24 hours (about 760), carrying the device name on a
  path that repeats unattended (``AGENTS.md`` section 5 (b)).

The Home Assistant development guidelines ask integrations to "be restrictive
with `_LOGGER.info`, use `_LOGGER.debug` for anything which is not targeting the
user". A record that does target the user stays at INFO and is pinned as well:
the cached AAS token being missing means a new sign-in, a state change.

The check is static (AST) like ``tests/test_locate_routine_log_levels.py``; the
renewal path needs the gpsoauth stack to run end to end. The poll-path record
is also covered at runtime in ``tests/test_poll_timeout_hardening.py``.

Declared blind spots: ``<logger>.log(level, ...)``, a format string held in a
variable, f-strings, and receivers other than a bare name containing
``logger`` or the attribute ``self.log`` (the TTL policy in
``NovaApi/nova_request.py`` logs through ``self.log``). The test pins the
records listed below; it is not a guard for new ones.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"

# (module relative to the package, start of the format string, expected calls)
_DEBUG_RECORDS: tuple[tuple[str, str, int], ...] = (
    (
        "Auth/adm_token_retrieval.py",
        "ADM token: generating new token via the entry AAS provider",
        1,
    ),
    (
        "Auth/adm_token_retrieval.py",
        "ADM token: generating new token using cached AAS token",
        1,
    ),
    ("Auth/adm_token_retrieval.py", "ADM token: successfully generated", 1),
    # The TTL policy has a sync and an async variant of the same record.
    ("NovaApi/nova_request.py", "ADM token reached measured threshold", 2),
    ("coordinator/polling.py", "Using old location data for ", 1),
    ("coordinator/polling.py", "Using location data for ", 1),
)

_INFO_RECORDS: tuple[tuple[str, str, int], ...] = (
    ("Auth/adm_token_retrieval.py", "ADM token: cached AAS token missing", 1),
)


def _is_logger_receiver(node: ast.expr) -> bool:
    """Return True for a bare name containing ``logger`` or ``self.log``."""

    if isinstance(node, ast.Name):
        return "LOGGER" in node.id.upper()
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "log"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _logger_calls(module: str, prefix: str) -> list[tuple[str, str]]:
    """Return ``(method, format string)`` for every logger call in ``module``
    whose first argument is a string literal starting with ``prefix``."""

    tree = ast.parse((_PACKAGE / module).read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_logger_receiver(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        if node.args[0].value.startswith(prefix):
            found.append((node.func.attr, node.args[0].value))
    return found


@pytest.mark.parametrize(("module", "prefix", "count"), _DEBUG_RECORDS)
def test_unattended_routine_record_logs_at_debug(
    module: str, prefix: str, count: int
) -> None:
    """Each routine record exists the expected number of times, all at DEBUG."""

    calls = _logger_calls(module, prefix)
    assert len(calls) == count, f"expected {count} call(s) for {prefix!r}: {calls}"
    assert all(method == "debug" for method, _ in calls), calls


@pytest.mark.parametrize(("module", "prefix", "count"), _INFO_RECORDS)
def test_user_facing_record_stays_at_info(module: str, prefix: str, count: int) -> None:
    """A record that reports a state change keeps its INFO level."""

    calls = _logger_calls(module, prefix)
    assert len(calls) == count, f"expected {count} call(s) for {prefix!r}: {calls}"
    assert all(method == "info" for method, _ in calls), calls


@pytest.mark.parametrize(("module", "prefix", "count"), _DEBUG_RECORDS + _INFO_RECORDS)
def test_record_has_no_trailing_period(module: str, prefix: str, count: int) -> None:
    """Home Assistant log style: no period and no ellipsis at the end."""

    calls = _logger_calls(module, prefix)
    assert len(calls) == count, f"expected {count} call(s) for {prefix!r}: {calls}"
    for _, fmt in calls:
        tail = fmt.rstrip()
        assert not tail.endswith((".", "…")), f"{fmt!r} ends with a period"
