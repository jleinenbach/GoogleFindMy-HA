# tests/test_log_hygiene_coordinator_nova.py
"""Log hygiene for the coordinator and Nova modules.

Covers ``coordinator/identity.py``, ``coordinator/locate.py``,
``NovaApi/ExecuteAction/LocateTracker/decrypt_locations.py`` and
``NovaApi/ListDevices/nbe_list_devices.py``.

Static checks walk every logger call in these modules:

* No argument contains a name or attribute with ``secret`` in it. Such names
  (``secrets_creation_date``, ``_SECRETS_STRUCT_LEN_THRESHOLD``) make CodeQL
  treat the value as a secret even when it is a timestamp or a length limit.
* No argument reads a coordinate (``lat``, ``lon``, ``lat_f``, ``lon_f``,
  ``latitude``, ``longitude`` as a name, an attribute or a string subscript)
  unless it is wrapped in ``_coordinate_kind`` or ``_coordinate_in_range``.
  ``AGENTS.md`` forbids precise coordinates in logs.
* ``identity.py`` passes no key material (``identity_key``,
  ``normalized_candidates``, ``candidate``, ``encrypted_identity_key``) to a
  logger.

The first and third check skip arguments of ``len``, ``type`` and
``isinstance``, because a length or a type name carries none of the value.

``nbe_list_devices.py`` is also a command-line tool whose purpose is to print
the location of a tracker to the person who runs it. That ``print`` is a
declared rest, not a log line; the test pins that coordinates are printed only
inside ``_print_locations``.

Behavioural checks pin that both rejection paths of ``_normalize_coords`` log
neither the rejected value nor the valid half of a pair.

Not covered: values that reach a logger through a helper or a variable with an
unrelated name, ``extra`` built before the call, logger methods bound to an
alias, and ``print`` calls outside ``nbe_list_devices.py``.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from custom_components.googlefindmy.coordinator import identity, locate
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.NovaApi.ListDevices import nbe_list_devices
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.locate_mixin_stub import LocateStub

_MODULES = tuple(
    Path(module.__file__)
    for module in (identity, locate, decrypt_locations, nbe_list_devices)
)
_LOGGER_OBJECT = re.compile(r"(?i)(_logger|logger|self.logger|log)")
_LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warn",
        "warning",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)
_SECRET_NAME = re.compile(r"(?i)secret")
_COORDINATE_NAMES = frozenset({"lat", "lon", "lat_f", "lon_f", "latitude", "longitude"})
_COORDINATE_HELPERS = frozenset({"_coordinate_kind", "_coordinate_in_range"})
_SHAPE_ONLY_CALLS = frozenset({"len", "type", "isinstance"})
_KEY_MATERIAL_NAMES = frozenset(
    {
        "identity_key",
        "normalized_identity_key",
        "decrypted_identity_key",
        "encrypted_identity_key",
        "normalized_candidates",
        "identity_candidates",
        "candidate",
        "effective_identity_for_log",
    }
)
# A precise coordinate pair used only as test input.
_LAT = "48.137412"
_LON = "11.575491"


def _logger_calls(path: Path) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
            and _LOGGER_OBJECT.search(ast.unparse(node.func.value))
        ):
            calls.append(node)
    return calls


def _call_values(call: ast.Call) -> list[ast.expr]:
    # The message argument is included: a message formatted before the call
    # carries its values in that argument.
    start = 1 if isinstance(call.func, ast.Attribute) and call.func.attr == "log" else 0
    return [*call.args[start:], *(kw.value for kw in call.keywords)]


def _identifiers(node: ast.AST) -> list[str]:
    """Return names, attribute names and string subscripts read in ``node``.

    Arguments of ``len``, ``type`` and ``isinstance`` are skipped: a length or
    a type name carries none of the value.
    """

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _SHAPE_ONLY_CALLS
    ):
        return []
    found: list[str] = []
    if isinstance(node, ast.Name):
        found.append(node.id)
    elif isinstance(node, ast.Attribute):
        found.append(node.attr)
    elif (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        found.append(node.slice.value)
    for child in ast.iter_child_nodes(node):
        found.extend(_identifiers(child))
    return found


def _coordinate_reads(node: ast.AST) -> list[str]:
    """Coordinate names read in ``node`` outside the two coordinate helpers."""

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _COORDINATE_HELPERS
    ):
        return []
    hits: list[str] = []
    if isinstance(node, ast.Name) and node.id in _COORDINATE_NAMES:
        hits.append(node.id)
    elif isinstance(node, ast.Attribute) and node.attr in _COORDINATE_NAMES:
        hits.append(node.attr)
    elif (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in _COORDINATE_NAMES
    ):
        hits.append(str(node.slice.value))
    for child in ast.iter_child_nodes(node):
        hits.extend(_coordinate_reads(child))
    return hits


def test_every_module_has_logger_calls() -> None:
    """Guard against a vacuous pass if the logger detection stops matching."""

    for path in _MODULES:
        assert _logger_calls(path), path.name


def test_no_secret_named_value_passed_to_logger() -> None:
    offenders = [
        f"{path.name}:{call.lineno}: {name}"
        for path in _MODULES
        for call in _logger_calls(path)
        for value in _call_values(call)
        for name in _identifiers(value)
        if _SECRET_NAME.search(name)
    ]
    assert offenders == []


def test_no_raw_coordinate_passed_to_logger() -> None:
    offenders = [
        f"{path.name}:{call.lineno}: {name}"
        for path in _MODULES
        for call in _logger_calls(path)
        for value in _call_values(call)
        for name in _coordinate_reads(value)
    ]
    assert offenders == []


def test_identity_logs_no_key_material() -> None:
    path = Path(identity.__file__)
    offenders = [
        f"{call.lineno}: {name}"
        for call in _logger_calls(path)
        for value in _call_values(call)
        for name in _identifiers(value)
        if name in _KEY_MATERIAL_NAMES
    ]
    assert offenders == []


def test_cli_prints_coordinates_only_in_print_locations() -> None:
    """Pin the declared rest: the CLI prints a location in one place only."""

    tree = ast.parse(Path(nbe_list_devices.__file__).read_text(encoding="utf-8"))
    printing: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
                and any(_coordinate_reads(arg) for arg in node.args)
            ):
                printing.add(func.name)
    assert printing == {"_print_locations"}


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (f"{_LAT}x", "text"),
        (_LAT.encode(), "bytes"),
        ({"lat": _LAT}, "a mapping"),
        ([_LAT], "a sequence"),
        (object(), "another type"),
    ],
)
def test_coordinate_kind_is_a_fixed_label(value: object, kind: str) -> None:
    assert locate._coordinate_kind(value) == kind


@pytest.fixture
def coord() -> LocateStub:
    return LocateStub(config_entry=make_config_entry(entry_id="hygiene-entry"))


def test_non_numeric_rejection_logs_no_value(
    coord: LocateStub, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {"latitude": f"{_LAT},", "longitude": [_LON]}
    with caplog.at_level(logging.WARNING, logger=locate.__name__):
        assert coord._normalize_coords(payload, device_label="Keys") is False
    assert "non-numeric" in caplog.text
    assert "lat is text, lon is a sequence" in caplog.text
    assert _LAT[:6] not in caplog.text
    assert _LON[:6] not in caplog.text


def test_out_of_range_rejection_hides_the_valid_half(
    coord: LocateStub, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {"latitude": _LAT, "longitude": "999.5"}
    with caplog.at_level(logging.WARNING, logger=locate.__name__):
        assert coord._normalize_coords(payload, device_label="Keys") is False
    assert "lat ok, lon invalid" in caplog.text
    assert _LAT[:6] not in caplog.text
    assert "999" not in caplog.text


def test_out_of_range_rejection_names_an_invalid_latitude(
    coord: LocateStub, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {"latitude": "nan", "longitude": _LON}
    with caplog.at_level(logging.WARNING, logger=locate.__name__):
        assert coord._normalize_coords(payload) is False
    assert "lat invalid, lon ok" in caplog.text
    assert _LON[:6] not in caplog.text
