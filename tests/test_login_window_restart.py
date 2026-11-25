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


# ---------------------------------------------------------------------------
# Sign-in 2 of 2 (L-9 S3, S3v, S3c, S4, S4b, S-DL; L-9a)
#
# The real chain from ``main._ensure_vault_keys`` through
# ``KeyBackup.shared_key_retrieval`` into ``KeyBackup.shared_key_flow``; only
# the driver, the waits, the key parser and the Spot request are doubles. The
# verdicts are read from the terminal output and the exit status, which is what
# the user sees, so the red probe against the old code fails on an assertion.
# ---------------------------------------------------------------------------

import json  # noqa: E402
import sys  # noqa: E402

from custom_components.googlefindmy.KeyBackup import (  # noqa: E402
    shared_key_flow,
)
from custom_components.googlefindmy.KeyBackup import (  # noqa: E402
    shared_key_retrieval as skr,
)
from tests.helpers.owner_key_vault_fixtures import (  # noqa: E402
    VaultWorld,
    make_vault_world,
)
from tests.helpers.shared_key_flow_doubles import (  # noqa: E402
    FakeClock,
    FlowWait,
    NoAlertWait,
    vault_answer,
)
from tests.helpers.shared_key_flow_doubles import (  # noqa: E402
    FakeDriver as StepTwoDriver,
)
from tests.test_main_owner_key_versions import (  # noqa: E402
    _base,
    _CountingCache,
    _run,
    _spot_serving,
    _Stdin,
)

_SECURITY_URL = "https://sec"
_ACCOUNTS_URL = "https://accounts.google.com/"
_CLOSED = "no such window: target window already closed"
_SET_KEYS = json.dumps({"method": "setVaultSharedKeys", "vaultKeys": "opaque"})
_CLOSE_VIEW = json.dumps({"method": "closeView"})
_RESTART_TWO = "The sign-in window 2 of 2 was closed before it finished"


def _closed_at(url: str, *, payloads: list[str] | None = None) -> StepTwoDriver:
    """A driver whose navigation to ``url`` reports a window the user closed."""
    driver = StepTwoDriver(payloads)
    driver.get_error = NoSuchWindowException(_CLOSED)
    driver.get_error_url = url
    return driver


def _closed_during_fetch() -> StepTwoDriver:
    """The window goes while the vault is read; every later command fails too."""
    driver = _closed_at(_SECURITY_URL)
    driver.async_answer = NoSuchWindowException(_CLOSED)
    return driver


def _step_two(
    monkeypatch: pytest.MonkeyPatch,
    sequence: list[StepTwoDriver],
    *,
    world: VaultWorld | None = None,
    wait: Any = FlowWait,
) -> list[StepTwoDriver]:
    """Wire sign-in 2 of 2 to ``sequence``, one driver per browser start."""
    started: list[StepTwoDriver] = []

    def _create_driver(**_kwargs: Any) -> StepTwoDriver:
        if len(started) >= len(sequence):
            pytest.fail("create_driver was called more often than the test allows")
        started.append(sequence[len(started)])
        return started[-1]

    async def _passthrough(func: Any, *args: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(shared_key_flow, "create_driver", _create_driver)
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", wait)
    monkeypatch.setattr(
        shared_key_flow, "get_security_domain_request_url", lambda: _SECURITY_URL
    )
    shared_key = world.shared_key if world is not None else bytes(range(32))
    monkeypatch.setattr(shared_key_flow, "get_fmdn_shared_key", lambda _: shared_key)
    monkeypatch.setattr(skr, "_run_in_executor", _passthrough)
    monkeypatch.setattr(skr, "_browser_flow_attempted", False)
    monkeypatch.setenv("GOOGLEFINDMY_CLI_PROCESS", "1")
    monkeypatch.delenv("GOOGLEFINDMY_ASSUME_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _Stdin(True))
    if world is not None:
        from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (  # noqa: PLC0415
            get_owner_key as gok,
        )

        serve = _spot_serving(world)

        async def _spot(*, cache: object, username: str | None) -> Any:
            return serve(cache=cache, username=username)

        monkeypatch.setattr(gok, "async_fetch_owner_key_uncached", _spot)
    return started


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


@pytest.mark.asyncio
async def test_a_closed_second_window_is_reported_as_closed_not_as_missing_chrome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """L-9a: the last loss names the window and the kept step, never Chrome."""
    _step_two(monkeypatch, [_closed_at(_SECURITY_URL) for _ in range(3)])
    cache = _CountingCache(_base())

    code = await _run(cache)
    out = _out(capsys)

    assert code == 130
    assert "The sign-in window 2 of 2 was closed before Google sent the key." in out
    assert "The result of sign-in 1 of 2 is kept" in out
    assert "Chrome" not in out
    assert "vault key retrieval failed" not in out
    assert cache.data.get("shared_key") is None


@pytest.mark.asyncio
async def test_a_closed_second_window_is_reopened_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _step_two(monkeypatch, [_closed_at(_SECURITY_URL) for _ in range(3)])

    await _run(_CountingCache(_base()))
    out = _out(capsys)

    assert len(started) == 3
    assert all(driver.quit_calls == 1 for driver in started)
    assert out.count(_RESTART_TWO) == 2
    assert "opening it again (1 of 2)" in out
    assert "opening it again (2 of 2)" in out
    # One retrieval, however many windows: the guard is not reset in between.
    assert skr._browser_flow_attempted is True


@pytest.mark.asyncio
async def test_a_reopened_second_window_that_finishes_saves_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S3 then S4: the second window delivers the key and announces its closing."""
    started = _step_two(
        monkeypatch, [_closed_at(_SECURITY_URL), StepTwoDriver([_SET_KEYS])]
    )
    cache = _CountingCache(_base())

    assert await _run(cache) is None
    out = _out(capsys)

    assert len(started) == 2
    assert out.count(_RESTART_TWO) == 1
    assert cache.data["shared_key"] == bytes(range(32)).hex()
    assert "Sign-in 2 of 2 finished; this window closes now." in out
    assert started[1].quit_calls == 1


@pytest.mark.asyncio
async def test_close_view_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S4b: a cancel on the Google page ends the step; nothing reopens."""
    started = _step_two(monkeypatch, [StepTwoDriver([_CLOSE_VIEW])])

    code = await _run(_CountingCache(_base()))
    out = _out(capsys)

    assert code == 130
    assert len(started) == 1
    assert "was cancelled on the Google page" in out
    assert "The result of sign-in 1 of 2 is kept" in out
    assert "opening it again" not in out
    assert "Chrome" not in out


@pytest.mark.asyncio
async def test_window_closed_during_vault_fetch_restarts_step_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S3v: the reopened window's vault result is the one that is saved.

    The sink keeps only the first result it gets, so the closed fetch must not
    reach it; otherwise the good second fetch would be dropped.
    """
    world = make_vault_world(versions=(1, 2), spot_version=2)
    second = StepTwoDriver([_SET_KEYS], async_answer=vault_answer(world))
    started = _step_two(monkeypatch, [_closed_during_fetch(), second], world=world)
    cache = _CountingCache(_base())

    assert await _run(cache) is None
    out = _out(capsys)

    assert len(started) == 2
    assert out.count(_RESTART_TWO) == 1
    assert "Owner-key versions saved (2 version(s))." in out
    assert cache.field_sets() == 1
    assert "cause=" not in out


@pytest.mark.asyncio
async def test_path_c_window_closed_names_the_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S3c: the vault-only sign-in is reopened twice, then the window is named.

    Each driver loses its window at a different command: the first navigation,
    the vault fetch twice.
    """
    world = make_vault_world(versions=(1, 2), spot_version=2)
    first = _closed_at(_ACCOUNTS_URL)
    started = _step_two(
        monkeypatch,
        [first, _closed_during_fetch(), _closed_during_fetch()],
        world=world,
    )
    cache = _CountingCache(_base(world))

    assert await _run(cache) is None  # non-fatal, like every vault failure
    out = _out(capsys)

    assert len(started) == 3
    assert out.count(_RESTART_TWO) == 2
    assert (
        "The sign-in window 2 of 2 was closed before the account vault was read." in out
    )
    assert "cause=window_closed" in out
    assert "http_status" not in out
    assert cache.field_sets() == 0


@pytest.mark.asyncio
async def test_path_c_reopened_window_that_finishes_saves_the_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = make_vault_world(versions=(1, 2), spot_version=2)
    second = StepTwoDriver(async_answer=vault_answer(world))
    started = _step_two(monkeypatch, [_closed_during_fetch(), second], world=world)
    cache = _CountingCache(_base(world))

    assert await _run(cache) is None
    out = _out(capsys)

    assert len(started) == 2
    assert out.count(_RESTART_TWO) == 1
    assert "Owner-key versions saved (2 version(s))." in out
    assert cache.field_sets() == 1


@pytest.mark.asyncio
async def test_deadline_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S-DL: the 300 s deadline keeps today's path; no new window opens."""
    clock = FakeClock()
    started = _step_two(monkeypatch, [StepTwoDriver()], wait=NoAlertWait(clock))
    monkeypatch.setattr(shared_key_flow, "_monotonic", clock)

    code = await _run(_CountingCache(_base()))
    out = _out(capsys)

    assert code == 1
    assert len(started) == 1
    assert "opening it again" not in out


@pytest.mark.asyncio
async def test_a_crashed_second_window_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash is a defect, not a decision: no restart, no "you closed it"."""
    crashed = StepTwoDriver()
    crashed.get_error = WebDriverException("chrome not reachable")
    crashed.get_error_url = _SECURITY_URL
    started = _step_two(monkeypatch, [crashed])

    code = await _run(_CountingCache(_base()))
    out = _out(capsys)

    assert code == 1
    assert len(started) == 1
    assert "opening it again" not in out
    assert "was closed" not in out


def test_the_vault_fetch_tells_a_closed_window_from_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_key_flow, "WebDriverWait", FlowWait)
    closed = StepTwoDriver(async_answer=NoSuchWindowException(_CLOSED))
    crashed = StepTwoDriver(async_answer=WebDriverException("chrome not reachable"))

    assert shared_key_flow.fetch_vault_entries(closed).cause == "window_closed"
    assert shared_key_flow.fetch_vault_entries(crashed).cause == "http_status"


@pytest.mark.parametrize(
    ("drivers", "expected"),
    [
        (lambda: [StepTwoDriver([_CLOSE_VIEW])], "was cancelled on the Google page"),
        (
            lambda: [_closed_at(_SECURITY_URL) for _ in range(3)],
            "The sign-in window 2 of 2 was closed",
        ),
    ],
    ids=["close_view", "closed_three_times"],
)
def test_the_standalone_helper_reports_the_user_ending_step_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    drivers: Any,
    expected: str,
) -> None:
    _step_two(monkeypatch, drivers())
    monkeypatch.setattr(sys, "argv", ["shared_key_flow"])

    assert shared_key_flow._main() == 130
    assert expected in capsys.readouterr().err


def test_a_non_driver_failure_in_step_two_is_neither_restarted_nor_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A defect after sign-in (here the key parser) keeps today's `None`."""
    started = _step_two(monkeypatch, [StepTwoDriver([_SET_KEYS])])

    def _broken_parser(_vault_keys: str) -> bytes:
        raise ValueError("unexpected key layout")

    monkeypatch.setattr(shared_key_flow, "get_fmdn_shared_key", _broken_parser)

    assert shared_key_flow.request_shared_key_flow() is None
    assert len(started) == 1
    assert started[0].quit_calls == 1
    assert "opening it again" not in capsys.readouterr().out
