# tests/test_shared_key_flow.py
"""Tests for the manual shared key retrieval flow ``KeyBackup.shared_key_flow``."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.KeyBackup import shared_key_flow
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    FETCH_SCRIPT,
    FIND_MY_DEVICE_URL,
    SharedKeyCancelledOnPage,
    SharedKeyFlowDeadlineError,
    VaultCause,
    VaultFetchResult,
)
from tests.helpers.owner_key_vault_fixtures import VaultWorld, make_vault_world
from tests.helpers.shared_key_flow_doubles import (
    FakeClock,
    FlowWait,
    NoAlertWait,
    vault_answer,
)
from tests.helpers.shared_key_flow_doubles import FakeDriver as _FakeDriver
from tests.helpers.shared_key_flow_doubles import StopFlow as _StopFlow
from tests.helpers.shared_key_flow_doubles import patch_flow as _patch_flow

# ---------------------------------------------------------------------------
# _parse_cli_args
# ---------------------------------------------------------------------------


def test_parse_cli_args_defaults() -> None:
    args = shared_key_flow._parse_cli_args([])
    assert args.chrome_path is None
    assert args.chrome_version is None


def test_parse_cli_args_values() -> None:
    args = shared_key_flow._parse_cli_args(
        ["--chrome-path", "/opt/chrome", "--chrome-version", "149"]
    )
    assert args.chrome_path == "/opt/chrome"
    assert args.chrome_version == 149


def test_parse_cli_args_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        shared_key_flow._parse_cli_args(["--help"])
    assert excinfo.value.code == 0


def test_parse_cli_args_invalid_version_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        shared_key_flow._parse_cli_args(["--chrome-version", "x"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# request_shared_key_flow
# ---------------------------------------------------------------------------


def test_create_driver_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("no chrome")

    monkeypatch.setattr(shared_key_flow, "create_driver", boom)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    assert shared_key_flow.request_shared_key_flow() is None


def test_returns_shared_key_hex_on_set_vault_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": "raw-keys"})
    driver = _FakeDriver([payload])
    raw_key = bytes(range(8))
    _patch_flow(monkeypatch, driver, shared_key=raw_key)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda d: d.quit())

    captured_path: dict[str, Any] = {}
    monkeypatch.setattr(
        shared_key_flow,
        "create_driver",
        lambda **kwargs: captured_path.update(kwargs) or driver,
    )

    result = shared_key_flow.request_shared_key_flow(
        chrome_path="/opt/chrome", chrome_version=149
    )

    assert result == raw_key.hex()
    assert captured_path == {"chrome_path": "/opt/chrome", "chrome_version": 149}
    assert driver.visited_urls == ["https://accounts.google.com/", "https://sec"]
    assert driver.quit_calls == 1


def test_close_view_raises_cancelled_on_page(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"method": "closeView"})
    driver = _FakeDriver([payload])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()


def test_malformed_json_is_discarded_then_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver(["{not-json", good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()
    assert "malformed alert payload" in " ".join(caplog.messages).lower()


def test_set_vault_keys_with_invalid_payload_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": 123})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([bad, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()
    assert "invalid vaultkeys" in " ".join(caplog.messages).lower()


def test_unhandled_method_is_logged_then_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    other = json.dumps({"method": "somethingElse"})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([other, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()
    assert "unhandled alert payload" in " ".join(caplog.messages).lower()


def test_timeout_exception_continues_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending-alert ``TimeoutException`` must ``continue`` the wait loop."""

    from selenium.common.exceptions import TimeoutException

    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([good])

    state = {"raised": False}

    class _TimeoutOnceWait:
        def __init__(self, drv: _FakeDriver, timeout: float) -> None:
            self.driver = drv
            self.timeout = timeout

        def until(self, _condition: Any) -> Any:
            if self.timeout >= 1:
                return True
            if not state["raised"]:
                state["raised"] = True
                raise TimeoutException
            if not self.driver.arm_next_alert():
                raise _StopFlow
            return True

    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", _TimeoutOnceWait)
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: "https://sec"
    )
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()
    assert state["raised"] is True


def test_module_entrypoint_invokes_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the ``__main__`` guard by running the module as a script.

    ``runpy`` re-executes the file with fresh module-level imports, so the
    patched ``WebDriverWait`` cannot reach the rebound copy. To avoid launching
    a real browser, the freshly imported ``create_driver`` is forced to raise so
    the flow returns ``None`` through its early failure branch before any wait
    loop runs.
    """

    import runpy

    from custom_components.googlefindmy import chrome_driver

    calls: dict[str, Any] = {}

    def boom(**kwargs: Any) -> Any:
        calls.update(kwargs)
        raise RuntimeError("no chrome in __main__ test")

    # shared_key_flow binds ``create_driver`` via ``from ... import`` at module
    # load, so the runpy copy resolves this patched attribute at import time.
    monkeypatch.setattr(chrome_driver, "create_driver", boom)
    monkeypatch.setattr(chrome_driver, "safe_quit_driver", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["shared_key_flow.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(shared_key_flow.__file__, run_name="__main__")

    assert calls == {"chrome_path": None, "chrome_version": None}
    # The flow returned no key, and since this commit the command says so in
    # its exit status instead of reporting success.
    assert excinfo.value.code == 1


def test_the_command_reports_failure_in_its_exit_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`request_shared_key_flow` returns `None` for most failures.

    A caller that ignores that return exits 0 on a run that retrieved nothing,
    and a script around it cannot tell success from failure. Missing browser
    packages are now an ordinary reason to land here, so the name of the
    remedy is printed with it.
    """

    from custom_components.googlefindmy import browser_deps
    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    monkeypatch.setattr(
        flow,
        "_parse_cli_args",
        lambda: SimpleNamespace(chrome_path=None, chrome_version=None),
    )
    monkeypatch.setattr(flow, "request_shared_key_flow", lambda **_: None)
    monkeypatch.setattr(browser_deps, "browser_packages_missing", lambda: True)

    assert flow._main() == 1
    assert browser_deps.INSTALL_COMMAND in capsys.readouterr().err


def test_the_command_reports_an_expired_unlock_page_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unlock-page deadline ends the command with a message and exit 1."""

    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    def _expire(**_: Any) -> None:
        raise flow.SharedKeyFlowDeadlineError("alert_deadline")

    monkeypatch.setattr(
        flow,
        "_parse_cli_args",
        lambda: SimpleNamespace(chrome_path=None, chrome_version=None),
    )
    monkeypatch.setattr(flow, "request_shared_key_flow", _expire)

    assert flow._main() == 1
    err = capsys.readouterr().err
    assert f"within {flow.ALERT_DEADLINE_S // 60} minutes" in err
    assert "Traceback" not in err


def test_vault_flow_driver_init_failure_logs_the_type_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A driver error message may carry page content: log its type, no traceback."""

    def _fail(**_: Any) -> None:
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(shared_key_flow, "create_driver", _fail)

    result = shared_key_flow.request_vault_entries_flow()

    assert result.cause is VaultCause.HTTP_STATUS
    assert result.entries == ()
    records = [r for r in caplog.records if "vault entries flow" in r.getMessage()]
    assert len(records) == 1
    assert "RuntimeError" in records[0].getMessage()
    assert _SECRET not in records[0].getMessage()
    assert records[0].exc_info is None


def test_the_command_succeeds_when_a_key_comes_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Counterpart, so the exit status is not simply always 1."""

    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    monkeypatch.setattr(
        flow,
        "_parse_cli_args",
        lambda: SimpleNamespace(chrome_path=None, chrome_version=None),
    )
    monkeypatch.setattr(flow, "request_shared_key_flow", lambda **_: "aa" * 32)

    assert flow._main() == 0
    assert "aa" * 32 in capsys.readouterr().out


def test_the_unusable_package_type_is_not_turned_into_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other driver failure becomes `None`; this one must not.

    `undetected_chromedriver` installed but unimportable is invisible to a
    presence probe and unreadable from the message, so the type is the only
    carrier left. Erasing it here left the user with generic Chrome advice
    against a missing package.
    """

    from custom_components.googlefindmy import browser_deps, chrome_driver
    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    def _unusable(**_: Any) -> Any:
        raise browser_deps.BrowserPackagesUnusable("uc installed but broken")

    monkeypatch.setattr(chrome_driver, "create_driver", _unusable)
    monkeypatch.setattr(flow, "create_driver", _unusable)

    with pytest.raises(browser_deps.BrowserPackagesUnusable):
        flow.request_shared_key_flow()


def test_an_ordinary_driver_failure_still_becomes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart, so the pass-through is not simply "raise everything"."""

    from custom_components.googlefindmy.KeyBackup import shared_key_flow as flow

    def _boom(**_: Any) -> Any:
        raise RuntimeError("chrome crashed")

    monkeypatch.setattr(flow, "create_driver", _boom)

    assert flow.request_shared_key_flow() is None


# ---------------------------------------------------------------------------
# payload redaction (the alert channel carries vault key material)
# ---------------------------------------------------------------------------


_SECRET = "deadbeefcafebabe" * 4


def test_malformed_payload_is_logged_by_shape_not_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-JSON alert must never reach the log verbatim.

    The branch still has to say *what* arrived, otherwise a maintainer cannot
    tell an absent payload from an empty or misshapen one, so type and length
    are asserted alongside the absence of the value.
    """

    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([_SECRET, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()

    logged = " ".join(caplog.messages)
    assert "malformed alert payload" in logged.lower()
    assert _SECRET not in logged
    assert f"str len={len(_SECRET)}" in logged


def test_invalid_vault_keys_payload_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = json.dumps(
        {"method": "setVaultSharedKeys", "str": _SECRET, "vaultKeys": [_SECRET]}
    )
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([bad, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()

    logged = " ".join(str(record.getMessage()) for record in caplog.records)
    assert "invalid vaultkeys" in logged.lower()
    assert _SECRET not in logged
    assert "list len=1" in logged
    # key names, never values: the shape of an unrecognised payload cannot be
    # assumed, so no value is logged at all
    assert "keys=[method, str, vaultKeys]" in logged


def test_unhandled_payload_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    other = json.dumps({"method": "somethingElse", "vaultKeys": _SECRET})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([other, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()

    logged = " ".join(str(record.getMessage()) for record in caplog.records)
    assert "unhandled alert payload" in logged.lower()
    assert _SECRET not in logged
    assert "keys=[method, vaultKeys]" in logged


def test_an_unexpected_key_is_not_logged_either(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The branch exists for payloads whose shape nobody anticipated.

    Redacting by key name would leak anything stored under a name that is not on
    the list, which is exactly the case these branches handle.
    """

    import logging

    caplog.set_level(logging.DEBUG)
    other = json.dumps({"method": "somethingElse", "surpriseField": _SECRET})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([other, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()

    logged = " ".join(str(record.getMessage()) for record in caplog.records)
    assert _SECRET not in logged
    assert "surpriseField" in logged  # the name is the useful part, not the value


def test_cli_path_binds_the_redaction_helper_not_the_diagnostics_copy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The effective patch point for the CLI path is this module, not diagnostics.

    ``shared_key_flow`` binds ``async_redact_data`` with ``from ... import``, so
    the name lives in this module's namespace. A test that wants to observe the
    redaction of the CLI path patches *here*; patching
    ``diagnostics.async_redact_data`` (which several diagnostics tests do) has no
    effect on this path at all. Both halves are asserted so the layering cannot
    regress silently.
    """

    import logging

    from custom_components.googlefindmy import redaction

    caplog.set_level(logging.DEBUG)
    calls: list[object] = []

    def _tracking(data: object) -> object:
        calls.append(data)
        return "TRACED"

    monkeypatch.setattr(shared_key_flow, "describe_keys", _tracking)

    other = json.dumps({"method": "somethingElse"})
    good = json.dumps({"method": "closeView"})
    driver = _FakeDriver([other, good])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda _: None)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()
    assert calls, "the unhandled branch must route through the redaction helper"
    assert "TRACED" in " ".join(str(r.getMessage()) for r in caplog.records)

    # the helper the module bound is the shared one, not a diagnostics-local copy
    assert redaction.describe_keys.__module__.endswith("redaction")


# ---------------------------------------------------------------------------
# vault fetch in the same session (vault_sink, fetch_vault_entries)
# ---------------------------------------------------------------------------


_CLOSE_VIEW = json.dumps({"method": "closeView"})
_SET_KEYS = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": "raw-keys"})


@pytest.fixture
def world() -> VaultWorld:
    return make_vault_world(versions=(1, 2, 3), spot_version=1)


def _quitting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_key_flow, "safe_quit_driver", lambda d: d and d.quit())


def test_without_sink_the_vault_is_never_fetched(
    monkeypatch: pytest.MonkeyPatch, world: VaultWorld
) -> None:
    driver = _FakeDriver([_SET_KEYS], async_answer=vault_answer(world))
    _patch_flow(monkeypatch, driver, shared_key=world.shared_key)
    _quitting(monkeypatch)

    assert shared_key_flow.request_shared_key_flow() == world.shared_key.hex()
    assert FIND_MY_DEVICE_URL not in driver.visited_urls
    assert driver.async_scripts == []


def test_sink_gets_the_entries_once_before_the_unlock_page(
    monkeypatch: pytest.MonkeyPatch, world: VaultWorld
) -> None:
    driver = _FakeDriver([_SET_KEYS], async_answer=vault_answer(world))
    _patch_flow(monkeypatch, driver, shared_key=world.shared_key)
    _quitting(monkeypatch)
    received: list[VaultFetchResult] = []

    result = shared_key_flow.request_shared_key_flow(vault_sink=received.append)

    assert result == world.shared_key.hex()
    assert len(received) == 1
    assert received[0].cause is None
    assert {e.version: e.key for e in received[0].entries} == world.wrapped_owner_keys
    assert driver.visited_urls == [
        "https://accounts.google.com/",
        FIND_MY_DEVICE_URL,
        "https://sec",
    ]
    assert driver.quit_calls == 1


def test_raising_sink_still_returns_the_shared_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    world: VaultWorld,
) -> None:
    """A sink that raises must not end the flow through the outer handler."""

    import logging

    caplog.set_level(logging.DEBUG)
    driver = _FakeDriver([_SET_KEYS], async_answer=vault_answer(world))
    _patch_flow(monkeypatch, driver, shared_key=world.shared_key)
    _quitting(monkeypatch)

    def _sink(_result: VaultFetchResult) -> None:
        raise ValueError("sink exploded")

    assert shared_key_flow.request_shared_key_flow(vault_sink=_sink) == (
        world.shared_key.hex()
    )
    assert "https://sec" in driver.visited_urls
    logged = " ".join(caplog.messages)
    assert "Vault sink raised: ValueError" in logged
    assert "sink exploded" not in logged
    assert "terminated unexpectedly" not in logged


def test_failed_fetch_reaches_the_sink_and_the_flow_goes_on(
    monkeypatch: pytest.MonkeyPatch, world: VaultWorld
) -> None:
    driver = _FakeDriver([_SET_KEYS], async_answer=RuntimeError("script died"))
    _patch_flow(monkeypatch, driver, shared_key=world.shared_key)
    _quitting(monkeypatch)
    received: list[VaultFetchResult] = []

    result = shared_key_flow.request_shared_key_flow(vault_sink=received.append)

    assert result == world.shared_key.hex()
    assert [r.cause for r in received] == [VaultCause.HTTP_STATUS]
    assert received[0].entries == ()


def test_navigates_before_fetch(
    world: VaultWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`get(FIND_MY_DEVICE_URL)` comes first, with exactly that constant."""

    monkeypatch.setattr(shared_key_flow, "WebDriverWait", FlowWait)
    driver = _FakeDriver(async_answer=vault_answer(world))

    result = shared_key_flow.fetch_vault_entries(driver)  # type: ignore[arg-type]

    assert result.cause is None
    kinds = [kind for kind, _ in driver.calls]
    assert kinds == ["get", "set_script_timeout", "execute_async_script"]
    assert driver.calls[0] == ("get", FIND_MY_DEVICE_URL)
    assert driver.calls[1] == (
        "set_script_timeout",
        shared_key_flow.VAULT_FETCH_TIMEOUT_S,
    )
    assert driver.calls[2] == ("execute_async_script", FETCH_SCRIPT)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(None, id="no_answer"),
        pytest.param("text", id="not_a_dict"),
        pytest.param({"ok": False, "error": "TypeError"}, id="script_error"),
        pytest.param({"ok": False, "status": 403, "text": "denied"}, id="status_403"),
        pytest.param({"ok": "yes", "status": 200, "text": "x"}, id="ok_not_true"),
        pytest.param({"ok": True, "status": 200, "text": None}, id="text_missing"),
    ],
)
def test_unusable_answer_is_http_status(
    monkeypatch: pytest.MonkeyPatch, answer: object
) -> None:
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", FlowWait)
    driver = _FakeDriver(async_answer=answer)

    result = shared_key_flow.fetch_vault_entries(driver)  # type: ignore[arg-type]

    assert result == VaultFetchResult((), VaultCause.HTTP_STATUS)


def test_answer_text_goes_through_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", FlowWait)
    driver = _FakeDriver(async_answer={"ok": True, "status": 200, "text": "<html>"})

    result = shared_key_flow.fetch_vault_entries(driver)  # type: ignore[arg-type]

    assert result == VaultFetchResult((), VaultCause.NO_FRAME)


def test_navigation_failure_is_http_status_and_skips_the_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", FlowWait)
    driver = _FakeDriver(async_answer={"ok": True, "text": ""})
    driver.get_error = RuntimeError("net::ERR_NAME_NOT_RESOLVED")
    driver.get_error_url = FIND_MY_DEVICE_URL

    result = shared_key_flow.fetch_vault_entries(driver)  # type: ignore[arg-type]

    assert result == VaultFetchResult((), VaultCause.HTTP_STATUS)
    assert driver.async_scripts == []


def test_page_readiness_timeout_is_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from selenium.common.exceptions import TimeoutException

    class _NeverReady:
        def __init__(self, _driver: object, _timeout: float) -> None:
            pass

        def until(self, condition: Any) -> Any:
            condition(driver)  # the readiness probe runs in the page
            raise TimeoutException

    monkeypatch.setattr(shared_key_flow, "WebDriverWait", _NeverReady)
    driver = _FakeDriver(async_answer={"ok": True, "text": ""})

    result = shared_key_flow.fetch_vault_entries(driver)  # type: ignore[arg-type]

    assert result == VaultFetchResult((), VaultCause.HTTP_STATUS)
    assert driver.scripts == [shared_key_flow._WIZ_READY_SCRIPT]
    assert driver.async_scripts == []


# ---------------------------------------------------------------------------
# request_vault_entries_flow (sign-in only, no unlock page)
# ---------------------------------------------------------------------------


def _forbid_unlock_page(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    requested: list[int] = []

    def _security_url() -> str:
        requested.append(1)
        return "https://sec"

    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", _security_url
    )
    return requested


def test_vault_entries_flow_skips_the_unlock_page(
    monkeypatch: pytest.MonkeyPatch, world: VaultWorld
) -> None:
    driver = _FakeDriver(async_answer=vault_answer(world))
    _patch_flow(monkeypatch, driver)
    requested = _forbid_unlock_page(monkeypatch)
    _quitting(monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        shared_key_flow,
        "create_driver",
        lambda **kwargs: captured.update(kwargs) or driver,
    )

    result = shared_key_flow.request_vault_entries_flow(
        chrome_path="/opt/chrome", chrome_version=149
    )

    assert isinstance(result, VaultFetchResult)
    assert result.cause is None
    assert len(result.entries) == 3
    assert requested == []
    assert not any("setVaultSharedKeys" in script for script in driver.scripts)
    assert "https://sec" not in driver.visited_urls
    assert driver.visited_urls == ["https://accounts.google.com/", FIND_MY_DEVICE_URL]
    assert captured == {"chrome_path": "/opt/chrome", "chrome_version": 149}
    assert driver.quit_calls == 1


def test_vault_entries_flow_sign_in_timeout_is_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from selenium.common.exceptions import TimeoutException

    class _SignInTimesOut:
        def __init__(self, _driver: object, _timeout: float) -> None:
            pass

        def until(self, _condition: Any) -> Any:
            raise TimeoutException

    driver = _FakeDriver(async_answer={"ok": True, "text": ""})
    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **_: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", _SignInTimesOut)
    requested = _forbid_unlock_page(monkeypatch)
    _quitting(monkeypatch)

    result = shared_key_flow.request_vault_entries_flow()

    assert result == VaultFetchResult((), VaultCause.HTTP_STATUS)
    assert driver.visited_urls == ["https://accounts.google.com/"]
    assert requested == []
    assert driver.quit_calls == 1


def test_vault_entries_flow_driver_failure_is_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**_: Any) -> Any:
        raise RuntimeError("chrome crashed")

    monkeypatch.setattr(shared_key_flow, "create_driver", _boom)

    result = shared_key_flow.request_vault_entries_flow()

    assert result == VaultFetchResult((), VaultCause.HTTP_STATUS)


def test_vault_entries_flow_passes_unusable_packages_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy import browser_deps

    def _unusable(**_: Any) -> Any:
        raise browser_deps.BrowserPackagesUnusable("uc installed but broken")

    monkeypatch.setattr(shared_key_flow, "create_driver", _unusable)

    with pytest.raises(browser_deps.BrowserPackagesUnusable):
        shared_key_flow.request_vault_entries_flow()


# ---------------------------------------------------------------------------
# alert deadline
# ---------------------------------------------------------------------------


def test_alert_deadline_ends_the_flow_after_300_seconds(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No usable alert within 300 s on the injected clock: deadline error, one quit.

    The flow raises ``SharedKeyFlowDeadlineError`` only after the driver quit, so
    the login tool can name the timeout instead of the browser installation.

    The wait double advances the clock by each 0.5 s alert wait and stops a
    loop without a deadline after 10 000 waits, so that mutant ends red here
    instead of hanging.
    """

    clock = FakeClock()
    wait = NoAlertWait(clock)
    driver = _FakeDriver()
    monkeypatch.setattr(shared_key_flow, "create_driver", lambda **_: driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", wait)
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: "https://sec"
    )
    monkeypatch.setattr(shared_key_flow, "_monotonic", clock)
    _quitting(monkeypatch)

    with pytest.raises(SharedKeyFlowDeadlineError, match="within 300 s"):
        shared_key_flow.request_shared_key_flow()

    assert shared_key_flow.ALERT_DEADLINE_S == 300
    assert 300 <= clock.now < 300 + 0.5 + 1e-9
    assert wait.alert_waits == 600
    assert driver.quit_calls == 1
    logged = " ".join(caplog.messages)
    assert "cause=alert_deadline" in logged
    assert "terminated unexpectedly" not in logged
    out = capsys.readouterr()
    assert out.out == ""
    assert "alert" not in out.err.lower()


def test_alert_deadline_is_not_reached_by_a_quick_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Counterpart: an answer inside the deadline wins, no deadline cause."""

    driver = _FakeDriver([_CLOSE_VIEW])
    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "_monotonic", FakeClock())
    _quitting(monkeypatch)

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()
    assert "alert_deadline" not in " ".join(caplog.messages)
    assert driver.quit_calls == 1


# ---------------------------------------------------------------------------
# terminal in sign-in 2 of 2: notice before the wait, no traceback for the two
# known endings (host run 2026-11-27, F-H3 to F-H5)
# ---------------------------------------------------------------------------

_WAIT_MARK = "<<sign-in wait>>"


class _MarkedWait:
    """Sign-in waits print a marker, then behave as ``sign_in`` says.

    ``sign_in`` is ``"pass"`` (signed in), ``"expire"`` (a new
    ``TimeoutException`` like Selenium's deadline) or ``"predicate"`` (the
    predicate runs, so a driver command that times out propagates). Alert waits
    raise ``alert_error`` when set, else arm the next payload.
    """

    def __init__(self, sign_in: str = "pass", alert_error: BaseException | None = None):
        self.sign_in = sign_in
        self.alert_error = alert_error
        self.sign_in_waits = 0

    def __call__(self, driver: _FakeDriver, timeout: float) -> _MarkedWait:
        self._driver = driver
        self._timeout = timeout
        return self

    def until(self, condition: Any) -> Any:
        from selenium.common.exceptions import TimeoutException  # noqa: PLC0415

        if self._timeout == shared_key_flow.SIGN_IN_TIMEOUT_S:
            self.sign_in_waits += 1
            print(_WAIT_MARK)
            if self.sign_in == "expire":
                raise TimeoutException
            if self.sign_in == "predicate":
                return condition(self._driver)
            return True
        if self._timeout >= 1:
            return True
        if self.alert_error is not None:
            raise self.alert_error
        if not self._driver.arm_next_alert():
            raise _StopFlow
        return True


def _container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", "1")
    monkeypatch.setenv("GOOGLEFINDMY_NOVNC_URL", "https://192.0.2.7:7900")
    monkeypatch.setenv("GOOGLEFINDMY_NOVNC_PASSWORD", "pw-of-this-run")


def _flow_with_wait(
    monkeypatch: pytest.MonkeyPatch, driver: _FakeDriver, wait: _MarkedWait
) -> list[int]:
    created: list[int] = []

    def _create(**_: Any) -> _FakeDriver:
        created.append(1)
        return driver

    _patch_flow(monkeypatch, driver)
    monkeypatch.setattr(shared_key_flow, "create_driver", _create)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", wait)
    _quitting(monkeypatch)
    return created


def test_step_two_names_viewer_and_password_right_before_its_wait(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F-H4/F-H5: a resumed sign-in 2 of 2 must not wait in silence."""

    _container(monkeypatch)
    _flow_with_wait(monkeypatch, _FakeDriver([_CLOSE_VIEW]), _MarkedWait())

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()

    out = capsys.readouterr().out
    notice, mark, _ = out.partition(_WAIT_MARK)
    assert mark, out
    assert "[AuthFlow] Sign-in 2 of 2 needs you in the browser view:" in notice
    assert (
        "Open https://192.0.2.7:7900 in your browser (password: pw-of-this-run)."
        in notice
    )
    assert "screen lock PIN" in notice
    assert "stays quiet while the sign-in waits, for up to 5 minutes" in notice


def test_step_two_prints_no_notice_outside_the_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Home Assistant never sets the container flag; its log gets no banner."""

    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)
    _flow_with_wait(monkeypatch, _FakeDriver([_CLOSE_VIEW]), _MarkedWait())

    with pytest.raises(SharedKeyCancelledOnPage):
        shared_key_flow.request_shared_key_flow()

    assert "[AuthFlow] ====" not in capsys.readouterr().out


def test_vault_flow_names_viewer_before_its_wait(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sign-in-only vault fetch waits just as silently; same notice."""

    _container(monkeypatch)
    _flow_with_wait(monkeypatch, _FakeDriver(), _MarkedWait(sign_in="expire"))

    result = shared_key_flow._vault_entries_flow_once(
        chrome_path=None, chrome_version=None
    )

    assert result.cause is VaultCause.HTTP_STATUS
    notice, mark, _ = capsys.readouterr().out.partition(_WAIT_MARK)
    assert mark
    assert "One more Google sign-in is needed (no PIN this time):" in notice
    assert "(password: pw-of-this-run)" in notice


def test_expired_step_two_sign_in_ends_with_one_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-H3: running out of time is named, not dumped; the result stays None."""

    driver = _FakeDriver([_CLOSE_VIEW])
    _flow_with_wait(monkeypatch, driver, _MarkedWait(sign_in="expire"))

    assert shared_key_flow.request_shared_key_flow() is None

    out = capsys.readouterr().out
    assert "[AuthFlow] Sign-in 2 of 2 was not finished within 5 minutes." in out
    assert "run the login again to finish sign-in 2 of 2." in out
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert not [r for r in caplog.records if r.exc_info]
    assert driver.quit_calls == 1


def test_a_driver_command_timing_out_in_the_wait_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Counterpart: only the deadline is the user's; a stalled command is a defect."""

    from selenium.common.exceptions import TimeoutException  # noqa: PLC0415

    class _StalledDriver(_FakeDriver):
        @property
        def current_url(self) -> str:
            raise TimeoutException("timeout: Timed out receiving message from renderer")

    _flow_with_wait(monkeypatch, _StalledDriver(), _MarkedWait(sign_in="predicate"))

    assert shared_key_flow.request_shared_key_flow() is None

    assert "was not finished within" not in capsys.readouterr().out
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage() for r in errors] == [
        "Shared key flow terminated unexpectedly"
    ]
    assert errors[0].exc_info is not None


def _refused_driver_error() -> BaseException:
    """The error urllib3 raises after Ctrl+C ended the local ChromeDriver.

    Built from the real urllib3 types in the order urllib3 itself links them:
    ``MaxRetryError`` from ``NewConnectionError`` from ``ConnectionRefusedError``.
    """

    from urllib3.exceptions import MaxRetryError, NewConnectionError  # noqa: PLC0415

    try:
        try:
            raise ConnectionRefusedError(111, "Connection refused")
        except ConnectionRefusedError as refused:
            raise NewConnectionError(
                None,  # type: ignore[arg-type]
                "Failed to establish a new connection: [Errno 111] Connection refused",
            ) from refused
    except NewConnectionError as wrapped:
        try:
            raise MaxRetryError(None, "/session/s/alert/text", wrapped) from wrapped
        except MaxRetryError as err:
            return err


def test_unreachable_driver_ends_step_two_with_one_line_and_no_restart(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F-H3: Ctrl+C kills ChromeDriver first; the worker must not dump a traceback."""

    import logging  # noqa: PLC0415

    caplog.set_level(logging.WARNING)
    created = _flow_with_wait(
        monkeypatch, _FakeDriver(), _MarkedWait(alert_error=_refused_driver_error())
    )

    assert shared_key_flow.request_shared_key_flow() is None

    assert len(created) == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    warnings = [r for r in caplog.records if "no longer reachable" in r.getMessage()]
    assert len(warnings) == 1
    assert "(MaxRetryError)" in warnings[0].getMessage()
    assert warnings[0].exc_info is None


def test_unreachable_driver_detail_is_kept_for_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The traceback of a driver that died on its own is still there with --debug."""

    import logging  # noqa: PLC0415

    caplog.set_level(logging.DEBUG, logger=shared_key_flow.LOGGER.name)
    _flow_with_wait(
        monkeypatch, _FakeDriver(), _MarkedWait(alert_error=_refused_driver_error())
    )

    shared_key_flow.request_shared_key_flow()

    detail = [
        r for r in caplog.records if r.getMessage() == "Unreachable driver detail"
    ]
    assert len(detail) == 1
    assert detail[0].levelname == "DEBUG"
    assert detail[0].exc_info is not None


def test_a_failure_without_a_refused_connection_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Counterpart: any other failure in the loop stays on the traceback path."""

    _flow_with_wait(
        monkeypatch, _FakeDriver(), _MarkedWait(alert_error=RuntimeError("other"))
    )

    assert shared_key_flow.request_shared_key_flow() is None

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage() for r in errors] == [
        "Shared key flow terminated unexpectedly"
    ]
    assert errors[0].exc_info is not None


def test_driver_unreachable_survives_a_cyclic_exception_chain() -> None:
    """A chain that loops back on itself ends the walk instead of hanging."""

    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__context__ = second
    second.__context__ = first

    assert shared_key_flow._driver_unreachable(first) is False
    assert shared_key_flow._driver_unreachable(_refused_driver_error()) is True
