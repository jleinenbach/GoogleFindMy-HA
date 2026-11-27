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


# ---------------------------------------------------------------------------
# README quotes (L-13, CA-N13)
#
# The docker-login guide quotes what the terminal prints when a sign-in window
# is closed, reopened, finished, cancelled on the Google page, or runs out of
# time. Each quoted block is rendered here through the same chain the tests
# above drive, and the block must be a run of *whole* output lines of that
# scenario, directly after its own lead-in. Whole lines, not a substring: a
# block that quotes half a sentence, or two lines from different places, would
# otherwise pass.
#
# Measured extent: the blocks listed in ``_README_QUOTES``. Every fenced block
# in "Browser closed by accident" must be one of them, so a new quote there
# without a scenario fails; in "Cancelling a login" only the listed ones are
# checked (the sign-in 1 of 2 and ``--reauth`` blocks have their own guards in
# ``tests/test_auth_flow.py`` and ``tests/test_main_cli_exit_paths.py``).
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from tests.test_auth_flow import _documented_quotes  # noqa: E402
from tests.test_docker_login_partial_bundle_notice import _notice  # noqa: E402

_CLOSED_SECTION = "Browser closed by accident"
_CANCEL_SECTION = "Cancelling a login"


def _step_one_restart_then_finish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    _drivers(
        monkeypatch,
        [
            _ClosedWindowDriver(cookie_after_wait=None),
            FakeDriver(cookie_after_wait=_TOKEN_COOKIE),
        ],
    )
    _run_cli_boundary()
    return _out(capsys)


async def _step_two_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sequence: list[StepTwoDriver],
    *,
    world: VaultWorld | None = None,
    base_world: bool = False,
) -> str:
    _step_two(monkeypatch, sequence, world=world)
    await _run(_CountingCache(_base(world) if base_world else _base()))
    return _out(capsys)


async def _step_two_reopened_during_vault(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    world = make_vault_world(versions=(1, 2), spot_version=2)
    second = StepTwoDriver([_SET_KEYS], async_answer=vault_answer(world))
    return await _step_two_output(
        monkeypatch, capsys, [_closed_during_fetch(), second], world=world
    )


async def _step_two_finished(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    return await _step_two_output(monkeypatch, capsys, [StepTwoDriver([_SET_KEYS])])


async def _step_two_closed_three_times(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    return await _step_two_output(
        monkeypatch, capsys, [_closed_at(_SECURITY_URL) for _ in range(3)]
    )


async def _step_two_cancelled_on_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    return await _step_two_output(monkeypatch, capsys, [StepTwoDriver([_CLOSE_VIEW])])


class _ExpiredSignInWait(FlowWait):
    """The sign-in wait's own deadline runs out; every other wait is ``FlowWait``'s."""

    def until(self, condition: Any) -> Any:
        if self.timeout == shared_key_flow.SIGN_IN_TIMEOUT_S:
            raise TimeoutException("deadline")
        return super().until(condition)


async def _step_two_sign_in_expired(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    started = _step_two(
        monkeypatch, [StepTwoDriver([_SET_KEYS])], wait=_ExpiredSignInWait
    )
    await _run(_CountingCache(_base()))
    # The guide also says the window is not opened again after the timeout.
    assert len(started) == 1
    return _out(capsys)


async def _vault_only_closed_three_times(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    world = make_vault_world(versions=(1, 2), spot_version=2)
    return await _step_two_output(
        monkeypatch,
        capsys,
        [_closed_at(_ACCOUNTS_URL), _closed_during_fetch(), _closed_during_fetch()],
        world=world,
        base_world=True,
    )


async def _step_one_reopened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _tmp: Path
) -> str:
    return _step_one_restart_then_finish(monkeypatch, capsys)


async def _partial_bundle_notice(
    _monkeypatch: pytest.MonkeyPatch, _capsys: pytest.CaptureFixture[str], tmp: Path
) -> str:
    # What a cancelled sign-in 2 of 2 leaves: tokens without the key. The guide
    # shows the container path from docker-compose.yml; the test runs the
    # function against a temporary file, so that one path is swapped.
    result = _notice(
        tmp, status=130, content='{"aas_token": "aas_et/X", "username": "u"}'
    )
    assert result.returncode == 0, result.stderr
    here = str(tmp / "secrets.json")
    assert result.stderr.count(here) == 1, result.stderr
    return result.stderr.replace(here, "/data/secrets.json")


# Scenario name -> what the terminal shows for it (stdout and stderr together).
_SCENARIOS = {
    "step-one-reopened": _step_one_reopened,
    "step-two-reopened-during-vault": _step_two_reopened_during_vault,
    "step-two-finished": _step_two_finished,
    "step-two-closed-three-times": _step_two_closed_three_times,
    "step-two-cancelled-on-page": _step_two_cancelled_on_page,
    "step-two-sign-in-expired": _step_two_sign_in_expired,
    "vault-only-closed-three-times": _vault_only_closed_three_times,
    "partial-bundle-notice": _partial_bundle_notice,
}


# (section, lead-in that ends right before the block, scenario that emits it)
_README_QUOTES = [
    (
        _CLOSED_SECTION,
        "Closing the sign-in window 1 of 2 the first time:",
        "step-one-reopened",
    ),
    (
        _CLOSED_SECTION,
        "Closing the sign-in window 2 of 2 the first time, also while it reads the "
        "account vault:",
        "step-two-reopened-during-vault",
    ),
    (_CLOSED_SECTION, "When sign-in 1 of 2 is done:", "step-one-reopened"),
    (_CLOSED_SECTION, "When sign-in 2 of 2 is done:", "step-two-finished"),
    (
        _CLOSED_SECTION,
        "the run says why they are missing:",
        "vault-only-closed-three-times",
    ),
    (
        _CANCEL_SECTION,
        "Closing the sign-in window 2 of 2 a third time, after it was opened again "
        "twice:",
        "step-two-closed-three-times",
    ),
    (
        _CANCEL_SECTION,
        "Pressing cancel on the Google unlock page during sign-in 2 of 2, which is "
        "not opened again because the page ended the step on purpose:",
        "step-two-cancelled-on-page",
    ),
    (
        _CANCEL_SECTION,
        "The line it prints when the time runs out:",
        "step-two-sign-in-expired",
    ),
    (
        _CANCEL_SECTION,
        "with the path inside the container in brackets:",
        "partial-bundle-notice",
    ),
]


def _is_whole_output_lines(block: str, output: str) -> bool:
    """True when ``block`` equals consecutive non-empty output lines, collapsed."""
    lines = [" ".join(line.split()) for line in output.splitlines()]
    lines = [line for line in lines if line]
    return any(
        " ".join(lines[start:stop]) == block
        for start in range(len(lines))
        for stop in range(start + 1, len(lines) + 1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "lead_in", "scenario"),
    _README_QUOTES,
    ids=[f"{section}: {scenario}" for section, _, scenario in _README_QUOTES],
)
async def test_the_guide_quotes_what_the_terminal_shows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    section: str,
    lead_in: str,
    scenario: str,
) -> None:
    output = await _SCENARIOS[scenario](monkeypatch, capsys, tmp_path)
    blocks = [
        block for prose, block in _documented_quotes(section) if prose.endswith(lead_in)
    ]

    assert len(blocks) == 1, (
        f"expected exactly one fenced block right after {lead_in!r} in "
        f"'## {section}', found {len(blocks)}"
    )
    # Guard against the vacuous pass: an empty block matches no line, but a
    # block holding one stray word could still be a whole line elsewhere.
    assert len(blocks[0]) > 40, blocks[0]
    assert _is_whole_output_lines(blocks[0], output), (
        f"the block after {lead_in!r} is not what the terminal shows for "
        f"{scenario}. Quote whole output lines, verbatim. Block: {blocks[0]!r}. "
        f"Output: {output!r}"
    )


def test_every_quote_in_the_closed_window_section_is_guarded() -> None:
    """A new quote in "Browser closed by accident" needs a scenario above."""
    guarded = [
        lead for section, lead, _ in _README_QUOTES if section == _CLOSED_SECTION
    ]
    quotes = _documented_quotes(_CLOSED_SECTION)

    assert quotes, "the section quotes nothing; the guard would pass vacuously"
    unguarded = [
        prose[-80:]
        for prose, _ in quotes
        if not any(prose.endswith(lead) for lead in guarded)
    ]
    assert not unguarded, f"fenced blocks without a rendering scenario: {unguarded}"


def _desktop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The desktop path: the stdin gate is active; return the prompts it shows."""
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)
    monkeypatch.setattr(auth_flow, "_stdin_is_attended", lambda: True)
    prompts: list[str] = []

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", _input)
    return prompts


def test_a_reopened_desktop_window_opens_without_asking_for_enter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restart says "opening it again" and does so (review F1, PR #1482).

    On the desktop the whole sign-in function used to run again, gate included:
    the instructions were printed a second time and nothing opened until Enter
    was pressed, although the line above it promised a new window and offered
    only Ctrl+C. Driven the way ``_ensure_authenticated`` calls it.
    """
    started = _drivers(
        monkeypatch,
        [
            _ClosedWindowDriver(cookie_after_wait=None),
            FakeDriver(cookie_after_wait=_TOKEN_COOKIE),
        ],
    )
    prompts = _desktop(monkeypatch)

    token, _email = cli_main._run_oauth_flow_or_exit(
        auth_flow.request_oauth_account_token_flow
    )

    out = capsys.readouterr().out
    assert token == "oauth2_4/TOKEN"
    assert len(started) == 2
    assert prompts == ["[AuthFlow] Press Enter to continue..."], (
        f"asked {len(prompts)} times; the reopened window must not wait for Enter"
    )
    assert out.count("This script will now open Google Chrome") == 1
    assert "opening it again (1 of 2)" in out


def test_a_new_sign_in_after_a_restart_asks_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skipping the gate holds for the reopened window only, not for later runs."""
    _drivers(
        monkeypatch,
        [
            _ClosedWindowDriver(cookie_after_wait=None),
            FakeDriver(cookie_after_wait=_TOKEN_COOKIE),
            FakeDriver(cookie_after_wait=_TOKEN_COOKIE),
        ],
    )
    prompts = _desktop(monkeypatch)

    cli_main._run_oauth_flow_or_exit(auth_flow.request_oauth_account_token_flow)
    auth_flow.request_oauth_account_token_flow()

    assert len(prompts) == 2
    assert capsys.readouterr().out.count("This script will now open Google Chrome") == 2


# ---------------------------------------------------------------------------
# F-H2: a closed sign-in-2 window can report its address as ``None``
#
# On the QNAP host (Selenium through the grid), closing the window of sign-in 2
# did not make the sign-in wait raise: ``driver.current_url`` returned ``None``
# and ``ec.url_contains`` failed with ``TypeError``, which the flow logged as
# "terminated unexpectedly" and ended with exit 1 instead of reopening the
# window. The doubles above raise on the next command, so they never reached the
# wait's predicate. These tests run the real predicate: the wait double below
# evaluates it once instead of returning at once.
# ---------------------------------------------------------------------------

_SIGNED_IN_URL = "https://myaccount.google.com/?pli=1"


class _AddressDriver(StepTwoDriver):
    """A step-two driver that answers ``current_url`` and ``window_handles``."""

    def __init__(
        self,
        payloads: list[str] | None = None,
        *,
        url: str | None,
        handles: list[str] | None,
        async_answer: object = None,
    ) -> None:
        super().__init__(payloads, async_answer=async_answer)
        self._url = url
        self._handles = handles

    @property
    def current_url(self) -> str | None:
        return self._url

    @property
    def window_handles(self) -> list[str] | None:
        return None if self._handles is None else list(self._handles)


def _closed_with_none_url() -> _AddressDriver:
    """The user closed the window; the driver says ``None`` instead of raising."""
    return _AddressDriver(url=None, handles=[])


class _PredicateWait(FlowWait):
    """Runs the sign-in wait's predicate once; every other wait is ``FlowWait``'s.

    The sign-in waits are the ones with the sign-in timeout; the page-readiness
    wait of the vault fetch and the 0.5 s alert waits keep resolving at once.
    """

    def until(self, condition: Any) -> Any:
        if self.timeout != shared_key_flow.SIGN_IN_TIMEOUT_S:
            return super().until(condition)
        result = condition(self.driver)
        if not result:
            pytest.fail("the sign-in wait would keep polling forever in this test")
        return result


@pytest.mark.asyncio
async def test_a_second_window_closed_with_a_none_address_is_reopened_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _step_two(
        monkeypatch,
        [_closed_with_none_url() for _ in range(3)],
        wait=_PredicateWait,
    )

    code = await _run(_CountingCache(_base()))
    out = _out(capsys)

    assert code == 130
    assert len(started) == 3
    assert out.count(_RESTART_TWO) == 2
    assert "The sign-in window 2 of 2 was closed before Google sent the key." in out
    assert "terminated unexpectedly" not in out
    assert "vault key retrieval failed" not in out


@pytest.mark.asyncio
async def test_a_reopened_window_after_a_none_address_saves_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    second = _AddressDriver([_SET_KEYS], url=_SIGNED_IN_URL, handles=["w1"])
    started = _step_two(
        monkeypatch, [_closed_with_none_url(), second], wait=_PredicateWait
    )
    cache = _CountingCache(_base())

    assert await _run(cache) is None
    out = _out(capsys)

    assert len(started) == 2
    assert out.count(_RESTART_TWO) == 1
    assert cache.data["shared_key"] == bytes(range(32)).hex()


@pytest.mark.asyncio
async def test_path_c_window_closed_with_a_none_address_names_the_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = make_vault_world(versions=(1, 2), spot_version=2)
    started = _step_two(
        monkeypatch,
        [_closed_with_none_url() for _ in range(3)],
        world=world,
        wait=_PredicateWait,
    )
    cache = _CountingCache(_base(world))

    assert await _run(cache) is None
    out = _out(capsys)

    assert len(started) == 3
    assert out.count(_RESTART_TWO) == 2
    assert "cause=window_closed" in out
    assert "http_status" not in out


def _sign_in_predicate() -> Any:
    # Looked up here, so the red probe against the old code fails in the test.
    return shared_key_flow._signed_in_or_window_gone


def test_the_sign_in_predicate_keeps_waiting_while_a_window_is_open() -> None:
    driver = _AddressDriver(url=None, handles=["w1"])
    assert _sign_in_predicate()(driver) is False


def test_the_sign_in_predicate_reports_a_window_gone_without_an_address() -> None:
    with pytest.raises(NoSuchWindowException) as caught:
        _sign_in_predicate()(_closed_with_none_url())
    assert auth_flow.describe_lost_session(caught.value) is not None


@pytest.mark.parametrize(
    ("url", "expected"),
    [(_SIGNED_IN_URL, True), (_ACCOUNTS_URL, False)],
    ids=["signed_in", "still_on_the_form"],
)
def test_the_sign_in_predicate_reads_the_address(url: str, expected: bool) -> None:
    driver = _AddressDriver(url=url, handles=["w1"])
    assert _sign_in_predicate()(driver) is expected


def test_the_sign_in_predicate_reports_no_window_list_as_window_gone() -> None:
    driver = _AddressDriver(url=None, handles=None)
    with pytest.raises(NoSuchWindowException):
        _sign_in_predicate()(driver)


class _CrashedOnHandlesDriver(_AddressDriver):
    """``None`` address, and asking for the windows reports a crash."""

    @property
    def window_handles(self) -> list[str]:
        raise WebDriverException("chrome not reachable")


def test_a_crash_while_asking_for_the_windows_keeps_its_traceback_path() -> None:
    driver = _CrashedOnHandlesDriver(url=None, handles=[])
    with pytest.raises(WebDriverException) as caught:
        _sign_in_predicate()(driver)
    assert not isinstance(caught.value, NoSuchWindowException)
    assert auth_flow.describe_lost_session(caught.value) is None
    assert shared_key_flow._failure_cause(caught.value) == "http_status"
