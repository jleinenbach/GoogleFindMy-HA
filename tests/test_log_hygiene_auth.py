# tests/test_log_hygiene_auth.py
"""Log hygiene for ``custom_components/googlefindmy/Auth``.

Two static checks walk every logger call in the package:

* No format string carries credential wording (``api key``, ``secret``,
  ``credential``, ``token``, ``password``) in front of a ``%s`` on the first
  source line of the literal. That is exactly what the Semgrep rule
  ``python-logger-credential-disclosure`` matches (AGENTS.md, "Log hygiene for
  scanners"), so a reworded message cannot slip back.
* No logger call passes a credential-named constant such as
  ``CONF_OAUTH_TOKEN``; CodeQL treats such names as sensitive data.

A behavioural check pins that seeding the username cache logs the masked
account, not the raw e-mail.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import adm_token_retrieval

_AUTH_DIR = Path(adm_token_retrieval.__file__).parent
_SEMGREP_FORMAT = re.compile(r"(?i).*(api.key|secret|credential|token|password).*%s.*")
# Semgrep searches the receiver text for "log" (``_LOGGER``, ``self.logger`` ...).
_LOGGER_OBJECT = re.compile(r"(?i)(_logger|logger|self.logger|log)")
_LOG_METHODS = frozenset(
    {"debug", "info", "warn", "warning", "error", "exception", "critical"}
)
_CREDENTIAL_CONSTANTS = re.compile(r"(?i)(token|secret|password|credential)")
_EMAIL = "pilot.user@example.com"


def _logger_calls() -> list[tuple[Path, str, ast.Call]]:
    calls: list[tuple[Path, str, ast.Call]] = []
    for path in sorted(_AUTH_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and _LOGGER_OBJECT.search(ast.unparse(node.func.value))
            ):
                calls.append((path, source, node))
    return calls


def test_auth_package_has_logger_calls() -> None:
    assert len(_logger_calls()) >= 100


def test_no_credential_wording_before_placeholder() -> None:
    offenders = []
    for path, source, call in _logger_calls():
        if not call.args:
            continue
        segment = ast.get_source_segment(source, call.args[0])
        if segment is None:
            continue
        first_line = segment.splitlines()[0]
        if _SEMGREP_FORMAT.match(first_line):
            offenders.append(f"{path.name}:{call.lineno}")
    assert offenders == []


def test_no_credential_named_constant_passed_to_logger() -> None:
    offenders = [
        f"{path.name}:{call.lineno}:{node.id}"
        for path, _source, call in _logger_calls()
        for arg in [*call.args[1:], *(kw.value for kw in call.keywords)]
        for node in ast.walk(arg)
        if isinstance(node, ast.Name)
        and node.id.isupper()
        and _CREDENTIAL_CONSTANTS.search(node.id)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ('"ADM token: generation failed. Error: %s"', True),
        ('"Failed to persist android_id from the secrets bundle. (%s at %s)"', True),
        ('"ADM exchange failed. Error: %s"', False),
        ('"FCM credential material corrupt "', False),
    ],
)
def test_semgrep_format_mirror(line: str, flagged: bool) -> None:
    assert bool(_SEMGREP_FORMAT.match(line)) is flagged


class _Cache:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, name: str) -> Any:
        return self.data.get(name)

    async def set(self, name: str, value: Any) -> None:
        self.data[name] = value


@pytest.mark.asyncio
async def test_username_seeding_logs_masked_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache = _Cache()
    with caplog.at_level(logging.DEBUG, logger=adm_token_retrieval._LOGGER.name):
        await adm_token_retrieval._seed_username_in_cache(_EMAIL, cache=cache)  # type: ignore[arg-type]

    assert _EMAIL in cache.data.values()
    seeded = [r for r in caplog.records if "username cache" in r.getMessage()]
    assert seeded, "the seeding must still be reported"
    assert all(_EMAIL not in r.getMessage() for r in caplog.records)
