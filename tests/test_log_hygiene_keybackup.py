# tests/test_log_hygiene_keybackup.py
"""Log hygiene for ``custom_components/googlefindmy/KeyBackup``.

The vault fetch handles the owner keys of every key version, still wrapped with
the shared key, plus the shared key itself. Only domain, version, epoch, count
and cause may leave the module (L-5).

A static check walks every logger call in the package, as in
``tests/test_log_hygiene_auth.py``: no format string carries credential wording
in front of a ``%s`` on its first source line.

The behavioural checks drive ``shared_key_flow`` through every outcome of the
vault fetch (success, each failure, a raising sink, the sign-in-only flow and
the alert deadline) and search ``caplog``, ``capsys`` (``print`` is invisible to
the logging guards) and ``repr``/``str`` of every ``VaultFetchResult`` for any
16-character window of any textual form of the synthetic secrets (AE-14). The
forms and their gaps are listed in ``VaultWorld.all_secret_strings``.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy.KeyBackup import shared_key_flow
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    FIND_MY_DEVICE_URL,
    VaultCause,
    VaultFetchResult,
)
from tests.helpers.owner_key_vault_fixtures import VaultWorld, make_vault_world
from tests.helpers.shared_key_flow_doubles import (
    FakeClock,
    FakeDriver,
    NoAlertWait,
    patch_flow,
    vault_answer,
    vault_answer_text,
)

_KEYBACKUP_DIR = Path(shared_key_flow.__file__).parent
_SEMGREP_FORMAT = re.compile(r"(?i).*(api.key|secret|credential|token|password).*%s.*")
_LOGGER_OBJECT = re.compile(r"(?i)(_logger|logger|self.logger|log)")
_LOG_METHODS = frozenset(
    {"debug", "info", "warn", "warning", "error", "exception", "critical"}
)
_HYGIENE_WINDOW = 16
# Response body and driver error text that must never be echoed.
_BODY_MARKER = "BODY-MARKER-do-not-log"
_ERROR_MARKER = "ERROR-MARKER-do-not-log"
_SET_KEYS = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": "raw-keys"})


def _logger_calls() -> list[tuple[Path, str, ast.Call]]:
    calls: list[tuple[Path, str, ast.Call]] = []
    for path in sorted(_KEYBACKUP_DIR.rglob("*.py")):
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


def test_keybackup_package_has_logger_calls() -> None:
    # The vault fetch adds its own calls; a walker that finds none is broken.
    files = {path.name for path, _source, _call in _logger_calls()}
    assert "shared_key_flow.py" in files
    assert len(_logger_calls()) >= 10


def test_no_credential_wording_before_placeholder() -> None:
    offenders = []
    for path, source, call in _logger_calls():
        if not call.args:
            continue
        segment = ast.get_source_segment(source, call.args[0])
        if segment is None:
            continue
        if _SEMGREP_FORMAT.match(segment.splitlines()[0]):
            offenders.append(f"{path.name}:{call.lineno}")
    assert offenders == []


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


def _leaks(text: str, world: VaultWorld) -> list[str]:
    hits: list[str] = []
    for form in world.all_secret_strings():
        for start in range(len(form) - _HYGIENE_WINDOW + 1):
            window = form[start : start + _HYGIENE_WINDOW]
            if window in text:
                hits.append(window)
                break
    return hits


def _quitting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda d: d and d.quit())


def _run_shared_key_flow(
    monkeypatch: pytest.MonkeyPatch,
    world: VaultWorld,
    driver: FakeDriver,
    sink: Callable[[VaultFetchResult], None],
) -> list[object]:
    patch_flow(monkeypatch, driver, shared_key=world.shared_key)
    _quitting(monkeypatch)
    return [shared_key_flow.request_shared_key_flow(vault_sink=sink)]


def _one_byte_longer(world: VaultWorld) -> VaultWorld:
    blobs = dict(world.wrapped_owner_keys)
    blobs[1] = blobs[1] + b"\x00"
    return world._replace(wrapped_owner_keys=blobs)


_EXPECTED_CAUSE: dict[str, VaultCause | None] = {
    "success": None,
    "status_not_ok": VaultCause.HTTP_STATUS,
    "script_error": VaultCause.HTTP_STATUS,
    "foreign_text": VaultCause.NO_FRAME,
    "wrong_length": VaultCause.WRONG_LENGTH,
    "script_raises": VaultCause.HTTP_STATUS,
    "navigation_raises": VaultCause.HTTP_STATUS,
}


def _scenario_driver(world: VaultWorld, name: str) -> FakeDriver:
    secret_hex = world.wrapped_owner_keys[1].hex()
    answers: dict[str, object] = {
        "success": vault_answer(world),
        "status_not_ok": {"ok": False, "status": 403, "text": _BODY_MARKER},
        "script_error": {"ok": False, "error": f"{_ERROR_MARKER} {secret_hex}"},
        "foreign_text": {
            "ok": True,
            "status": 200,
            "text": f"{_BODY_MARKER} {vault_answer_text(world)[:40]}",
        },
        "wrong_length": {
            "ok": True,
            "status": 200,
            "text": vault_answer_text(_one_byte_longer(world)),
        },
        "script_raises": RuntimeError(f"{_ERROR_MARKER} {secret_hex}"),
        "navigation_raises": {"ok": True, "text": vault_answer_text(world)},
    }
    driver = FakeDriver([_SET_KEYS], async_answer=answers[name])
    if name == "navigation_raises":
        driver.get_error = RuntimeError(f"{_ERROR_MARKER} {secret_hex}")
        driver.get_error_url = FIND_MY_DEVICE_URL
    return driver


_FETCH_SCENARIOS = tuple(_EXPECTED_CAUSE)


@pytest.fixture
def world() -> VaultWorld:
    return make_vault_world(versions=(1, 2, 3), spot_version=1)


def _assert_clean(
    world: VaultWorld,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    results: list[VaultFetchResult],
) -> None:
    captured = capsys.readouterr()
    rendered = [repr(r) for r in results] + [str(r) for r in results]
    text = "\n".join(
        [*caplog.messages, captured.out, captured.err, *rendered]
        + [record.getMessage() for record in caplog.records]
        + [str(record.args) for record in caplog.records]
    )
    assert _leaks(text, world) == []
    assert _BODY_MARKER not in text
    assert _ERROR_MARKER not in text


@pytest.mark.parametrize("scenario", _FETCH_SCENARIOS)
def test_fetch_outcomes_leak_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    world: VaultWorld,
    scenario: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    received: list[VaultFetchResult] = []
    driver = _scenario_driver(world, scenario)

    returned = _run_shared_key_flow(monkeypatch, world, driver, received.append)

    assert returned == [world.shared_key.hex()]  # the flow itself still succeeds
    assert len(received) == 1
    assert received[0].cause is _EXPECTED_CAUSE[scenario]
    assert (received[0].entries == ()) is (scenario != "success")
    assert any("Vault fetch:" in message for message in caplog.messages)
    _assert_clean(world, caplog, capsys, received)


def test_raising_sink_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    world: VaultWorld,
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[VaultFetchResult] = []

    def _sink(result: VaultFetchResult) -> None:
        seen.append(result)
        raise ValueError(f"{_ERROR_MARKER} {world.shared_key.hex()}")

    driver = _scenario_driver(world, "success")
    returned = _run_shared_key_flow(monkeypatch, world, driver, _sink)

    assert returned == [world.shared_key.hex()]
    assert "Vault sink raised: ValueError" in caplog.messages
    _assert_clean(world, caplog, capsys, seen)


@pytest.mark.parametrize("sign_in", ["completes", "times_out"])
def test_vault_entries_flow_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    world: VaultWorld,
    sign_in: str,
) -> None:
    from selenium.common.exceptions import TimeoutException

    caplog.set_level(logging.DEBUG)
    driver = FakeDriver(async_answer=vault_answer(world))
    patch_flow(monkeypatch, driver)
    _quitting(monkeypatch)
    if sign_in == "times_out":

        class _SignInTimesOut:
            def __init__(self, _driver: object, _timeout: float) -> None:
                pass

            def until(self, _condition: Any) -> Any:
                raise TimeoutException(f"{_ERROR_MARKER} {world.shared_key.hex()}")

        monkeypatch.setattr(shared_key_flow, "WebDriverWait", _SignInTimesOut)

    result = shared_key_flow.request_vault_entries_flow()

    if sign_in == "completes":
        assert len(result.entries) == 3
    else:
        assert result.cause is VaultCause.HTTP_STATUS
    _assert_clean(world, caplog, capsys, [result])


def test_alert_deadline_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    world: VaultWorld,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    driver = FakeDriver(async_answer=vault_answer(world))
    received: list[VaultFetchResult] = []
    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **_: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", NoAlertWait(clock))
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: "https://sec"
    )
    monkeypatch.setattr(shared_key_flow, "_monotonic", clock)
    _quitting(monkeypatch)

    assert shared_key_flow.request_shared_key_flow(vault_sink=received.append) is None

    assert len(received[0].entries) == 3
    assert any("cause=alert_deadline" in message for message in caplog.messages)
    _assert_clean(world, caplog, capsys, received)
