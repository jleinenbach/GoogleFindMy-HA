# tests/test_main_cli_single_publish.py
"""The standalone login publishes its credential bundle in one write.

Home Assistant's watcher offers a card whenever ``secrets.json`` changes. The
login used to store the vault keys, the owner-key versions and the push
credentials one by one, so a single run changed the file four times within a
minute and the first versions had no ``shared_key``: the user saw several cards
for one sign-in (L-9b). ``_run_cli_bootstrap`` now runs those steps inside
``_FileCache.deferred_writes()``; the AAS token in front of them is still written
at once, because a later failure must not lose it.

The steps are doubles that write through the cache's public interface, the cache
and its file are real, and ``_atomic_write_json`` is counted, not replaced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

# A registration as the receiver stores it: both receivers read the push token
# from ``fcm.registration.token`` only.
_STORED_PUSH_DATA: dict[str, Any] = {
    "gcm": {"android_id": "1", "security_token": "2"},
    "fcm": {"registration": {"token": "push-token"}},
}


def _reset_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe the module-global TokenCache registry so tests don't leak."""
    from custom_components.googlefindmy.Auth import token_cache

    monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
    monkeypatch.setitem(token_cache._STATE, "default_entry_id", None)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Fcm:
    async def async_stop(self) -> None:
        return None


def _bootstrap_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial: dict[str, Any]
) -> tuple[Any, Path, list[dict[str, Any]]]:
    """Return a real file cache on ``tmp_path`` and the list of written payloads."""
    import aiohttp

    from custom_components.googlefindmy import main as cli_main
    from custom_components.googlefindmy.NovaApi.ListDevices import (
        nbe_list_devices as nbe,
    )

    _reset_registry(monkeypatch)
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(path))

    writes: list[dict[str, Any]] = []
    real_write = cli_main._atomic_write_json

    def _counting_write(target: Path, data: dict[str, Any]) -> None:
        writes.append(json.loads(json.dumps(data)))
        real_write(target, data)

    monkeypatch.setattr(cli_main, "_atomic_write_json", _counting_write)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Session())
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **k: object())

    async def _cli_main(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nbe, "_async_cli_main", _cli_main)
    cache = cli_main._register_file_cache("")
    return cache, path, writes


def _install_steps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    aas: str = "aas_et/TOKEN",
    shared_key: str = "aa" * 32,
    fcm_credentials: dict[str, Any] | None = None,
    vault_exit: bool = False,
    seen_on_disk_at_vault: list[dict[str, Any]] | None = None,
) -> None:
    from custom_components.googlefindmy import main as cli_main

    async def _aas(cache: Any) -> None:
        await cache.set("aas_token", aas)

    async def _vault(cache: Any) -> None:
        if seen_on_disk_at_vault is not None:
            path = Path(cli_main._resolve_secrets_path())
            seen_on_disk_at_vault.append(json.loads(path.read_text(encoding="utf-8")))
        await cache.set("shared_key", shared_key)
        await cache.set("owner_key", "bb" * 32)
        await cache.set("owner_key_versions", [1])
        if vault_exit:
            raise SystemExit(1)

    async def _clear_adm(cache: Any) -> None:
        cache.sync_pop("adm_token_user@example.com")

    async def _fcm(cache: Any) -> _Fcm:
        # Like the receiver: the registration, then its routing entry.
        await cache.set("fcm_credentials", fcm_credentials or _STORED_PUSH_DATA)
        await cache.set("fcm_routing_tokens", ["push-token"])
        return _Fcm()

    monkeypatch.setattr(cli_main, "_ensure_aas_token", _aas)
    monkeypatch.setattr(cli_main, "_ensure_vault_keys", _vault)
    monkeypatch.setattr(cli_main, "_clear_stale_adm_token", _clear_adm)
    monkeypatch.setattr(cli_main, "_setup_fcm_receiver", _fcm)


@pytest.mark.asyncio
async def test_one_login_run_writes_keys_and_push_data_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keys, versions and push data reach the file in exactly one write."""
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {
            "username": "user@example.com",
            "oauth_token": "oauth2_4/X",
            "adm_token_user@example.com": "stale",
        },
    )
    _install_steps(monkeypatch)

    await cli_main._run_cli_bootstrap(cache, "")

    with_key = [w for w in writes if "shared_key" in w]
    assert len(with_key) == 1, (
        f"the key steps wrote {len(with_key)} times; Home Assistant offers a card "
        "for each write"
    )
    published = with_key[0]
    assert published["fcm_credentials"] == _STORED_PUSH_DATA
    assert published["fcm_routing_tokens"] == ["push-token"]
    assert published["owner_key_versions"] == [1]
    assert "adm_token_user@example.com" not in published
    # The AAS token is written before the block, on its own.
    assert len(writes) == 2
    assert writes[0]["aas_token"] == "aas_et/TOKEN"
    assert "shared_key" not in writes[0]
    assert json.loads(path.read_text(encoding="utf-8")) == published


@pytest.mark.asyncio
async def test_an_unchanged_bundle_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that stores what is already there leaves the file untouched.

    The watcher compares modification times; a rewrite with the same content
    would still look like a new bundle.
    """
    from custom_components.googlefindmy import main as cli_main

    complete = {
        "username": "user@example.com",
        "aas_token": "aas_et/TOKEN",
        "shared_key": "aa" * 32,
        "owner_key": "bb" * 32,
        "owner_key_versions": [1],
        "fcm_credentials": _STORED_PUSH_DATA,
        "fcm_routing_tokens": ["push-token"],
    }
    cache, path, writes = _bootstrap_world(monkeypatch, tmp_path, complete)
    _install_steps(monkeypatch)

    async def _aas_cached(_cache: Any) -> None:
        return None  # the token is cached; the real step returns early

    monkeypatch.setattr(cli_main, "_ensure_aas_token", _aas_cached)
    before = path.stat().st_mtime_ns

    await cli_main._run_cli_bootstrap(cache, "")

    assert writes == []
    assert path.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_an_abort_after_the_token_keeps_the_token_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The AAS token is on disk before the key steps start, and stays there.

    It is what the next run, and the --reauth restore, read from the file. A
    failed key step still persists what it stored before failing: the block
    writes on SystemExit too.
    """
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    on_disk: list[dict[str, Any]] = []
    _install_steps(monkeypatch, vault_exit=True, seen_on_disk_at_vault=on_disk)

    with pytest.raises(SystemExit):
        await cli_main._run_cli_bootstrap(cache, "")

    assert on_disk and on_disk[0].get("aas_token") == "aas_et/TOKEN"
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["aas_token"] == "aas_et/TOKEN"
    assert final["shared_key"] == "aa" * 32
    assert "fcm_credentials" not in final
    assert len(writes) == 2


def test_nested_blocks_write_once_at_the_outermost_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An inner block does not publish early; only the outermost one writes."""
    cache, _path, writes = _bootstrap_world(monkeypatch, tmp_path, {})

    with cache.deferred_writes():
        cache.sync_set("a", 1)
        with cache.deferred_writes():
            cache.sync_set("b", 2)
        assert writes == []
        cache.sync_set("c", 3)
    assert writes == [{"a": 1, "b": 2, "c": 3}]

    cache.sync_set("d", 4)
    assert len(writes) == 2  # outside the block every change writes again


def test_a_block_that_restores_the_content_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changing a value and putting it back is no change for the watcher."""
    cache, _path, writes = _bootstrap_world(monkeypatch, tmp_path, {"a": 1})

    with cache.deferred_writes():
        cache.sync_set("a", 2)
        cache.sync_set("a", 1)
    assert writes == []


def test_a_value_that_cannot_be_compared_still_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a comparable snapshot the block writes rather than guess."""
    from custom_components.googlefindmy import main as cli_main

    cache, _path, _writes = _bootstrap_world(monkeypatch, tmp_path, {"a": 1})
    calls: list[str] = []
    monkeypatch.setattr(
        cli_main, "_atomic_write_json", lambda _p, data: calls.append(sorted(data))
    )

    with cache.deferred_writes():
        cache.sync_set("blob", object())
    assert calls == [["a", "blob"]]


def test_a_cache_without_the_block_is_left_as_it_is() -> None:
    """Other caches get a no-op block instead of an AttributeError."""
    from custom_components.googlefindmy import main as cli_main

    with cli_main._deferred_writes(object()):
        pass


class _RegisteringPushClient:
    """Stands in for ``firebase_messaging``'s push client at its documented seam.

    The real client registers over the network and reports the new
    registration through ``credentials_updated_callback`` from inside
    ``checkin_or_register``. The sleep stands for that round trip: the push
    data exist only after the receiver's supervisor has run for a while, which
    is what the step double above hid by writing them itself.
    """

    def __init__(
        self,
        _on_notification: object,
        _config: object,
        credentials: object,
        credentials_updated_callback: Any,
        **_kwargs: object,
    ) -> None:
        self.credentials = credentials
        self._updated = credentials_updated_callback
        self.run_state: object = None
        self.do_listen = False

    async def checkin_or_register(self) -> str:
        import asyncio

        await asyncio.sleep(0.05)
        self.credentials = {
            "gcm": {"android_id": "1", "security_token": "2"},
            "fcm": {"registration": {"token": "push-token"}},
        }
        self._updated(self.credentials)
        return "push-token"

    async def start(self) -> None:
        from custom_components.googlefindmy.Auth.fcm_receiver_ha import (
            FcmPushClientRunState,
        )

        self.run_state = FcmPushClientRunState.STARTED

    async def stop(self) -> None:
        self.run_state = None


def _real_receiver_with_fake_push_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real ``_setup_fcm_receiver``; only the push client is a double."""
    from custom_components.googlefindmy import api
    from custom_components.googlefindmy import main as cli_main
    from custom_components.googlefindmy.Auth import fcm_receiver_ha
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        location_request,
    )

    real_setup = cli_main._setup_fcm_receiver
    _install_steps(monkeypatch)
    monkeypatch.setattr(cli_main, "_setup_fcm_receiver", real_setup)
    monkeypatch.setattr(fcm_receiver_ha, "FcmPushClient", _RegisteringPushClient)
    monkeypatch.setattr(fcm_receiver_ha, "HAVE_FCM_PUSH_CLIENT", True)
    # The setup registers the receiver as a process-wide provider.
    monkeypatch.setitem(location_request._fcm_receiver_state, "getter", None)
    monkeypatch.setattr(api, "_FCM_ReceiverGetter", None, raising=False)


@pytest.mark.asyncio
async def test_the_real_receiver_s_push_data_land_in_the_keys_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh login publishes keys and push data together (Codex, PR #1482).

    The receiver's ``async_initialize`` creates no client; the supervisor that
    registers is only scheduled by ``register_coordinator``. Leaving the block
    right after ``_setup_fcm_receiver`` therefore wrote the keys without push
    data, and the push data followed in a second write: two cards again.
    """
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {"username": "user@example.com", "oauth_token": "oauth2_4/X"},
    )
    _real_receiver_with_fake_push_client(monkeypatch)

    await cli_main._run_cli_bootstrap(cache, "")

    with_key = [w for w in writes if "shared_key" in w]
    assert with_key, "the keys never reached the file"
    assert "fcm_credentials" in with_key[0], (
        "the first write with the keys had no push data; Home Assistant offers "
        "a card for it and another one for the push data"
    )
    assert with_key[0]["fcm_credentials"]["fcm"]["registration"]["token"] == (
        "push-token"
    )
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["fcm_credentials"] == with_key[0]["fcm_credentials"]


@pytest.mark.asyncio
async def test_a_registration_that_never_finishes_still_saves_the_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wait for the push data is bounded; the keys are not held back for ever."""
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {"username": "user@example.com", "oauth_token": "oauth2_4/X"},
    )
    _install_steps(monkeypatch)

    async def _unregistered(_cache: Any) -> _Fcm:
        return _Fcm()  # the push endpoint never answers

    monkeypatch.setattr(cli_main, "_setup_fcm_receiver", _unregistered)
    monkeypatch.setattr(cli_main, "_PUSH_REGISTRATION_WAIT_S", 0.3, raising=False)
    monkeypatch.setattr(cli_main, "_PUSH_REGISTRATION_POLL_S", 0.01, raising=False)

    # Bounded here too: a wait without its limit must fail this test, not hang it.
    await asyncio.wait_for(cli_main._run_cli_bootstrap(cache, ""), timeout=10)

    with_key = [w for w in writes if "shared_key" in w]
    assert len(with_key) == 1
    assert "fcm_credentials" not in with_key[0]
    assert json.loads(path.read_text(encoding="utf-8"))["shared_key"] == "aa" * 32
    out = capsys.readouterr().out
    assert (
        "The push registration did not finish within 0 s; saving the keys now." in out
    )


# --- docker stop (SIGTERM) inside the write block ---------------------------
#
# ``docker stop`` sends SIGTERM; ``entrypoint.sh`` forwards it to the login.
# Without a handler the process died at once and the block never wrote: the
# vault keys gathered in sign-in 2 of 2 were lost. While the block runs, the
# event loop's handler now cancels the bootstrap, the block writes what it
# holds, and the run ends with status 143 (128 + SIGTERM).
#
# The tests send a real SIGTERM to this process. A counting stand-in handler is
# installed first, so a build without the loop handler (the red probe) records
# the signal instead of killing pytest.


@pytest.fixture
def _sigterm_stand_in(monkeypatch: pytest.MonkeyPatch) -> Any:
    import os
    import signal

    def _exit_in_test(code: int) -> None:
        raise SystemExit(code)  # os._exit would end pytest itself

    monkeypatch.setattr(os, "_exit", _exit_in_test)
    received: list[int] = []
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))
    try:
        yield received
    finally:
        signal.signal(signal.SIGTERM, previous)


def _send_sigterm() -> None:
    import os
    import signal

    os.kill(os.getpid(), signal.SIGTERM)


async def _bootstrap_outcome(cache: Any) -> BaseException | None:
    """Run the bootstrap; return what it raised (``None`` for a normal end)."""
    from custom_components.googlefindmy import main as cli_main

    try:
        await asyncio.wait_for(cli_main._run_cli_bootstrap(cache, ""), timeout=10)
    except BaseException as err:  # noqa: BLE001 - the outcome is the result
        return err
    return None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_sigterm_stand_in")
async def test_a_stop_during_the_vault_read_saves_the_key_read_so_far(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGTERM after the shared key: it is on disk, and the run ends with 143."""
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    _install_steps(monkeypatch)

    async def _vault_stopped(cache_: Any) -> None:
        await cache_.set("shared_key", "aa" * 32)
        _send_sigterm()
        await asyncio.sleep(2)  # the owner-key versions are still being read
        await cache_.set("owner_key_versions", [1])

    monkeypatch.setattr(cli_main, "_ensure_vault_keys", _vault_stopped)

    outcome = await _bootstrap_outcome(cache)

    assert isinstance(outcome, SystemExit), f"run ended with {outcome!r}"
    assert outcome.code == 143
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["shared_key"] == "aa" * 32
    assert "owner_key_versions" not in final
    assert "fcm_credentials" not in final
    assert len([w for w in writes if "shared_key" in w]) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_sigterm_stand_in")
async def test_a_stop_while_waiting_for_push_data_saves_the_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SIGTERM in the push wait: keys and versions are saved, without push data.

    This is the likely moment: the browser already showed success. Home
    Assistant's own receiver registers when the bundle has no push data.
    """
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    _install_steps(monkeypatch)

    async def _unregistered(_cache: Any) -> _Fcm:
        asyncio.get_running_loop().call_later(0.05, _send_sigterm)
        return _Fcm()

    monkeypatch.setattr(cli_main, "_setup_fcm_receiver", _unregistered)
    monkeypatch.setattr(cli_main, "_PUSH_REGISTRATION_WAIT_S", 1.0, raising=False)
    monkeypatch.setattr(cli_main, "_PUSH_REGISTRATION_POLL_S", 0.01, raising=False)

    outcome = await _bootstrap_outcome(cache)

    assert isinstance(outcome, SystemExit), f"run ended with {outcome!r}"
    assert outcome.code == 143
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["shared_key"] == "aa" * 32
    assert final["owner_key_versions"] == [1]
    assert "fcm_credentials" not in final
    assert len([w for w in writes if "shared_key" in w]) == 1
    captured = capsys.readouterr()
    assert (
        "The login was stopped (SIGTERM) during sign-in 2 of 2; whatever it had "
        "read by then was saved." in captured.err
    )
    assert "The push registration did not finish" not in captured.out


@pytest.mark.asyncio
async def test_the_stop_handler_ends_with_the_write_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After the block SIGTERM is no longer the login's: the menu keeps the default."""
    import signal

    from custom_components.googlefindmy import main as cli_main
    from custom_components.googlefindmy.NovaApi.ListDevices import (
        nbe_list_devices as nbe,
    )

    cache, _path, _writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    _install_steps(monkeypatch)
    seen: dict[str, Any] = {}

    async def _vault_sees(cache_: Any) -> None:
        seen["in_block"] = signal.getsignal(signal.SIGTERM)
        await cache_.set("shared_key", "aa" * 32)

    async def _menu_sees(**_kwargs: object) -> None:
        seen["after_block"] = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(cli_main, "_ensure_vault_keys", _vault_sees)
    monkeypatch.setattr(nbe, "_async_cli_main", _menu_sees)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        await cli_main._run_cli_bootstrap(cache, "")
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert seen["in_block"] not in (signal.SIG_DFL, signal.SIG_IGN, None)
    assert seen["after_block"] == signal.SIG_DFL


@pytest.mark.asyncio
@pytest.mark.usefixtures("_sigterm_stand_in")
async def test_a_cancellation_that_is_not_a_stop_stays_a_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the SIGTERM handler turns a cancellation into status 143."""
    from custom_components.googlefindmy import main as cli_main

    cache, path, _writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    _install_steps(monkeypatch)

    async def _vault_cancelled(cache_: Any) -> None:
        await cache_.set("shared_key", "aa" * 32)
        raise asyncio.CancelledError

    monkeypatch.setattr(cli_main, "_ensure_vault_keys", _vault_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await cli_main._run_cli_bootstrap(cache, "")
    assert json.loads(path.read_text(encoding="utf-8"))["shared_key"] == "aa" * 32


@pytest.mark.asyncio
async def test_a_loop_without_signal_handlers_runs_the_login_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows' loop has no ``add_signal_handler``; the login runs as before."""
    from custom_components.googlefindmy import main as cli_main

    cache, _path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    _install_steps(monkeypatch)
    loop = asyncio.get_running_loop()

    def _unsupported(*_args: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _unsupported)

    await cli_main._run_cli_bootstrap(cache, "")

    assert len([w for w in writes if "shared_key" in w]) == 1


_STOP_IN_BROWSER_WORKER = (
    Path(__file__).resolve().parent / "fixtures" / "stop_probe" / "browser_worker.py"
)


def test_a_stop_does_not_wait_for_the_browser_worker(tmp_path: Path) -> None:
    """The run ends at once with 143, although the browser thread is still busy.

    The browser flow of sign-in 2 runs in an executor thread that a cancelled
    task cannot stop. Ending through ``SystemExit`` made ``asyncio.run`` wait
    for that thread (up to the 300 s sign-in deadline); ``docker stop`` then
    killed the container before ``entrypoint.sh`` handed the file over. Only a
    real process shows the wait, so this test starts one. The time is taken
    from the signal on (the clock is system-wide on Linux), not from the start,
    which includes importing Home Assistant.
    """
    import os
    import subprocess
    import sys
    import time

    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"username": "user@example.com"}), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "GOOGLEFINDMY_SECRETS_PATH": str(path),
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(root), os.environ.get("PYTHONPATH", "")) if p
        ),
    }

    done: subprocess.CompletedProcess[str] | None
    try:
        done = subprocess.run(
            [sys.executable, str(_STOP_IN_BROWSER_WORKER)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        done = None
    ended = time.monotonic()

    assert done is not None, "the stopped login waited for the browser thread"
    assert done.returncode == 143, done.stderr[-2000:]
    sent = [ln for ln in done.stderr.splitlines() if ln.startswith("SIGTERM_AT=")]
    assert len(sent) == 1
    elapsed = ended - float(sent[0].split("=", 1)[1])
    # The worker sleeps 30 s; waiting for it would take that long.
    assert elapsed < 10, f"the stopped login ended {elapsed:.1f} s after SIGTERM"
    assert json.loads(path.read_text(encoding="utf-8"))["shared_key"] == "aa" * 32
    assert "The login was stopped (SIGTERM) during sign-in 2 of 2" in done.stderr
    assert "vault read started" in done.stdout  # earlier output is not lost


def test_a_closed_stdout_does_not_keep_a_stopped_login_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing flush (the terminal is gone) still ends the process."""
    import os
    import sys

    from custom_components.googlefindmy import main as cli_main

    class _ClosedPipe:
        def flush(self) -> None:
            raise BrokenPipeError

    ended: list[int] = []
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    monkeypatch.setattr(os, "_exit", ended.append)

    cli_main._end_process_now(143)

    assert ended == [143]


# --- push data from an earlier run (review of PR #1482, P2 #1 and #2) ---


@pytest.mark.asyncio
async def test_stored_push_data_leave_no_write_after_the_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With push data from an earlier run the keys still land in one write.

    The wait used to end at once because ``fcm_credentials`` were already
    there; the receiver then added ``fcm_routing_tokens`` and stored the same
    registration twice more after the block, three more versions of the file
    and three more cards (``--reauth`` always takes this path).
    """
    from custom_components.googlefindmy import main as cli_main
    from custom_components.googlefindmy.NovaApi.ListDevices import (
        nbe_list_devices as nbe,
    )

    cache, _path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {
            "username": "user@example.com",
            "oauth_token": "oauth2_4/X",
            "fcm_credentials": _STORED_PUSH_DATA,
        },
    )
    _real_receiver_with_fake_push_client(monkeypatch)

    async def _menu(**_kwargs: object) -> None:
        # The real menu runs for a while; the receiver's tasks run meanwhile.
        await asyncio.sleep(0.5)

    monkeypatch.setattr(nbe, "_async_cli_main", _menu)

    await asyncio.wait_for(cli_main._run_cli_bootstrap(cache, ""), timeout=10)

    first = next(i for i, w in enumerate(writes) if "shared_key" in w)
    assert writes[first].get("fcm_routing_tokens") == ["push-token"]
    assert len(writes) == first + 1, [sorted(w) for w in writes[first:]]


class _DictCache:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    async def get(self, name: str) -> object:
        return self.data.get(name)


@pytest.mark.asyncio
async def test_push_data_without_a_token_do_not_end_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registration without its token (mid re-registration) is not finished.

    The routing grace is cut short, so a wait that took the incomplete
    registration for a finished one would end well before the token arrives.
    """
    from custom_components.googlefindmy import main as cli_main

    monkeypatch.setattr(cli_main, "_ROUTING_ENTRY_GRACE_S", 0.05, raising=False)
    cache = _DictCache({"fcm_credentials": {"gcm": {"android_id": "1"}}})

    async def _register() -> None:
        await asyncio.sleep(0.3)
        cache.data["fcm_credentials"] = _STORED_PUSH_DATA
        cache.data["fcm_routing_tokens"] = ["push-token"]

    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.create_task(_register())
    await asyncio.wait_for(cli_main._await_push_credentials(cache), timeout=10)
    waited = loop.time() - started
    await task

    assert waited >= 0.3, f"the wait ended after {waited:.2f} s, before the token"


@pytest.mark.asyncio
async def test_a_routing_entry_that_never_comes_ends_the_wait_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The routing entry is best-effort; its absence costs a short grace only."""
    from custom_components.googlefindmy import main as cli_main

    monkeypatch.setattr(cli_main, "_ROUTING_ENTRY_GRACE_S", 0.2, raising=False)
    cache = _DictCache({"fcm_credentials": _STORED_PUSH_DATA})

    await asyncio.wait_for(cli_main._await_push_credentials(cache), timeout=5)

    assert "did not finish" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_an_unchanged_value_outside_the_block_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Storing what the file already holds leaves its modification time alone."""
    cache, _path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com"}
    )

    await cache.set("fcm_credentials", dict(_STORED_PUSH_DATA))
    await cache.set("fcm_credentials", dict(_STORED_PUSH_DATA))
    await cache.set("username", "user@example.com")
    await cache.set("fcm_routing_tokens", ["push-token"])

    assert [sorted(w) for w in writes] == [
        ["fcm_credentials", "username"],
        ["fcm_credentials", "fcm_routing_tokens", "username"],
    ]


@pytest.mark.asyncio
async def test_a_value_changed_in_place_is_still_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``get`` hands out the stored object; changing it and storing it writes.

    The receiver invalidates its registration that way (pop ``fcm`` and the
    GCM token, then ``set`` the same dict). Compared with the object in memory
    the value looked unchanged, and the stale token stayed on disk.
    """
    cache, path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {"fcm_credentials": json.loads(json.dumps(_STORED_PUSH_DATA))},
    )

    creds = await cache.get("fcm_credentials")
    creds.pop("fcm")
    await cache.set("fcm_credentials", creds)

    assert len(writes) == 1
    assert "fcm" not in json.loads(path.read_text(encoding="utf-8"))["fcm_credentials"]


@pytest.mark.asyncio
async def test_push_data_stored_as_json_text_end_the_wait() -> None:
    """The receiver accepts its registration as JSON text; so does the wait."""
    from custom_components.googlefindmy import main as cli_main

    cache = _DictCache(
        {
            "fcm_credentials": json.dumps(_STORED_PUSH_DATA),
            "fcm_routing_tokens": ["push-token"],
        }
    )
    try:
        await asyncio.wait_for(cli_main._await_push_credentials(cache), timeout=2)
    except TimeoutError:
        finished = False
    else:
        finished = True

    assert finished, "a registration stored as JSON text was not recognised"


@pytest.mark.asyncio
async def test_storing_what_was_loaded_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Right after loading, the loaded content counts as the file's content."""
    cache, _path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"fcm_routing_tokens": ["push-token"]}
    )

    await cache.set("fcm_routing_tokens", ["push-token"])

    assert writes == []


@pytest.mark.parametrize(
    "stored",
    [None, "not json", "[]", {"gcm": {"token": "t"}}, {"fcm": {"registration": {}}}],
)
def test_a_registration_without_its_push_token_has_none(stored: object) -> None:
    """Text that is not JSON and registrations without the token count as none."""
    from custom_components.googlefindmy import main as cli_main

    assert cli_main._push_token(stored) is None


@pytest.mark.asyncio
async def test_a_block_writes_a_value_changed_in_place_before_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The block compares with the file, not with the memory on entry."""
    cache, path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {"fcm_credentials": json.loads(json.dumps(_STORED_PUSH_DATA))},
    )

    creds = await cache.get("fcm_credentials")
    creds.pop("fcm")
    with cache.deferred_writes():
        await cache.set("fcm_credentials", creds)

    assert len(writes) == 1
    assert "fcm" not in json.loads(path.read_text(encoding="utf-8"))["fcm_credentials"]
