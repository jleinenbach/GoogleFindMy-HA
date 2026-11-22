# tests/test_main_owner_key_versions.py
"""Owner-key version field in the login tool: vault sink, self-test, single writer.

Drives the real chain from ``main._ensure_vault_keys`` through
``KeyBackup.shared_key_retrieval`` with a stand-in ``shared_key_flow`` module that
counts browser starts per function, a cache that counts writes per name and a
Spot double for the owner key of the run. All key material comes from
``tests.helpers.owner_key_vault_fixtures``; expected values are the plaintexts
fed in there, never an output of the code under test.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from custom_components.googlefindmy import main as cli_main
from custom_components.googlefindmy.browser_deps import (
    INSTALL_COMMAND,
    BrowserPackagesUnusable,
)
from custom_components.googlefindmy.KeyBackup import shared_key_retrieval as skr
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    OWNER_KEY_VERSIONS_FIELD,
    VaultCause,
    VaultEntry,
    VaultFetchResult,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_owner_key as gok,
)
from tests.helpers.owner_key_vault_fixtures import (
    OWNER_KEY_VERSIONS_FIELD as FIXTURE_FIELD,
)
from tests.helpers.owner_key_vault_fixtures import (
    VAULT_DOMAIN,
    VaultWorld,
    make_vault_world,
)

_FLOW_MODULE = "custom_components.googlefindmy.KeyBackup.shared_key_flow"
_USERNAME = "user@example.com"
_HYGIENE_WINDOW = 16  # 16 hex or Base64 characters (8 bytes)


def test_field_name_matches_fixture() -> None:
    """The fixture spells the field name once; it must equal the production constant."""
    assert FIXTURE_FIELD == OWNER_KEY_VERSIONS_FIELD


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _CountingCache:
    """Entry cache double that counts ``set`` calls per name."""

    def __init__(self, data: dict[str, object]) -> None:
        self.data: dict[str, object] = dict(data)
        self.sets: list[tuple[str, object]] = []

    async def get(self, name: str) -> object:
        return self.data.get(name)

    async def set(self, name: str, value: object) -> None:
        self.sets.append((name, value))
        if value is None:
            self.data.pop(name, None)
        else:
            self.data[name] = value

    async def get_or_set(self, name: str, generator: Callable[[], Any]) -> object:
        existing = self.data.get(name)
        if existing is not None:
            return existing
        value = generator()
        if hasattr(value, "__await__"):
            value = await value
        await self.set(name, value)
        return value

    def field_sets(self) -> int:
        return sum(1 for name, _ in self.sets if name == OWNER_KEY_VERSIONS_FIELD)


class _Flow:
    """Stand-in ``shared_key_flow`` module; counts browser starts per function."""

    def __init__(
        self,
        *,
        shared_hex: str,
        vault: VaultFetchResult | None,
        vault_only_raises: BaseException | None = None,
    ) -> None:
        self.shared_hex = shared_hex
        self.vault = vault
        self.vault_only_raises = vault_only_raises
        self.shared_calls = 0
        self.vault_only_calls = 0

    def request_shared_key_flow(self, **kwargs: Any) -> str:
        self.shared_calls += 1
        sink = kwargs.get("vault_sink")
        if sink is not None and self.vault is not None:
            sink(self.vault)
        return self.shared_hex

    def request_vault_entries_flow(self) -> VaultFetchResult:
        self.vault_only_calls += 1
        if self.vault_only_raises is not None:
            raise self.vault_only_raises
        assert self.vault is not None
        return self.vault


class _Stdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _entries(
    world: VaultWorld, versions: tuple[int, ...] | None = None
) -> list[VaultEntry]:
    chosen = sorted(world.owner_keys) if versions is None else list(versions)
    return [
        VaultEntry(
            version=v,
            epoch=int(world.bundle_field[str(v)]["epoch"]),
            domain=VAULT_DOMAIN,
            key=world.wrapped_owner_keys[v],
        )
        for v in chosen
    ]


def _ok(entries: list[VaultEntry]) -> VaultFetchResult:
    return VaultFetchResult(tuple(entries), None)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    flow: _Flow,
    *,
    attended: bool = True,
    spot: Callable[..., Any] | None = None,
) -> list[str]:
    """Wire the flow double, the session state and the Spot double; return the call log."""
    module = ModuleType(_FLOW_MODULE)
    module.request_shared_key_flow = flow.request_shared_key_flow  # type: ignore[attr-defined]
    module.request_vault_entries_flow = flow.request_vault_entries_flow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _FLOW_MODULE, module)

    async def passthrough(func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(skr, "_run_in_executor", passthrough)
    monkeypatch.setattr(skr, "_browser_flow_attempted", False)
    monkeypatch.setenv("GOOGLEFINDMY_CLI_PROCESS", "1")
    monkeypatch.delenv("GOOGLEFINDMY_ASSUME_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _Stdin(attended))

    log: list[str] = []
    if spot is not None:

        async def _spot(*, cache: object, username: str | None) -> gok.OwnerKeyInfo:
            log.append("spot")
            return spot(cache=cache, username=username)

        monkeypatch.setattr(gok, "async_fetch_owner_key_uncached", _spot)
    return log


def _spot_serving(world: VaultWorld, version: int | None = None) -> Callable[..., Any]:
    v = world.spot_version if version is None else version

    def _serve(**_kwargs: Any) -> gok.OwnerKeyInfo:
        return gok.OwnerKeyInfo(key=world.owner_keys[v], version=v)

    return _serve


def _spot_raising(exc: BaseException) -> Callable[..., Any]:
    def _serve(**_kwargs: Any) -> gok.OwnerKeyInfo:
        raise exc

    return _serve


def _base(world: VaultWorld | None = None, **extra: object) -> dict[str, object]:
    """Cache content of a CLI run; ``owner_key`` present so the eager fetch is skipped."""
    data: dict[str, object] = {"username": _USERNAME, "owner_key": "00" * 32}
    if world is not None:
        data["shared_key"] = world.shared_key.hex()
    data.update(extra)
    return data


async def _run(cache: _CountingCache) -> int | None:
    """Run ``_ensure_vault_keys``; return the exit code, ``None`` when it returned."""
    try:
        await cli_main._ensure_vault_keys(cache)
    except SystemExit as exc:
        return int(exc.code or 0)
    return None


def _console(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# ---------------------------------------------------------------------------
# Self-test before the single write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_test_passes_writes_field_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) New shared key, matching entry M: the field is written exactly once."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex=world.shared_key.hex(), vault=_ok(_entries(world)))
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base())

    assert await _run(cache) is None

    assert cache.data[OWNER_KEY_VERSIONS_FIELD] == world.bundle_field
    assert cache.field_sets() == 1
    assert flow.shared_calls == 1
    assert flow.vault_only_calls == 0
    assert "cause=" not in _console(capsys)


@pytest.mark.asyncio
async def test_self_test_no_write_before_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until the Spot owner key of the run is in hand, the field is never written."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex=world.shared_key.hex(), vault=_ok(_entries(world)))
    cache = _CountingCache(_base())
    writes_at_spot: list[int] = []
    serve = _spot_serving(world)

    def _spy(**kwargs: Any) -> gok.OwnerKeyInfo:
        writes_at_spot.append(cache.field_sets())
        return serve(**kwargs)

    _install(monkeypatch, flow, spot=_spy)
    await _run(cache)

    assert writes_at_spot == [0]
    assert cache.field_sets() == 1


async def _reference_run(
    monkeypatch: pytest.MonkeyPatch, world: VaultWorld, data: dict[str, object]
) -> tuple[int | None, dict[str, object]]:
    """The same run without any vault fetch (the sink is never fed)."""
    flow = _Flow(shared_hex=world.shared_key.hex(), vault=None)
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(data)
    code = await _run(cache)
    return code, cache.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["mismatch", "current_version_missing", "spot_unavailable", "decrypt_fails"],
)
async def test_self_test_failure_matches_reference_run(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(b), (c), (d) and a broken blob: field absent, exit code and fields as without a fetch."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    ref_code, ref_data = await _reference_run(monkeypatch, world, _base())
    capsys.readouterr()

    entries = _entries(world)
    spot = _spot_serving(world)
    if case == "mismatch":
        other = make_vault_world(versions=(2,), spot_version=2)
        spot = _spot_serving(other)
    elif case == "current_version_missing":
        entries = _entries(world, versions=(1,))
    elif case == "spot_unavailable":
        spot = _spot_raising(RuntimeError("Spot returned no owner key"))
    elif case == "decrypt_fails":
        blob = bytearray(entries[1].key)
        blob[-1] ^= 0x01
        entries[1] = VaultEntry(
            version=entries[1].version,
            epoch=entries[1].epoch,
            domain=entries[1].domain,
            key=bytes(blob),
        )

    flow = _Flow(shared_hex=world.shared_key.hex(), vault=_ok(entries))
    _install(monkeypatch, flow, spot=spot)
    cache = _CountingCache(_base())
    code = await _run(cache)

    assert code == ref_code
    assert cache.data == ref_data
    assert OWNER_KEY_VERSIONS_FIELD not in cache.data
    assert cache.field_sets() == 0
    console = _console(capsys)
    assert console.count("cause=") == 1
    assert console.count(f"cause={VaultCause.SELF_TEST_FAILED.value}") == 1


@pytest.mark.asyncio
async def test_self_test_shared_key_missing_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No shared key of the run: self-test fails without asking Spot or a browser."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex=world.shared_key.hex(), vault=_ok(_entries(world)))
    log = _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base())
    sink = cli_main._VaultSink()
    sink(_ok(_entries(world)))

    await cli_main._ensure_owner_key_versions(cache, sink)

    assert log == []
    assert flow.shared_calls == 0
    assert cache.sets == []
    assert _console(capsys).count(f"cause={VaultCause.SELF_TEST_FAILED.value}") == 1


@pytest.mark.asyncio
async def test_self_test_reads_legacy_user_scoped_shared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shared_key_<username>`` is the run's shared key when ``shared_key`` is absent."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex="unused", vault=_ok(_entries(world)))
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base(**{f"shared_key_{_USERNAME}": world.shared_key.hex()}))

    assert await _run(cache) is None

    assert flow.vault_only_calls == 1
    assert flow.shared_calls == 0
    assert cache.data[OWNER_KEY_VERSIONS_FIELD] == world.bundle_field


# ---------------------------------------------------------------------------
# Failed fetch, domain filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_raises_in_vault_only_flow_matches_reference_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fetch that raises: exit code and fields as without a fetch, cause once."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    ref_flow = _Flow(shared_hex="unused", vault=None)
    _install(monkeypatch, ref_flow, attended=False, spot=_spot_serving(world))
    ref_cache = _CountingCache(_base(world))
    ref_code = await _run(ref_cache)
    capsys.readouterr()

    flow = _Flow(
        shared_hex="unused",
        vault=None,
        vault_only_raises=RuntimeError("driver exploded"),
    )
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base(world))
    code = await _run(cache)

    assert flow.vault_only_calls == 1
    assert code == ref_code
    assert cache.data == ref_cache.data
    assert cache.sets == []
    console = _console(capsys)
    assert console.count("cause=") == 1
    assert console.count(f"cause={VaultCause.HTTP_STATUS.value}") == 1
    assert "driver exploded" not in console


@pytest.mark.asyncio
async def test_fetch_raises_cause_in_shared_key_flow_matches_reference_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fetch result with a cause in the full flow: shared key kept, cause once."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    ref_code, ref_data = await _reference_run(monkeypatch, world, _base())
    capsys.readouterr()

    flow = _Flow(
        shared_hex=world.shared_key.hex(),
        vault=VaultFetchResult((), VaultCause.WRONG_LENGTH),
    )
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base())
    code = await _run(cache)

    assert code == ref_code
    assert cache.data == ref_data
    console = _console(capsys)
    assert console.count("cause=") == 1
    assert console.count(f"cause={VaultCause.WRONG_LENGTH.value}") == 1


@pytest.mark.asyncio
async def test_mixed_domains_keep_finder_hw_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Foreign domains and older epochs never reach the field."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    good = _entries(world)
    noise = [
        # Same version as M, foreign domain, listed first: the self-test must
        # not pick it as the entry of the current version.
        VaultEntry(
            version=2, epoch=good[1].epoch + 5, domain="other_domain", key=b"\x03" * 60
        ),
        VaultEntry(version=7, epoch=1, domain="other_domain", key=b"\x01" * 60),
        VaultEntry(
            version=1, epoch=good[0].epoch - 1, domain=VAULT_DOMAIN, key=b"\x02" * 60
        ),
    ]
    flow = _Flow(shared_hex=world.shared_key.hex(), vault=_ok(noise + good))
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base())

    await _run(cache)

    assert cache.data[OWNER_KEY_VERSIONS_FIELD] == world.bundle_field


# ---------------------------------------------------------------------------
# Field replaced, removed with a new shared key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_runs_field_equals_second_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second successful fetch replaces the whole field, never merges it."""
    first = make_vault_world(versions=(1, 2), spot_version=2)
    _install(
        monkeypatch,
        _Flow(shared_hex=first.shared_key.hex(), vault=_ok(_entries(first))),
        spot=_spot_serving(first),
    )
    cache = _CountingCache(_base())
    await _run(cache)
    assert cache.data[OWNER_KEY_VERSIONS_FIELD] == first.bundle_field

    second = make_vault_world(versions=(3,), spot_version=3)
    await cache.set("shared_key", None)
    _install(
        monkeypatch,
        _Flow(shared_hex=second.shared_key.hex(), vault=_ok(_entries(second))),
        spot=_spot_serving(second),
    )
    await _run(cache)

    assert cache.data[OWNER_KEY_VERSIONS_FIELD] == second.bundle_field


@pytest.mark.asyncio
@pytest.mark.parametrize("vault", ["never_fed", "cause"])
async def test_new_shared_key_without_fetch_removes_old_field(
    vault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New shared key, no usable fetch: an existing field is removed."""
    old = make_vault_world(versions=(1,), spot_version=1)
    new = make_vault_world(versions=(1, 2), spot_version=2)
    result = (
        None if vault == "never_fed" else VaultFetchResult((), VaultCause.HTTP_STATUS)
    )
    flow = _Flow(shared_hex=new.shared_key.hex(), vault=result)
    _install(monkeypatch, flow, spot=_spot_serving(new))
    cache = _CountingCache(_base(**{OWNER_KEY_VERSIONS_FIELD: old.bundle_field}))

    assert await _run(cache) is None

    assert OWNER_KEY_VERSIONS_FIELD not in cache.data
    assert cache.data["shared_key"] == new.shared_key.hex()
    assert cache.field_sets() == 1


@pytest.mark.asyncio
async def test_new_shared_key_without_fetch_and_without_field_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to remove: no write under the field name at all."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex=world.shared_key.hex(), vault=None)
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base())

    await _run(cache)

    assert cache.field_sets() == 0


# ---------------------------------------------------------------------------
# Cached shared key without the field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_shared_key_without_field_fetches_vault_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sign-in only: vault flow once, unlock flow never, shared key byte-equal."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex="unused", vault=_ok(_entries(world)))
    _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(_base(world))

    assert await _run(cache) is None

    assert flow.vault_only_calls == 1
    assert flow.shared_calls == 0
    assert cache.data["shared_key"] == world.shared_key.hex()
    assert all(name != "shared_key" for name, _ in cache.sets)
    assert cache.data[OWNER_KEY_VERSIONS_FIELD] == world.bundle_field
    assert cache.field_sets() == 1
    assert skr._browser_flow_attempted is False
    assert "Google sign-in only, no PIN" in _console(capsys)


@pytest.mark.asyncio
async def test_cached_shared_key_with_field_opens_no_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The field is there: no browser, no write, no output."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex="unused", vault=_ok(_entries(world)))
    log = _install(monkeypatch, flow, spot=_spot_serving(world))
    cache = _CountingCache(
        _base(world, **{OWNER_KEY_VERSIONS_FIELD: world.bundle_field})
    )

    assert await _run(cache) is None

    assert flow.vault_only_calls == 0
    assert flow.shared_calls == 0
    assert log == []
    assert cache.sets == []
    assert _console(capsys) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_marker", [True, False])
async def test_cached_shared_key_no_tty_is_silent(
    cli_marker: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No attended command-line session: no browser, no cause, no output, exit as today."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(shared_hex="unused", vault=_ok(_entries(world)))
    log = _install(monkeypatch, flow, attended=False, spot=_spot_serving(world))
    if not cli_marker:
        monkeypatch.delenv("GOOGLEFINDMY_CLI_PROCESS", raising=False)
    cache = _CountingCache(_base(world))

    assert await _run(cache) is None

    assert flow.vault_only_calls == 0
    assert flow.shared_calls == 0
    assert log == []
    assert cache.sets == []
    assert _console(capsys) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["driver", "import"])
async def test_vault_only_browser_packages_unusable_reports_once(
    where: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Absent or broken browser packages: one report with the hint and the cause."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    flow = _Flow(
        shared_hex="unused", vault=None, vault_only_raises=BrowserPackagesUnusable()
    )
    _install(monkeypatch, flow, spot=_spot_serving(world))
    if where == "import":
        # A `None` entry makes `import_module` raise `ImportError`.
        monkeypatch.setitem(sys.modules, _FLOW_MODULE, None)
    cache = _CountingCache(_base(world))

    assert await _run(cache) is None

    console = _console(capsys)
    assert console.count(INSTALL_COMMAND) == 1
    assert console.count("cause=") == 1
    assert console.count(f"cause={VaultCause.HTTP_STATUS.value}") == 1
    # Unimportable packages fail before the browser would open; a driver that
    # fails to start was announced first.
    assert ("Google sign-in only" in console) is (where == "driver")
    assert cache.sets == []


# ---------------------------------------------------------------------------
# Output hygiene over every outcome of the owner-key version step
# ---------------------------------------------------------------------------


def _assert_no_secret_window(text: str, world: VaultWorld) -> None:
    for form in world.all_secret_strings():
        for start in range(len(form) - _HYGIENE_WINDOW + 1):
            window = form[start : start + _HYGIENE_WINDOW]
            assert window not in text, "secret material in output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "ok",
        "fetch_raises",
        "current_version_missing",
        "spot_unavailable",
        "shared_key_missing",
        "decrypt_fails",
        "mismatch",
        "browser_packages_unusable",
    ],
)
async def test_ensure_owner_key_versions_output_hygiene(
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Console, log and the sink's repr carry no 16-character window of any secret."""
    caplog.set_level("DEBUG")
    world = make_vault_world(versions=(1, 2), spot_version=2)
    entries = _entries(world)
    spot = _spot_serving(world)
    raises: BaseException | None = None
    data = _base(world)
    if outcome == "fetch_raises":
        raises = RuntimeError(world.shared_key.hex())
    elif outcome == "current_version_missing":
        entries = entries[:1]
    elif outcome == "spot_unavailable":
        spot = _spot_raising(RuntimeError(world.owner_keys[2].hex()))
    elif outcome == "shared_key_missing":
        data = _base()
    elif outcome == "decrypt_fails":
        entries[1] = VaultEntry(
            version=2, epoch=entries[1].epoch, domain=VAULT_DOMAIN, key=b"\x00" * 60
        )
    elif outcome == "mismatch":
        spot = _spot_serving(make_vault_world(versions=(2,), spot_version=2))
    elif outcome == "browser_packages_unusable":
        raises = BrowserPackagesUnusable()

    flow = _Flow(
        shared_hex=world.shared_key.hex(), vault=_ok(entries), vault_only_raises=raises
    )
    _install(monkeypatch, flow, spot=spot)
    cache = _CountingCache(data)
    sinks: list[cli_main._VaultSink] = []
    real_sink = cli_main._VaultSink

    def _capture() -> cli_main._VaultSink:
        sink = real_sink()
        sinks.append(sink)
        return sink

    monkeypatch.setattr(cli_main, "_VaultSink", _capture)

    if outcome == "shared_key_missing":
        # Both chain paths end with a shared key in the cache, so this outcome
        # is reached through the step itself.
        sink = _capture()
        sink(_ok(entries))
        await cli_main._ensure_owner_key_versions(cache, sink)
        assert cache.sets == []
    else:
        await _run(cache)

    captured = capsys.readouterr()
    _assert_no_secret_window(captured.out + captured.err + caplog.text, world)
    assert len(sinks) == 1
    for rendering in (repr(sinks[0]), str(sinks[0])):
        _assert_no_secret_window(rendering, world)
        assert "b'" not in rendering
        assert 'b"' not in rendering


# ---------------------------------------------------------------------------
# The uncached Spot owner key of the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_owner_key_uncached_never_touches_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spot's owner key and version come back; no cache name is read or written."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    calls: list[tuple[str, object]] = []

    async def _retrieve(username: str, *, cache: object) -> dict[str, Any]:
        calls.append((username, cache))
        return {"key": world.owner_keys[2].hex(), "version": 2}

    monkeypatch.setattr(gok, "_retrieve_owner_key", _retrieve)

    class _NoCache:
        async def get(self, name: str) -> object:
            raise AssertionError(f"cache read: {name}")

        async def set(self, name: str, value: object) -> None:
            raise AssertionError(f"cache write: {name}")

    cache = _NoCache()
    info = await gok.async_fetch_owner_key_uncached(cache=cache, username=None)  # type: ignore[arg-type]

    assert info == gok.OwnerKeyInfo(key=world.owner_keys[2], version=2)
    assert calls == [("", cache)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"key": "00" * 31, "version": 2}, {"key": None, "version": 2}],
    ids=["short_key", "no_key"],
)
async def test_fetch_owner_key_uncached_rejects_unusable_payload(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _retrieve(_username: str, *, cache: object) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(gok, "_retrieve_owner_key", _retrieve)

    with pytest.raises(RuntimeError):
        await gok.async_fetch_owner_key_uncached(cache=object(), username="u")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Edge paths of the CLI helpers
# ---------------------------------------------------------------------------


def test_vault_sink_keeps_only_the_first_result() -> None:
    """A second browser result never replaces the first one."""
    sink = cli_main._VaultSink()
    first = VaultFetchResult((), VaultCause.NO_FRAME)
    second = VaultFetchResult((), VaultCause.EMPTY_PAYLOAD)

    sink(first)
    sink(second)

    assert sink.results == [first]
    assert sink.browser_ran is True


async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("must not run")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["entry", "user"])
async def test_ensure_shared_key_without_sink_keeps_cached_key(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """Without a sink a cached key ends the step: no fetch and no vault call."""
    monkeypatch.setattr(skr, "async_get_shared_key", _forbidden)
    monkeypatch.setattr(cli_main, "_fetch_vault_for_cached_shared_key", _forbidden)
    data: dict[str, object] = (
        {"shared_key": "ab" * 32}
        if scope == "entry"
        else {"username": _USERNAME, f"shared_key_{_USERNAME}": "ab" * 32}
    )
    cache = _CountingCache(data)

    await cli_main._ensure_shared_key(cache)

    assert cache.sets == []


@pytest.mark.asyncio
async def test_ensure_shared_key_without_sink_fetches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a cached key the fetch runs once and gets no sink."""
    calls: list[dict[str, Any]] = []

    async def _fetch(**kwargs: Any) -> bytes:
        calls.append(kwargs)
        return b"\x01" * 32

    monkeypatch.setattr(skr, "async_get_shared_key", _fetch)

    await cli_main._ensure_shared_key(_CountingCache({"username": _USERNAME}))

    assert len(calls) == 1
    assert calls[0]["vault_sink"] is None
    assert calls[0]["username"] == _USERNAME


_KEY = bytes(range(32))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"shared_key": base64.b64encode(_KEY).decode()}, _KEY),
        ({"shared_key": "not a key"}, None),
        ({"username": None}, None),
        ({"username": 7, "shared_key_7": "ab" * 32}, None),
    ],
    ids=["base64", "invalid", "no_username", "username_not_text"],
)
async def test_read_run_shared_key_edge_values(
    data: dict[str, object], expected: bytes | None
) -> None:
    assert await cli_main._read_run_shared_key(_CountingCache(data)) == expected


@pytest.mark.asyncio
async def test_owner_key_versions_write_error_reports_self_test_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raising write is reported as a failed self-test, by cause and count only."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    sink = cli_main._VaultSink()
    sink(_ok(_entries(world)))

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("SENTINEL-write")

    monkeypatch.setattr(cli_main, "_write_owner_key_versions", _boom)
    cache = _CountingCache(_base(world))

    await cli_main._ensure_owner_key_versions(cache, sink)

    err = capsys.readouterr().err
    assert f"cause={VaultCause.SELF_TEST_FAILED.value}" in err
    assert "entries=2" in err
    assert "SENTINEL-write" not in err
    assert cache.field_sets() == 0


class _GetRaisingCache(_CountingCache):
    async def get(self, name: str) -> object:
        raise RuntimeError("cache down")


@pytest.mark.asyncio
async def test_field_removal_error_after_new_shared_key_is_swallowed() -> None:
    """A cache that cannot be read never breaks the run after a new shared key."""
    sink = cli_main._VaultSink()
    sink.shared_key_refreshed = True
    cache = _GetRaisingCache({})

    await cli_main._ensure_owner_key_versions(cache, sink)

    assert cache.sets == []


# ---------------------------------------------------------------------------
# Remedy texts against the CLI (the advice must name a flag that exists)
# ---------------------------------------------------------------------------

_INTEGRATION_DIR = Path(cli_main.__file__).resolve().parent
_REMEDY_KEYS = ("owner_key_version_login_required", "owner_key_version_not_in_vault")


def _remedy_texts() -> list[tuple[str, str]]:
    sources = [_INTEGRATION_DIR / "strings.json"]
    sources += sorted((_INTEGRATION_DIR / "translations").glob("*.json"))
    texts: list[tuple[str, str]] = []
    for source in sources:
        issues = json.loads(source.read_text(encoding="utf-8"))["issues"]
        texts += [
            (f"{source.name}:{key}", issues[key]["description"]) for key in _REMEDY_KEYS
        ]
    return texts


def test_remedy_texts_name_only_flags_the_cli_accepts() -> None:
    """Every ``--flag`` in a remedy text is an option of ``main.py``.

    Renaming the flag in the parser turns this red instead of leaving the
    Repairs text silently wrong. The texts must name ``--reauth`` at all: it is
    the way past an old ``secrets.json`` that a plain run reuses.
    """
    options = {
        option
        for action in cli_main._build_cli_parser()._actions
        for option in action.option_strings
    }
    texts = _remedy_texts()
    assert len(texts) == 2 * 11
    for where, text in texts:
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))
        assert "--reauth" in flags, where
        assert flags <= options, (where, flags - options)


def test_docker_helper_hands_gfmy_args_to_main_py() -> None:
    """The Docker remedy ``GFMY_ARGS=--reauth`` only works if the helper passes it."""
    entrypoint = (_INTEGRATION_DIR / "docker-login" / "entrypoint.sh").read_text(
        encoding="utf-8"
    )
    assert re.search(r"python3 main\.py \$\{GFMY_ARGS:-\}", entrypoint)


def test_reauth_clears_the_version_field_and_the_shared_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--reauth`` drops the field and the shared key, so the next run reads the vault.

    This is the premise of the remedy texts: a kept ``secrets.json`` with the
    field skips the vault, ``--reauth`` does not.
    """
    secrets = tmp_path / "secrets.json"
    secrets.write_text(
        json.dumps(
            {
                "username": _USERNAME,
                "aas_token": "aas",
                "shared_key": "11" * 32,
                OWNER_KEY_VERSIONS_FIELD: {"1": {"version": 1}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(secrets))

    assert cli_main._clear_stale_tokens_for_reauth() is not None

    remaining = json.loads(secrets.read_text(encoding="utf-8"))
    assert OWNER_KEY_VERSIONS_FIELD not in remaining
    assert "shared_key" not in remaining
    assert remaining["username"] == _USERNAME
