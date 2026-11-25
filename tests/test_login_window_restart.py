# tests/test_login_window_restart.py
"""A sign-in window closed by accident is opened again, at most twice.

Closing the Chrome window used to end the whole login with "Login cancelled",
although closing it is often an accident and the step had produced nothing yet
(L-9 S1). ``_run_oauth_flow_or_exit`` now runs the account sign-in through
``restart_on_closed_window``: a closed window is reported in the terminal and
opened again, twice at most; the third loss ends the run with today's message
and status 130. A crash and an expired wait are not restarted, and the window
the program closes itself after a successful sign-in never is (S2).

The tests drive the real ``request_oauth_account_token_flow`` through the real
CLI boundary; only the driver and the wait are doubles. Names introduced with
this change are looked up inside the tests, so the red probe against the old
code fails on an assertion, not on an import.
"""

from __future__ import annotations

from typing import Any

import pytest
from selenium.common.exceptions import (
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)

from custom_components.googlefindmy import main as cli_main
from custom_components.googlefindmy.Auth import auth_flow
from tests.test_auth_flow import FakeDriver, ImmediateWaitFactory

_TOKEN_COOKIE = {"value": "oauth2_4/TOKEN"}


class _ClosedWindowDriver(FakeDriver):
    """The user closes the window: the navigation reports the lost window."""

    def get(self, url: str) -> None:
        super().get(url)
        raise NoSuchWindowException("no such window: target window already closed")


class _CrashedDriver(FakeDriver):
    def get(self, url: str) -> None:
        super().get(url)
        raise WebDriverException("chrome not reachable")


class _ExpiringWaitFactory(ImmediateWaitFactory):
    """The user never finishes the form: the wait's own deadline expires."""

    def __call__(self, driver: FakeDriver, timeout: int) -> Any:
        class _Wait:
            def until(self, _predicate: Any) -> Any:
                raise TimeoutException("deadline")

        return _Wait()


def _drivers(
    monkeypatch: pytest.MonkeyPatch, sequence: list[FakeDriver], wait: Any = None
) -> list[FakeDriver]:
    """Hand out ``sequence`` one driver per ``create_driver`` call."""
    started: list[FakeDriver] = []

    def _create_driver(**_kwargs: Any) -> FakeDriver:
        driver = sequence[len(started)] if len(started) < len(sequence) else None
        if driver is None:
            pytest.fail("create_driver was called more often than the test allows")
        started.append(driver)
        return driver

    monkeypatch.setattr(auth_flow, "create_driver", _create_driver)
    monkeypatch.setattr(auth_flow, "WebDriverWait", wait or ImmediateWaitFactory())
    monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", "1")  # no stdin gate
    return started


def _run_cli_boundary() -> tuple[str, str | None]:
    return cli_main._run_oauth_flow_or_exit(
        lambda: auth_flow.request_oauth_account_token_flow(headless=False)
    )


def test_a_closed_first_window_is_reopened_twice_then_aborts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _drivers(
        monkeypatch,
        [_ClosedWindowDriver(cookie_after_wait=None) for _ in range(4)],
    )

    with pytest.raises(SystemExit) as exc:
        _run_cli_boundary()

    out = capsys.readouterr().out
    assert len(started) == 3, "two restarts, then the third loss ends the run"
    assert exc.value.code == 130
    assert "opening it again (1 of 2)" in out
    assert "opening it again (2 of 2)" in out
    assert "opening it again (3 of" not in out
    assert "The sign-in window 1 of 2 was closed before it finished" in out
    assert "Press Ctrl+C here to cancel." in out
    assert "Login cancelled: the browser window was closed" in out
    assert all(driver.quit_calls == 1 for driver in started)


def test_a_reopened_window_that_finishes_returns_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _drivers(
        monkeypatch,
        [
            _ClosedWindowDriver(cookie_after_wait=None),
            FakeDriver(cookie_after_wait=_TOKEN_COOKIE),
        ],
    )

    token, _email = _run_cli_boundary()

    assert token == "oauth2_4/TOKEN"
    assert len(started) == 2
    assert capsys.readouterr().out.count("opening it again") == 1


def test_a_crash_is_not_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    started = _drivers(monkeypatch, [_CrashedDriver(cookie_after_wait=None)])

    with pytest.raises(WebDriverException, match="chrome not reachable"):
        _run_cli_boundary()
    assert len(started) == 1


def test_an_expired_wait_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _drivers(
        monkeypatch,
        [FakeDriver(cookie_after_wait=None)],
        wait=_ExpiringWaitFactory(),
    )

    with pytest.raises(SystemExit) as exc:
        _run_cli_boundary()

    assert exc.value.code == 130
    assert len(started) == 1
    out = capsys.readouterr().out
    assert "No login completed within" in out
    assert "opening it again" not in out


def test_the_program_closing_the_window_never_restarts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S2: after the token the program closes the window itself, and says so."""
    started = _drivers(monkeypatch, [FakeDriver(cookie_after_wait=_TOKEN_COOKIE)])

    token, _email = _run_cli_boundary()

    out = capsys.readouterr().out
    assert token == "oauth2_4/TOKEN"
    assert len(started) == 1
    assert started[0].quit_calls == 1
    assert "opening it again" not in out
    assert "Sign-in 1 of 2 finished; this window closes now." in out
    announcement = out.index("Sign-in 1 of 2 finished; this window closes now.")
    assert out.index("Retrieved Account Token successfully.") < announcement


def test_a_closed_window_is_its_own_kind_of_abort() -> None:
    """Callers that only know LoginAborted keep working unchanged."""
    closed = auth_flow.LoginWindowClosed("x")
    assert isinstance(closed, auth_flow.LoginAborted)


def test_the_restart_budget_is_a_parameter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []

    def _always_closed() -> str:
        calls.append(1)
        raise auth_flow.LoginWindowClosed("closed")

    with pytest.raises(auth_flow.LoginWindowClosed):
        auth_flow.restart_on_closed_window(
            _always_closed, step=2, total=2, max_restarts=1
        )

    assert len(calls) == 2
    assert "The sign-in window 2 of 2 was closed" in capsys.readouterr().out
