# tests/test_location_request_not_accepted.py
"""Contract: a locate request that never reached the accept point is distinguishable.

``location_request.get_location_data_for_device`` used to return ``[]`` both for a
request the server ACCEPTED that had nothing to report (the healthy idle outcome of
a BLE tag with no reporter in range) and for a request that never got that far.
``api.async_get_device_location`` mapped both to ``{}``, so both coordinator callers
read every non-raising return as positive proof that the credentials work
(``PLAN_GFMY_EMPTY_RESULT_DISTINGUISHABLE``).

``LocationRequestNotAcceptedError`` carries the second state across the
``location_request.py`` boundary. This module has two halves, and mixing them up is
how the change becomes inert.

The FIRST half pins what the CHOICE OF BASE CLASS can decide, and nothing beyond
it: the signal must not inherit from a base that an existing
NARROW handler routes somewhere wrong (``RuntimeError``, ``DecryptionError``, the
``NovaError`` family, ``HomeAssistantError`` -- each has a measured counterpart), and it
must not reach a handler that catches a fixed tuple without a broad fallback, where it
would propagate uncaught. Both failure modes are measured in
``custom_components/googlefindmy/AGENTS.md``; they are opposite, so neither test is
redundant with the other.

What these tests deliberately do NOT show is that the signal survives a broad
``except Exception``. It does not: ``Exception`` is the base, so every broad handler
catches it too. That property cannot be bought with a base class at all; it is bought
with an explicit re-raise placed BEFORE each broad handler. Reading the base-class
choice as the whole answer is the documented way for this change to become inert.

The SECOND half, further down under its own banner, is exactly those re-raises: it
drives the real seams with the signal injected at ``api.async_get_device_location``
and pins where it comes out. Behaviour, not typing.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

import custom_components.googlefindmy.api as api_module
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LOCATION_REQUEST_NOT_ACCEPTED_STAGES,
    LocationRequestNotAcceptedError,
)
from custom_components.googlefindmy.NovaApi.nova_request import NovaAuthError, NovaError
from tests.helpers import drain_loop
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.locate_mixin_stub import LocateStub

_LOCATE_TRACKER_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "googlefindmy"
    / "NovaApi"
    / "ExecuteAction"
    / "LocateTracker"
)
_LOCATION_REQUEST_PY = _LOCATE_TRACKER_DIR / "location_request.py"
# The two handlers that catch a fixed tuple with no broad fallback live here.
_TUPLE_HANDLER_MODULES = frozenset({"start_sound_request.py", "stop_sound_request.py"})
_LOCATE_ENTRYPOINT = "get_location_data_for_device"
_PLAY_SOUND_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "googlefindmy"
    / "NovaApi"
    / "ExecuteAction"
    / "PlaySound"
)


def _raised_stages(source: str) -> list[str]:
    """Collect the ``stage=`` literal of every ``LocationRequestNotAcceptedError`` raise."""
    stages: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "LocationRequestNotAcceptedError":
            continue
        for keyword in node.exc.keywords:
            if keyword.arg == "stage" and isinstance(keyword.value, ast.Constant):
                stages.append(str(keyword.value.value))
    return stages


@pytest.mark.parametrize(
    ("base", "why"),
    [
        (RuntimeError, "api.py's broad except RuntimeError would flatten it to {}"),
        # DecryptionError subclasses RuntimeError, so this row cannot fail on its own.
        # It is kept because the two bases fail for different measured reasons, and a
        # future re-parenting of DecryptionError must not silently drop the second one.
        (
            DecryptionError,
            "polling.py would route it into the account-wide reauth verdict",
        ),
        (NovaError, "ten try blocks catch that family, eight with a broad handler"),
        (NovaAuthError, "same family, plus the transient-auth counter branches"),
        (
            HomeAssistantError,
            "exceptions.py reserves that base for finished user-facing messages",
        ),
    ],
)
def test_the_signal_is_not_a_runtime_or_decryption_or_nova_error(
    base: type[BaseException], why: str
) -> None:
    """Each rejected base class has a measured handler that would misroute the signal."""
    assert not issubclass(LocationRequestNotAcceptedError, base), why


def test_the_signal_carries_a_stage_and_no_device_name() -> None:
    """R6: the payload is stage plus status -- never the raw device display name."""
    error = LocationRequestNotAcceptedError(stage="server_error", status=503)

    assert error.stage == "server_error"
    assert error.status == 503
    assert "503" in str(error)
    assert set(vars(error)) == {"stage", "status"}

    bare = LocationRequestNotAcceptedError(stage="no_fcm_token")
    assert bare.status is None
    assert "status" not in str(bare)

    # Pin the constructor itself, not just an instance: the reason no device name can
    # reach ``str(exc)``/``args[0]`` -- which is what travels into the coordinator log
    # records, into ``note_error`` and into any re-wrapped user-facing error -- is that
    # there is no parameter to pass one through. Asserting "the sample name is absent"
    # on an instance built WITHOUT that name would be circular; the signature is not.
    parameters = inspect.signature(LocationRequestNotAcceptedError).parameters
    assert set(parameters) == {"stage", "status"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())

    # And the message stays derived from those two, so a name cannot be baked in
    # as a literal either (the mutation that check exists for).
    assert (
        str(error) == "Location request not accepted (stage=server_error, status=503)"
    )
    assert str(bare) == "Location request not accepted (stage=no_fcm_token)"


def test_no_sound_path_reaches_the_location_request_module() -> None:
    """The second failure mode is constructively absent, measured rather than asserted.

    The two sound-request handlers catch a fixed tuple with no broad fallback, so a
    signal that reached them would propagate uncaught. It cannot, because every raise
    site AP-3 adds sits inside ``get_location_data_for_device`` (today there are none
    yet) and no module under ``PlaySound/`` reaches that function -- not by call, not
    by import, not by a string handed to ``getattr``/``import_module``.

    Stated so it is not mistaken for a full reachability proof: the scan is one hop
    and package-local. Four routes pass it -- reaching the locate path THROUGH a third
    module (``api.py`` exposes it as ``async_get_device_location``), splitting the
    identifier across two literals, binding it to a local name first
    (``fn = module.get_location_data_for_device``), and calling a sound module from a
    new helper outside this package. What it does cover is the direct route, which is
    the one a future edit takes.

    What the sound modules DO reference is measured and deliberately allowed:
    ``_cli_helpers.py`` imports the location_request MODULE to read its
    ``_fcm_receiver_state``/``_FCM_ReceiverGetter`` attribute, and
    ``sound_request.py`` names it in a comment as a sibling builder. Neither can
    produce the signal. Guarding on the module name instead of the function name
    would therefore fail on an unrelated import and stop measuring reachability.
    """
    modules = sorted(_PLAY_SOUND_DIR.rglob("*.py"))
    # Without these anchors a renamed or moved package makes rglob return nothing and
    # the assertion below passes vacuously (tests/AGENTS.md names that failure mode).
    # A count would not do it: the two modules that actually carry the fixed-tuple
    # handlers have to be IN the scan, so they are named rather than counted.
    assert _PLAY_SOUND_DIR.is_dir(), f"{_PLAY_SOUND_DIR} is gone; this test is blind"
    scanned = {path.name for path in modules}
    assert _TUPLE_HANDLER_MODULES <= scanned, (
        f"the modules this test exists for are outside the scan: "
        f"{sorted(_TUPLE_HANDLER_MODULES - scanned)}"
    )

    offenders: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                )
                hit = name == _LOCATE_ENTRYPOINT
            elif isinstance(node, ast.ImportFrom):
                hit = any(alias.name == _LOCATE_ENTRYPOINT for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Covers getattr(module, "...") and any dynamic resolution by name.
                hit = _LOCATE_ENTRYPOINT in node.value
            if hit:
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], (
        f"a sound module now reaches {_LOCATE_ENTRYPOINT}: {offenders}; the "
        "fixed-tuple handlers there have no broad fallback and would let the "
        "signal propagate uncaught"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the raise sites are not armed yet; until then no stage marker is in use. "
        "This is also the tripwire for the prose that is only true until they are: "
        "when it flips to XPASS, re-read the 'as of this step' paragraphs in "
        "api.async_get_device_location's Returns and Raises sections, its "
        "LocationRequestNotAcceptedError pass-through comment, the class "
        "docstring in location_request.py, the module comment above "
        "LOCATION_REQUEST_NOT_ACCEPTED_STAGES (which asserts both that the flag "
        "does not exist and that nothing enforces the set), and the 'Measured: "
        "today' paragraph in coordinator/polling.py's handler. Nothing else "
        "enforces those."
    ),
)
def test_the_stage_markers_are_a_closed_set() -> None:
    """Every raise site uses a declared marker, and every declared marker is used."""
    stages = _raised_stages(_LOCATION_REQUEST_PY.read_text(encoding="utf-8"))

    assert stages, "no raise site uses the signal yet"
    assert set(stages) <= LOCATION_REQUEST_NOT_ACCEPTED_STAGES
    assert set(stages) == LOCATION_REQUEST_NOT_ACCEPTED_STAGES
    assert len(stages) == len(set(stages)), f"a marker is used twice: {stages}"


# ===========================================================================
# AP-2: the receivers. Installed BEFORE anything raises, on purpose.
#
# The tests above are static: they pin what the choice of base class can decide.
# The tests below are the opposite kind -- they drive the real seams with the
# signal injected at ``api.async_get_device_location``, the single chokepoint both
# coordinator sinks consume, and pin where it comes out. They are the reason the
# base-class tests are not the whole answer: ``Exception`` IS the base, so every
# broad handler on the path catches this type too, and only an explicit branch
# placed BEFORE each of them changes that. This section is what fails if such a
# branch is deleted, reordered behind its broad neighbour, or never installed.
#
# Injecting at the api seam rather than deep in ``location_request`` is deliberate
# and mirrors ``tests/test_transient_owner_key_propagation.py``: the receivers must
# be provably correct while the sender is still silent, because arming the sender
# first would let the signal reach the poll loop's broad per-device handler, which
# sets both ``cycle_failed`` and ``last_exception`` -- one 5xx on one tracker would
# mark every entity of the account unavailable.
# ===========================================================================


class _DummyCache:
    """Minimal cache stub for the real coordinator build."""

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(self, _key: str, _value):  # pragma: no cover
        return None


class _DummyHass:
    """Minimal HA stub exposing the loop and task helper the coordinator needs."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict = {}

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401
        return self.loop.create_task(coro)


class _NotAcceptedAPI:
    """API stub whose per-device locate raises the signal, counting its calls.

    The call count pins that the signal does not ABORT the cycle: the second
    device is still requested. It deliberately does not claim more than that.
    Measured, it cannot prove the ``continue`` itself, because the broad neighbour
    it precedes carries neither ``continue`` nor ``raise`` -- after it the loop body
    only reaches the inter-device delay and iterates on. The one observable
    difference the ``continue`` makes is skipping that delay, which is what the
    empty-result arm does today for the same 429 or 5xx.
    """

    def __init__(self, error: LocationRequestNotAcceptedError) -> None:
        self._error = error
        self.calls: list[str] = []

    async def async_get_device_location(self, dev_id: str, _dev_name: str):
        self.calls.append(dev_id)
        raise self._error


class _StubCache:
    """Minimal cache the real ``GoogleFindMyAPI`` constructor accepts."""

    entry_id = "entry-not-accepted"

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(self, _key: str, _value):  # pragma: no cover
        return None


def _make_api() -> Any:
    """Build a ``GoogleFindMyAPI`` through its real constructor.

    Deliberately not the ``__new__``-plus-hand-set-attributes shortcut its nearest
    sibling ``tests/test_api_transient_owner_key.py`` uses: that shape breaks with
    an ``AttributeError`` -- not a statement about the subject -- the day the
    constructor reads one more field. The locate seam is stubbed at the module
    binding anyway, so nothing here needs the wiring to be absent.
    """
    return api_module.GoogleFindMyAPI(cache=_StubCache())


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    api: object,
    devices: list[dict[str, str]],
):
    """Build a real coordinator wired with stubs (mirrors the transient AP10 test)."""
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    hass = _DummyHass(loop)
    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = make_config_entry(
        entry_id="entry-id", title="Test Entry"
    )
    coordinator.api = api
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)

    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data):
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data
    return coordinator


@pytest.mark.asyncio
async def test_api_passes_the_signal_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``api.async_get_device_location`` re-raises the signal, unchanged.

    Two claims, and the second is the one that decays quietly. It must ARRIVE
    (rather than being flattened into ``{}`` by the broad handler), and it must
    arrive as the SAME object: a handler that caught and re-wrapped it would still
    satisfy ``pytest.raises`` while destroying the stage marker the receivers read.
    Identity is therefore asserted, not just the type.
    """
    signal = LocationRequestNotAcceptedError(stage="server_error", status=503)

    async def _raise_signal(*_a: object, **_k: object) -> list[dict[str, Any]]:
        raise signal

    monkeypatch.setattr(api_module, "get_location_data_for_device", _raise_signal)

    with pytest.raises(LocationRequestNotAcceptedError) as excinfo:
        await _make_api().async_get_device_location("device-xyz", "Tracker")

    assert excinfo.value is signal
    assert excinfo.value.stage == "server_error"
    assert excinfo.value.status == 503


@pytest.mark.asyncio
async def test_the_typeerror_cascade_does_not_retry_on_the_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signal must not be re-sent by the legacy-signature fallback ladder.

    ``async_get_device_location`` wraps its call in three nested ``except TypeError``
    retries for older ``get_location_data_for_device`` signatures. That ladder is
    keyed on TypeError alone, so it is blind to what a call actually did: were the
    signal ever routed into it, one refused request would become four -- against a
    server that just answered 429 or 5xx, which is the worst possible moment to
    retry. One call is the whole assertion.
    """
    signal = LocationRequestNotAcceptedError(stage="rate_limited", status=429)
    calls = 0

    async def _raise_signal(*_a: object, **_k: object) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise signal

    monkeypatch.setattr(api_module, "get_location_data_for_device", _raise_signal)

    with pytest.raises(LocationRequestNotAcceptedError):
        await _make_api().async_get_device_location("device-xyz", "Tracker")

    assert calls == 1


def test_poll_treats_the_signal_as_a_per_device_skip_for_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll loop skips the device and keeps going; the cycle does not fail.

    "For now" is load-bearing. Evaluating the signal cycle-wide is a later step;
    at this point the correct outcome is deliberately the modest one, because the
    broad handler this branch precedes would set ``cycle_failed`` AND
    ``last_exception``, and that turns one refused tracker into an account-wide
    ``unavailable``.

    The load-bearing assertions are ``note_error`` and the two cycle fields, not
    the call list. Without this branch the broad neighbour sets ``last_exception``,
    the ``finally`` block feeds it to ``async_set_update_error``, and
    ``last_update_success`` goes False -- that is what turns red. The call list
    pins the weaker, still worth-having claim that the cycle was not aborted; it
    is not the proof of the skip, and thinning out "redundant" assertions here
    would remove the ones that carry the test.
    """
    loop = asyncio.new_event_loop()
    devices = [
        {"id": "dev-refused", "name": "Refused Tag"},
        {"id": "dev-sibling", "name": "Sibling Tag"},
    ]
    api = _NotAcceptedAPI(
        LocationRequestNotAcceptedError(stage="server_error", status=503)
    )
    coordinator = _make_coordinator(monkeypatch, loop, api, devices)
    note_error = MagicMock(return_value=None)
    coordinator.note_error = note_error

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)
        loop.close()

    assert api.calls == ["dev-refused", "dev-sibling"]
    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None
    # The poll sink records nothing per-tracker here: unlike the manual path there
    # is no user waiting on a verdict, and a per-device error record would show up
    # in diagnostics as a fault the cycle did not commit to.
    note_error.assert_not_called()


def test_poll_does_not_clear_the_auth_state_on_the_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused request must not be read as proof that the credentials work.

    This is the defect itself, in miniature. Today the empty result reaches the
    success path, which runs ``_set_auth_state(failed=False)`` and resets
    ``_consecutive_transient_auth_failures`` -- so a 5xx wipes the transient-auth
    counter on its way through, and the escalation budget never fills. The fix is
    not that the branch below undoes the reset; it is that raising SKIPS the reset
    entirely. Asserting the counter is untouched is what would catch a well-meant
    "restore previous behaviour" edit putting it back.
    """
    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-refused", "name": "Refused Tag"}]
    api = _NotAcceptedAPI(
        LocationRequestNotAcceptedError(stage="fcm_registration_failed")
    )
    coordinator = _make_coordinator(monkeypatch, loop, api, devices)
    set_auth_state = MagicMock(return_value=None)
    coordinator._set_auth_state = set_auth_state
    coordinator._consecutive_transient_auth_failures = 3
    coordinator._consecutive_timeouts = 2

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)
        loop.close()

    set_auth_state.assert_not_called()
    assert coordinator._consecutive_transient_auth_failures == 3
    # Nor may the branch invent a health signal the request never earned: today a
    # failed request reaches the ``if not location:`` arm, which does NOT clear the
    # timeout counter either. Most sibling handlers in that loop DO clear it
    # (eight of ten unconditionally; ``TimeoutError`` increments it instead, and
    # ``NovaAuthError`` clears it on one of its three exits), so the majority shape
    # is a standing invitation to "fix" this asymmetry. This assertion is what
    # turns that edit red.
    assert coordinator._consecutive_timeouts == 2


@pytest.fixture
def locate_coord(monkeypatch: pytest.MonkeyPatch) -> LocateStub:
    """A ``LocateStub`` past every cooldown gate so the api seam is reached."""
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.locate.time.monotonic",
        lambda: 1000.0,
    )
    entry = make_config_entry(entry_id="not-accepted-locate-entry")
    return LocateStub(config_entry=entry)


@pytest.mark.asyncio
async def test_locate_returns_empty_on_the_signal_without_raising(
    locate_coord: LocateStub,
) -> None:
    """Manual locate degrades to an empty result instead of a user-facing error.

    The broad handler this branch precedes re-wraps whatever reaches it into a
    ``HomeAssistantError``, which surfaces as a red toast. A refused request is not
    an unexpected error, and the user already sees the honest outcome -- the locate
    found nothing -- so the visible behaviour is deliberately unchanged from today.
    What changes is invisible and sits in the next test.
    """
    coord = locate_coord
    coord.api.async_get_device_location = AsyncMock(
        side_effect=LocationRequestNotAcceptedError(stage="network_error")
    )

    result = await coord.async_locate_device("dev-1")

    assert result == {}


@pytest.mark.asyncio
async def test_locate_does_not_clear_the_auth_state_on_the_signal(
    locate_coord: LocateStub,
) -> None:
    """The manual path carries the same ordering defect, and the same fix.

    ``coordinator/locate.py`` calls ``_set_auth_state(failed=False)`` immediately
    after the api call and BEFORE its empty-result guard, so a refused manual
    locate clears the account's auth-failure state exactly as the poll loop does.
    Raising skips it. ``note_error`` is the per-tracker DIAGNOSTIC hook, not a
    counter, and the sibling transient handler calls it by design -- so its use
    here is asserted as intended behaviour rather than tolerated.
    """
    coord = locate_coord
    coord.note_error = MagicMock(return_value=None)
    coord.config_entry.async_start_reauth = MagicMock()
    coord.api.async_get_device_location = AsyncMock(
        side_effect=LocationRequestNotAcceptedError(stage="nova_request_failed")
    )

    result = await coord.async_locate_device("dev-1")

    assert result == {}
    coord._set_auth_state.assert_not_called()
    coord.config_entry.async_start_reauth.assert_not_called()
    coord.note_error.assert_called_once()


def test_the_sync_wrapper_still_flattens_the_signal_to_an_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KNOWN, deliberately unclosed edge: the sync wrapper is not a receiver.

    ``get_device_location`` is the sync entry point for non-HA contexts -- measured,
    its only in-tree caller is the compatibility shim ``api.locate_device`` -- and
    its ``_run_sync_helper`` catches every exception uniformly to return the
    default.
    The signal is therefore flattened right back into ``{}`` there -- the exact
    shape this change removes everywhere else.

    Pinned rather than fixed, and pinned HERE rather than left implicit, because an
    unstated exception is indistinguishable from an oversight. No coordinator uses
    this entry point; closing it belongs to the step that tightens the CLI edges.
    If that step lands and forgets this test, the failure is the reminder.
    """
    signal = LocationRequestNotAcceptedError(stage="no_fcm_token")

    async def _raise_signal(*_a: object, **_k: object) -> list[dict[str, Any]]:
        raise signal

    monkeypatch.setattr(api_module, "get_location_data_for_device", _raise_signal)

    api = _make_api()
    sync_loop = asyncio.new_event_loop()
    api._sync_call_guard = lambda _message: False
    api._resolve_sync_loop = lambda: sync_loop
    try:
        result = api.get_device_location("device-xyz", "Tracker")
    finally:
        sync_loop.close()

    assert result == {}
