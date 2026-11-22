# tests/test_main_owner_key_diagnosis.py
"""The CLI diagnosis ``--diagnose-owner-key-versions``.

The diagnosis lists the vault entries (domain, version, epoch), their count and
cause class, the owner-key version Spot serves by default and for explicit
versions. It must never print key material (``print`` escapes the
logging guards, hence ``capsys``), return 0 even for an empty result and 2 only
for a session that may not open a browser. The runtime never asks Spot for an
explicit version; an AST test pins that.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import main as cli_main
from custom_components.googlefindmy.browser_deps import BrowserPackagesUnusable
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import (
    SharedKeyUnavailableError,
)
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    VaultCause,
    VaultEntry,
    VaultFetchResult,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_eid_info_request as _eid_info_request,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    _build_request_bytes,
)
from tests.helpers.owner_key_vault_fixtures import make_vault_world

_PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
_HEX_WINDOW = 16


class _Spot:
    """Spot double: default request serves ``default``; explicit N serves ``answers``."""

    def __init__(self, default: int, answers: dict[int, int | Exception]) -> None:
        self.default = default
        self.answers = answers
        self.requested: list[int] = []

    async def __call__(self, *, cache: object, owner_key_version: int = -1) -> Any:
        self.requested.append(owner_key_version)
        served: int | Exception = (
            self.default
            if owner_key_version == -1
            else self.answers.get(owner_key_version, self.default)
        )
        if isinstance(served, Exception):
            raise served
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(ownerKeyVersion=served)
        )


def _fetch_returning(result: VaultFetchResult) -> Any:
    async def _fetch(*, on_start: Any = None) -> VaultFetchResult:
        if on_start is not None:
            on_start()
        return result

    return _fetch


def _world_result() -> tuple[Any, VaultFetchResult]:
    world = make_vault_world(versions=(1, 2, 3), spot_version=1)
    entries = tuple(
        VaultEntry(
            version=version,
            epoch=1_700_000_000 + version,
            domain="finder_hw",
            key=world.wrapped_owner_keys[version],
        )
        for version in (1, 2, 3)
    )
    return world, VaultFetchResult(entries=entries, cause=None)


def _key_windows(world: Any) -> set[str]:
    blobs: list[bytes] = [world.shared_key]
    for mapping in (world.owner_keys, world.wrapped_owner_keys):
        blobs.extend(mapping.values())
    return {
        blob.hex()[i : i + _HEX_WINDOW]
        for blob in blobs
        for i in range(len(blob.hex()) - _HEX_WINDOW + 1)
    }


@pytest.mark.asyncio
async def test_diagnosis_prints_entries_and_spot_versions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Entries, count, cause, M and every vault version above M; no material."""
    world, result = _world_result()
    spot = _Spot(default=1, answers={2: 2, 3: 1})

    rc = await cli_main._run_owner_key_diagnosis(
        object(), 0, fetch=_fetch_returning(result), eid_info=spot
    )

    out = capsys.readouterr()
    assert rc == 0
    lines = out.out.splitlines()
    for version in (1, 2, 3):
        assert (
            f"domain=finder_hw version={version} epoch={1_700_000_000 + version}"
            in lines
        )
    assert "entries=3" in lines
    assert "cause=none" in lines
    assert "spot_default_version=1" in lines
    assert "spot_explicit[2]=2" in lines
    assert "spot_explicit[3]=1" in lines
    assert spot.requested == [-1, 2, 3]

    windows = _key_windows(world)
    assert windows
    text = (out.out + out.err).lower()
    assert not any(window in text for window in windows)
    assert "b'" not in text


@pytest.mark.asyncio
async def test_diagnosis_empty_result_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rc 0 for an empty vault; a failing Spot call shows its class only."""
    spot = _Spot(default=1, answers={})
    spot.default = RuntimeError("secret body")  # type: ignore[assignment]

    rc = await cli_main._run_owner_key_diagnosis(
        object(),
        0,
        fetch=_fetch_returning(VaultFetchResult(entries=(), cause=VaultCause.NO_FRAME)),
        eid_info=spot,
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "entries=0" in out.splitlines()
    assert "cause=no_frame" in out.splitlines()
    assert "spot_default_version=RuntimeError" in out.splitlines()
    assert "secret body" not in out
    assert spot.requested == [-1]


@pytest.mark.asyncio
async def test_diagnosis_without_higher_vault_version_asks_m_plus_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No vault version above M -> ask Spot for M+1."""
    spot = _Spot(default=4, answers={5: 4})

    rc = await cli_main._run_owner_key_diagnosis(
        object(),
        0,
        fetch=_fetch_returning(VaultFetchResult(entries=(), cause=None)),
        eid_info=spot,
    )

    assert rc == 0
    assert "spot_explicit[5]=4" in capsys.readouterr().out.splitlines()
    assert spot.requested == [-1, 5]


@pytest.mark.asyncio
async def test_diagnosis_requested_version_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With N, Spot is asked for N only."""
    _world, result = _world_result()
    spot = _Spot(default=1, answers={7: 1})

    rc = await cli_main._run_owner_key_diagnosis(
        object(), 7, fetch=_fetch_returning(result), eid_info=spot
    )

    assert rc == 0
    assert "spot_explicit[7]=1" in capsys.readouterr().out.splitlines()
    assert spot.requested == [-1, 7]


@pytest.mark.asyncio
async def test_diagnosis_fetch_failure_reports_cause_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing vault fetch is a cause class, not a crash; rc stays 0."""

    async def _boom(*, on_start: Any = None) -> VaultFetchResult:
        raise RuntimeError("driver crashed with body")

    rc = await cli_main._run_owner_key_diagnosis(
        object(), 0, fetch=_boom, eid_info=_Spot(default=1, answers={})
    )

    out = capsys.readouterr()
    assert rc == 0
    assert "cause=http_status" in out.out.splitlines()
    assert "driver crashed" not in out.out + out.err


@pytest.mark.asyncio
async def test_diagnosis_without_attended_session_returns_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No attended session -> rc 2, no Spot call."""

    async def _refuse(*, on_start: Any = None) -> VaultFetchResult:
        raise SharedKeyUnavailableError("not attended")

    spot = _Spot(default=1, answers={})
    rc = await cli_main._run_owner_key_diagnosis(
        object(), 0, fetch=_refuse, eid_info=spot
    )

    assert rc == 2
    assert spot.requested == []
    assert "attended terminal" in capsys.readouterr().err


def test_parser_diagnose_flag() -> None:
    """The flag takes an optional version; absent means no diagnosis."""
    parser = cli_main._build_cli_parser()
    assert parser.parse_args([]).diagnose_owner_key_versions is None
    assert (
        parser.parse_args(["--diagnose-owner-key-versions"]).diagnose_owner_key_versions
        == 0
    )
    assert (
        parser.parse_args(
            ["--diagnose-owner-key-versions", "3"]
        ).diagnose_owner_key_versions
        == 3
    )
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["--diagnose-owner-key-versions", "x"])
    assert ei.value.code == 2


def test_main_routes_to_diagnosis_without_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_main`` exits with the diagnosis code and never runs the bootstrap."""
    calls: list[int | None] = []

    def _diagnosis(args: Any) -> int:
        calls.append(args.diagnose_owner_key_versions)
        return 0

    def _no_bootstrap(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("bootstrap must not run")

    monkeypatch.setattr(cli_main, "_owner_key_diagnosis_cli", _diagnosis)
    monkeypatch.setattr(cli_main, "_ensure_authenticated", _no_bootstrap)
    monkeypatch.setattr(cli_main, "_run_cli_bootstrap", _no_bootstrap)
    monkeypatch.setattr(cli_main, "_configure_cli_logging", lambda **_kwargs: None)
    # ``_main`` marks the process as the CLI in os.environ; registering the
    # variable with monkeypatch first lets teardown restore the prior state.
    monkeypatch.setenv("GOOGLEFINDMY_CLI_PROCESS", "")

    with pytest.raises(SystemExit) as ei:
        cli_main._main(["--diagnose-owner-key-versions", "2"])

    assert ei.value.code == 0
    assert calls == [2]


def test_build_request_bytes_carries_requested_version() -> None:
    """The runtime default stays -1; the diagnosis can ask for N."""
    default = Common_pb2.GetEidInfoForE2eeDevicesRequest()
    default.ParseFromString(_build_request_bytes())
    explicit = Common_pb2.GetEidInfoForE2eeDevicesRequest()
    explicit.ParseFromString(_build_request_bytes(5))

    assert default.ownerKeyVersion == -1
    assert explicit.ownerKeyVersion == 5
    assert explicit.hasOwnerKeyVersion is True


# ---------------------------------------------------------------------------
# No runtime caller asks Spot for an explicit owner-key version
# ---------------------------------------------------------------------------

_SPOT_REQUEST_FUNCS = frozenset({"async_get_eid_info", "_build_request_bytes"})
_VERSION_PARAM = "owner_key_version"


def _positional_version_index() -> dict[str, int | None]:
    """Position of the version parameter per helper, read from the real signatures.

    ``None`` means keyword-only: a positional argument cannot carry the version.
    """
    index: dict[str, int | None] = {}
    for name in _SPOT_REQUEST_FUNCS:
        helper = getattr(_eid_info_request, name)
        params = list(inspect.signature(helper).parameters.values())
        positional = [
            p.name
            for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        index[name] = (
            positional.index(_VERSION_PARAM) if _VERSION_PARAM in positional else None
        )
    return index


def _callee(func: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _carries_version(
    helper: str,
    args: list[ast.expr],
    keywords: list[ast.keyword],
    positions: dict[str, int | None],
    forwarding: bool,
) -> bool:
    """Return True when a call of ``helper`` with these arguments sets the version."""

    def own_parameter(value: ast.expr) -> bool:
        return forwarding and isinstance(value, ast.Name) and value.id == _VERSION_PARAM

    for keyword in keywords:
        if keyword.arg == _VERSION_PARAM and not own_parameter(keyword.value):
            return True
        if keyword.arg is None:  # ``**mapping``
            mapping = keyword.value
            if not isinstance(mapping, ast.Dict) or any(
                key is None
                or (isinstance(key, ast.Constant) and key.value == _VERSION_PARAM)
                for key in mapping.keys
            ):
                return True
    position = positions[helper]
    for index, arg in enumerate(args):
        if isinstance(arg, ast.Starred):
            return True
        if index == position and not own_parameter(arg):
            return True
    return False


class _ExplicitVersionFinder(ast.NodeVisitor):
    def __init__(self, positions: dict[str, int | None]) -> None:
        self.positions = positions
        self.aliases: dict[str, str] = {}
        self.functions: list[str] = []
        self.lines: list[int] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in _SPOT_REQUEST_FUNCS or (
                node.module == "functools" and alias.name == "partial"
            ):
                self.aliases[alias.asname or alias.name] = alias.name

    def _bind(self, target: ast.expr, value: ast.expr | None) -> None:
        name = _callee(value, self.aliases) if value is not None else None
        if isinstance(target, ast.Name) and name in _SPOT_REQUEST_FUNCS:
            self.aliases[target.id] = str(name)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:  # also ``a = b = helper``
            self._bind(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind(node.target, node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind(node.target, node.value)
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        # Inside a helper, passing its own ``owner_key_version`` on is forwarding.
        forwarding = bool(self.functions) and self.functions[-1] in _SPOT_REQUEST_FUNCS
        func = node.func.value if isinstance(node.func, ast.NamedExpr) else node.func
        name = _callee(func, self.aliases)
        if name in _SPOT_REQUEST_FUNCS:
            if _carries_version(
                name, node.args, node.keywords, self.positions, forwarding
            ):
                self.lines.append(node.lineno)
        elif name == "partial" and node.args:
            bound = _callee(node.args[0], self.aliases)
            if bound in _SPOT_REQUEST_FUNCS and _carries_version(
                bound, node.args[1:], node.keywords, self.positions, forwarding
            ):
                self.lines.append(node.lineno)
        self.generic_visit(node)


def _explicit_version_calls(source: str) -> list[int]:
    """Return the line of every call that hands Spot an explicit owner-key version.

    Recognised: a helper called by name, by ``from … import … as`` alias, by a
    module-level alias or as an attribute; the version as keyword, as positional
    argument where the signature allows it, inside ``**mapping`` (a literal dict
    without the key is clean, any other mapping counts) or through ``*args``;
    and the same arguments bound with ``functools.partial``. Passing a helper's
    own parameter on inside that helper is forwarding, not a request. Aliases
    count from plain, chained (``a = b = helper``), annotated and walrus
    bindings anywhere in the module, and ``partial`` imported under another name.
    Declared blind spots: ``getattr(module, "…")``, an alias stored on an
    attribute (``self.fetch = helper``), a helper passed as an argument, and a
    wrapper function in between. A request built directly is covered by
    ``test_only_the_request_builder_writes_owner_key_version``.
    """
    finder = _ExplicitVersionFinder(_positional_version_index())
    finder.visit(ast.parse(source))
    return sorted(finder.lines)


def test_no_runtime_owner_key_version() -> None:
    """Outside ``main.py`` no call passes ``owner_key_version`` to Spot."""
    offenders = {
        str(path.relative_to(_PACKAGE)): hits
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path.name != "main.py"
        and (hits := _explicit_version_calls(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_no_runtime_owner_key_version_detector_controls() -> None:
    """Positive control: the detector finds both helpers, plain and as attribute."""
    assert _explicit_version_calls(
        "await async_get_eid_info(cache=c, owner_key_version=3)\n"
    )
    assert _explicit_version_calls("api._build_request_bytes(owner_key_version=3)\n")
    assert _explicit_version_calls("await async_get_eid_info(cache=c)\n") == []
    assert _explicit_version_calls("other(owner_key_version=3)\n") == []


@pytest.mark.parametrize(
    "source",
    [
        "_build_request_bytes(3)\n",
        "api._build_request_bytes(n)\n",
        "_build_request_bytes(*args)\n",
        'await async_get_eid_info(cache=c, **{"owner_key_version": 3})\n',
        "await async_get_eid_info(cache=c, **options)\n",
        "from m import async_get_eid_info as fetch\nawait fetch(cache=c, owner_key_version=3)\n",
        "from m import _build_request_bytes as build\nbuild(4)\n",
        "fetch = async_get_eid_info\nawait fetch(cache=c, owner_key_version=3)\n",
        "functools.partial(async_get_eid_info, owner_key_version=3)\n",
        "partial(_build_request_bytes, 3)\n",
        "def other(owner_key_version):\n    _build_request_bytes(owner_key_version)\n",
        "fetch: object = async_get_eid_info\nawait fetch(cache=c, owner_key_version=3)\n",
        "a = fetch = async_get_eid_info\nawait fetch(cache=c, owner_key_version=3)\n",
        "await (f := async_get_eid_info)(cache=c, owner_key_version=3)\n",
        "from functools import partial as p\np(_build_request_bytes, 3)\n",
        "def run():\n    fetch = async_get_eid_info\n    fetch(cache=c, owner_key_version=3)\n",
    ],
)
def test_detector_finds_every_explicit_form(source: str) -> None:
    """Each form hands Spot a version; a bare-keyword rule would miss them."""
    assert _explicit_version_calls(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "_build_request_bytes()\n",
        "await async_get_eid_info(cache=c)\n",
        'await async_get_eid_info(**{"cache": c})\n',
        "functools.partial(async_get_eid_info, cache=c)\n",
        "def _build_request_bytes(owner_key_version=-1):\n    pass\n"
        "async def async_get_eid_info(*, cache, owner_key_version=-1):\n"
        "    _build_request_bytes(owner_key_version)\n",
    ],
)
def test_detector_negative_controls(source: str) -> None:
    """No version, or a helper forwarding its own parameter: clean."""
    assert _explicit_version_calls(source) == []


_OWNER_KEY_VERSION_WRITERS = {
    # The one builder of the Spot request; both helpers go through it.
    "SpotApi/GetEidInfoForE2eeDevices/get_eid_info_request.py",
    # Registers a new tracker: EncryptedUserSecrets of the pairing request,
    # not a Spot owner-key request.
    "SpotApi/CreateBleDevice/create_ble_device.py",
}


def _owner_key_version_writes(source: str) -> list[int]:
    """Lines that set ``ownerKeyVersion`` on a request: attribute target, keyword
    of a constructor call, or ``setattr(…, "ownerKeyVersion", …)``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        lines += [
            node.lineno
            for target in targets
            if isinstance(target, ast.Attribute) and target.attr == "ownerKeyVersion"
        ]
        if isinstance(node, ast.Call):
            if any(k.arg == "ownerKeyVersion" for k in node.keywords):
                lines.append(node.lineno)
            if (
                _callee(node.func, {}) == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "ownerKeyVersion"
            ):
                lines.append(node.lineno)
    return sorted(set(lines))


def test_only_the_request_builder_writes_owner_key_version() -> None:
    """A Spot request built by hand would bypass both helpers and the detector."""
    writers = {
        str(path.relative_to(_PACKAGE)): hits
        for path in sorted(_PACKAGE.rglob("*.py"))
        if not path.name.endswith("_pb2.py")
        and (hits := _owner_key_version_writes(path.read_text(encoding="utf-8")))
    }
    assert set(writers) == _OWNER_KEY_VERSION_WRITERS


@pytest.mark.parametrize(
    "source",
    [
        "req.ownerKeyVersion = 4\n",
        "req.ownerKeyVersion += 1\n",
        "Request(ownerKeyVersion=4)\n",
        'setattr(req, "ownerKeyVersion", 4)\n',
    ],
)
def test_owner_key_version_write_detector_controls(source: str) -> None:
    assert _owner_key_version_writes(source) == [1]
    assert _owner_key_version_writes("v = msg.ownerKeyVersion\n") == []


def test_detector_positions_come_from_the_signatures() -> None:
    """Keyword-only in ``async_get_eid_info``, first positional in the builder."""
    assert _positional_version_index() == {
        "async_get_eid_info": None,
        "_build_request_bytes": 0,
    }


@pytest.mark.asyncio
async def test_async_get_eid_info_forwards_explicit_version_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime call builds the request without arguments; N is forwarded."""
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
        get_eid_info_request as module,
    )

    built: list[tuple[int, ...]] = []

    def _spy(*args: int) -> bytes:
        built.append(args)
        return b"request"

    async def _spot(_scope: str, _payload: bytes, *, cache: object) -> bytes:
        return b""

    class _Response:
        def ParseFromString(self, _data: bytes) -> None:
            return None

    monkeypatch.setattr(module, "_build_request_bytes", _spy)
    monkeypatch.setattr(module, "_spot_call_async", _spot)
    monkeypatch.setattr(
        module.DeviceUpdate_pb2, "GetEidInfoForE2eeDevicesResponse", _Response
    )

    for kwargs in ({}, {"owner_key_version": 5}):
        try:
            await module.async_get_eid_info(cache=object(), **kwargs)
        except Exception:  # noqa: BLE001 - an empty body may raise after the build
            pass

    assert built == [(), (5,)]


@pytest.mark.asyncio
async def test_diagnosis_unusable_browser_packages_prints_the_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unusable browser packages print their hint and count as an HTTP cause."""

    async def _unusable(*, on_start: Any = None) -> VaultFetchResult:
        raise BrowserPackagesUnusable("install the browser packages first")

    rc = await cli_main._run_owner_key_diagnosis(
        object(), 0, fetch=_unusable, eid_info=_Spot(default=1, answers={})
    )

    out = capsys.readouterr()
    assert rc == 0
    assert "cause=http_status" in out.out.splitlines()
    assert "install the browser packages first" in out.err


def _drive_without_loop(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine that never suspends to its result, without a loop."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise AssertionError("the coroutine suspended; it needs a real event loop")


def test_diagnosis_cli_signs_in_and_runs_with_the_file_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sign-in, entry id, file cache and token come first; the rc is passed on."""
    calls: list[object] = []
    file_cache = object()

    def _entry_id(arg: object, env: object) -> str:
        calls.append(("entry", arg, env))
        return "entry-9"

    def _register(entry_id: str) -> object:
        calls.append(("cache", entry_id))
        return file_cache

    async def _aas(cache: object) -> None:
        calls.append(("aas", cache is file_cache))

    async def _diagnosis(cache: object, requested: int) -> int:
        calls.append(("diagnosis", cache is file_cache, requested))
        return 3

    monkeypatch.setattr(cli_main, "_ensure_authenticated", lambda: calls.append("auth"))
    monkeypatch.setattr(cli_main, "_resolve_effective_entry_id", _entry_id)
    monkeypatch.setattr(cli_main, "_register_file_cache", _register)
    monkeypatch.setattr(cli_main, "_ensure_aas_token", _aas)
    monkeypatch.setattr(cli_main, "_run_owner_key_diagnosis", _diagnosis)
    monkeypatch.setenv("GOOGLEFINDMY_ENTRY_ID", "env-entry")
    # No competing event loop in tests (tests/AGENTS.md): every double above
    # returns without suspending, so the coroutine runs to its end on send().
    monkeypatch.setattr(asyncio, "run", _drive_without_loop)

    rc = cli_main._owner_key_diagnosis_cli(
        SimpleNamespace(entry="arg-entry", diagnose_owner_key_versions=5)
    )

    assert rc == 3
    assert calls == [
        "auth",
        ("entry", "arg-entry", "env-entry"),
        ("cache", "entry-9"),
        ("aas", True),
        ("diagnosis", True, 5),
    ]
