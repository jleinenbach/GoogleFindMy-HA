# tests/test_location_request_not_accepted.py
"""Contract: a locate request that never reached the accept point is distinguishable.

``location_request.get_location_data_for_device`` used to return ``[]`` both for a
request the server ACCEPTED that had nothing to report (the healthy idle outcome of
a BLE tag with no reporter in range) and for a request that never got that far.
``api.async_get_device_location`` mapped both to ``{}``, so both coordinator callers
read every non-raising return as positive proof that the credentials work
(``PLAN_GFMY_EMPTY_RESULT_DISTINGUISHABLE``).

``LocationRequestNotAcceptedError`` carries the second state across the
``location_request.py`` boundary. This module pins what the CHOICE OF BASE CLASS can
decide, and nothing beyond it: the signal must not inherit from a base that an existing
NARROW handler routes somewhere wrong (``RuntimeError``, ``DecryptionError``, the
``NovaError`` family, ``HomeAssistantError`` -- each has a measured counterpart), and it
must not reach a handler that catches a fixed tuple without a broad fallback, where it
would propagate uncaught. Both failure modes are measured in
``custom_components/googlefindmy/AGENTS.md``; they are opposite, so neither test is
redundant with the other.

What these tests deliberately do NOT show is that the signal survives a broad
``except Exception``. It does not: ``Exception`` is the base, so every broad handler
catches it too. That property cannot be bought with a base class at all; it is bought
with an explicit re-raise placed BEFORE each broad handler, which AP-2 and AP-3 install
and the AST guard in ``tests/test_nova_request.py`` pins. Reading the base-class choice
as the whole answer is the documented way for this change to become inert.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LOCATION_REQUEST_NOT_ACCEPTED_STAGES,
    LocationRequestNotAcceptedError,
)
from custom_components.googlefindmy.NovaApi.nova_request import NovaAuthError, NovaError

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
    reason="AP-3 arms the raise sites; until then no stage marker is in use",
)
def test_the_stage_markers_are_a_closed_set() -> None:
    """Every raise site uses a declared marker, and every declared marker is used."""
    stages = _raised_stages(_LOCATION_REQUEST_PY.read_text(encoding="utf-8"))

    assert stages, "no raise site uses the signal yet"
    assert set(stages) <= LOCATION_REQUEST_NOT_ACCEPTED_STAGES
    assert set(stages) == LOCATION_REQUEST_NOT_ACCEPTED_STAGES
    assert len(stages) == len(set(stages)), f"a marker is used twice: {stages}"
