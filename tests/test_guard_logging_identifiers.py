# tests/test_guard_logging_identifiers.py
"""Guard: no log call in the integration passes a class (c) identifier in clear text.

`AGENTS.md` section 5 grades device identifiers by one question: can somebody
who holds only the log file use the value without owning this Home Assistant
instance or this Google account? Class (c) (hardware addresses, stable
identifiers of third parties) is never logged in clear text; class (a)
(rotating identifiers) and class (b) (registry identifiers of this instance,
the operator's own Google canonical ids) are allowed.

This guard watches class (c) only. Class (b) is not watched: 253 DEBUG sites
carry `entry_id`/`device_id` values that are contract-conformant, and a guard
over them would have to whitelist every single one. Class (c) is recognised
at the identifier: a leaf named like an address (`ble_address`, `mac`,
`scanner_address`) or the bare Bermuda `scanner` name. A value counts as
handled only when a masking helper wraps it (same rule as the account-address
guard in `test_config_flow_basics.py`); any other wrapper still leaks.

Phone values are recognised the same way: a leaf named like one of the phone
secrets (`_PHONE_SECRET_NAME`), an `android_id`/`androidId` outside `Auth/`, and
every field of `decoder._SECRET_PROTO_FIELDS` in schema or snake_case spelling,
compared on the whole leaf. The set is derived, so a field the decoder hides is
watched here without an edit.

A phone's Android ID is also followed through names the patterns do not know.
It enters a function at `decoder.owned_phone_target(...)`,
`decoder.canonicless_android_id(...)`, the field `phoneInformationField1`, an
`android_id` outside `Auth/` or the target table (`phone_target`). Every name
bound from such an expression is a carrier, and so is every name bound from a
carrier: plain assignment, tuple and starred unpacking (`cid, aid = target`),
subscripts (`target[1]`, `owned[canonical_id]`), `.values()`, `.items()`,
`.keys()`, `.get()`, `.pop()`, loop and comprehension targets, `with ... as`,
`match` captures, the walrus, f-strings, `str()`/`int()`/`hex()` and the
container constructors, a container the value is stored in (`table[key] = value`,
`.append`, `.update`, ...) and an attribute chain assigned in the same function
(`self._last = aid`). A carrier in a log call is reported under its own name,
also when another call wraps it; for carriers, `len()`, `bool()`, `type()`, a
comparison and `not` do not show the value and are not reported. A watched
source name (`_phone_targets`, `android_id`) is reported inside them as well.
The condition of `a if c else b` hands no value on to a carrier, but inside a
log call it is reported like any other operand. One function together with
the functions nested in it is one unit, the module level is another whose
carriers every function sees. The order of statements is ignored.

Blind spots, stated so that a green run is not read as more: a field named by a
string argument (`data.get("imei")`, `getattr(rec, "imei")`, `data[KEY_IMEI]`),
a longer name that contains a field name (`device_imei`), and a whole message
passed to the log (`str(phone_info)`, `MessageToDict(meta)`). Ordinary words in
the set (`encrypted`, `iv`, `email`) also match harmless uses such as
`len(iv)`; such a site is masked or listed in `_NOT_AN_ADDRESS` with its reason.

Limits of the tracking: it follows the Android ID only. A hardware address, an
e-mail address or another watched value bound to a neutral name
(`suffix = f"email={email}"`) is not followed. It ends at the function: a
parameter that receives the value from its caller, the result of a call other
than the two decoder predicates and the listed pass-through calls
(`row = helper(aid)`), a source function handed on as a reference instead of
being called (`async_add_executor_job(owned_phone_target, ...)`, an alias of
it), an exception that carries the value (`except ... as err`), a `return`
value, an attribute read in another method or
on another object (`self._last` set in one method and logged in the next;
`request.owner` when the request was built elsewhere) and a value that travels
through a module-level name assigned inside a function are not seen. In the
other direction it over-reports: all names of an unpacking and all keys and
values of a table count, so the canonical ID out of `cid, aid = target` or a
key of the target table in a log call is reported although it is class (b),
and a name that held the value once counts everywhere in its function. The
run-time sweep in `test_phone_log_hygiene.py` looks for the value itself, by
sentinel and by shape, on the paths it drives.

The scan is package-wide and covers all log levels, because a clear-text
address is equally wrong at DEBUG and at WARNING. Written as an AST walk
rather than a pin on the sites that were found, because the leak reappears
with every new log line, in a shape nobody predicted.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from collections.abc import Set as AbstractSet
from pathlib import Path

import pytest

from custom_components import googlefindmy
from custom_components.googlefindmy.ProtoDecoders import decoder

_PACKAGE_ROOT = Path(googlefindmy.__file__).resolve().parent

# Leaf identifiers that carry a class (c) value. `(^|_)mac(_|$)` and
# `address` cover hardware addresses in every spelling the tree uses;
# `^scanner$` is the bare Bermuda scanner name (the scanner device's name,
# which falls back to a slug of its MAC, see `_mask_address_for_logs`).
# `scanner_device_id` is a Home Assistant registry id (class (b)) and is
# deliberately not matched.
_ADDRESS_NAME = re.compile(r"(^|_)mac(_|$)|address|^scanner$", re.IGNORECASE)

# Values of phones listed without a canonical ID: the
# phone's Wi-Fi name, its SIM name, the per-request locate key, the per-entry
# key behind the phone row ids, ciphertext and raw pushes. Only names that are
# unambiguous across the package: a bare `android_id` is the integration's own
# GSF ID in `Auth/` (logged there by design) and a bare `key` is mostly a dict
# key, so the phone modules carry their own, stricter guard in
# `tests/test_phone_log_hygiene.py`. `phone_target` names the table from row id
# to Android ID (`api.py`), whose values are class (c).
_PHONE_SECRET_NAME = re.compile(
    r"ssid|wifi|sim_display|locate_key|phone_id_key|ciphertext|push_hex"
    r"|phone_android_id|phone_status|phone_target",
    re.IGNORECASE,
)

# Outside `Auth/` an `android_id` (also `androidId`) is a phone's Android ID
# (class (c)); inside it is the integration's own GSF ID and stays allowed.
_ANDROID_ID_NAME = re.compile(r"android_?id", re.IGNORECASE)


def _spelling_key(name: str) -> str:
    """One key for the camelCase and the snake_case spelling of a name."""
    return name.replace("_", "").lower()


# Fields of the protobuf schema that the decoder never shows
# (`decoder._SECRET_PROTO_FIELDS`, kept complete by
# `tests/test_decoder_spot_schema.py`). A value read from such a field is a
# secret or personal data whatever the code calls it, so the field name itself
# is a leaf to watch, in the schema spelling (`simDisplayName`,
# `phoneInformationField1`) and in snake_case (`sim_display_name`). Derived, not
# listed: a field added to the decoder's set is watched here without an edit.
# Matched on the whole leaf, not as a substring: `iv` and `email` are fields of
# that set and must not match `active` or `email_hash`.
_SECRET_FIELD_KEYS: frozenset[str] = frozenset(
    _spelling_key(name) for name in decoder._SECRET_PROTO_FIELDS
)
_OWN_ANDROID_ID_DIR = "Auth/"

# Identifiers that match the pattern but are not addresses. Empty on purpose:
# an entry is added only when the red probe below reports a false positive,
# never as a stock of guesses.
_NOT_AN_ADDRESS: frozenset[str] = frozenset()

# Helpers whose return value is log-safe.
_MASKERS = frozenset(
    {"_mask_address_for_logs", "_mask_email_for_logs", "_redact_account_for_log"}
)

# Reviewed sites that log a class (a) value under an address-like name:
# (path relative to the package, leaf name). `ble_address` in the BLE scanner
# is `service_info.address`, the MAC of an FMDN advertisement. The resolved
# branch logs it in full: the advertisement belongs to one of the user's own
# trackers and the MAC rotates with the EID (`eid_resolver.py`,
# `EIDMatch.ble_address` docstring; `docs/FMDN.md` S3.5). The unresolved
# branch of the same module masks it (somebody else's tracker until proven
# otherwise, class (c)). The exception is keyed by (path, leaf, format-string
# prefix) so that it excuses exactly that one site and not every `ble_address`
# line of the module; `test_reviewed_rotating_sites_still_exist` fails when
# the site disappears.
# A line moved out of the resolved branch while keeping the prefix would
# still count as the one match; that case is caught by the behavioural test
# `test_callback_masks_unresolved_advertisement_mac` in
# `test_ble_scanner_contracts.py`, which drives the unresolved path and asserts
# the clear-text MAC is absent.
_REVIEWED_ROTATING: frozenset[tuple[str, str, str]] = frozenset(
    {("fmdn_finder/ble_scanner.py", "ble_address", "BLE scan: resolved")}
)

_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


# Where a phone's Android ID enters a function under a name the patterns
# above know: the two decoder predicates that return it, the schema field, any
# `android_id` outside `Auth/` and the target table. From there the guard
# follows the value through neutral names (`_carriers`).
_ANDROID_ID_FUNCTIONS = frozenset({"owned_phone_target", "canonicless_android_id"})
_ANDROID_ID_FIELD = _spelling_key("phoneInformationField1")
_PHONE_TARGET_NAME = re.compile(r"phone_target", re.IGNORECASE)

# Calls and operators whose result says nothing about the value passed in: a
# count, a truth value, a type. A carrier inside one of them is neither
# reported nor does it make the bound name a carrier. Watched names keep the
# older, stricter rule and are reported there as well (`len(iv)`).
_VALUE_FREE = frozenset({"len", "bool", "type", "isinstance", "callable", "hasattr"})

# Calls that hand a value on: conversions and containers by function name,
# readers of a container or a string by method name. Any other call is opaque
# for the tracking (its result is not followed), see the module docstring.
_PASS_THROUGH_FUNCTIONS = frozenset(
    {
        "dict",
        "enumerate",
        "format",
        "frozenset",
        "getattr",
        "hex",
        "int",
        "iter",
        "list",
        "max",
        "min",
        "next",
        "repr",
        "reversed",
        "set",
        "sorted",
        "str",
        "tuple",
        "zip",
    }
)
_PASS_THROUGH_METHODS = frozenset(
    {"copy", "format", "get", "items", "join", "keys", "pop", "popitem", "values"}
)

# Methods that store their arguments in the receiver.
_MUTATORS = frozenset(
    {"add", "append", "appendleft", "extend", "insert", "setdefault", "update"}
)


def _is_watched(leaf: str, relative: str) -> bool:
    """Whether `leaf` is named like a class (c) value."""
    return leaf not in _NOT_AN_ADDRESS and bool(
        _ADDRESS_NAME.search(leaf)
        or _PHONE_SECRET_NAME.search(leaf)
        or _spelling_key(leaf) in _SECRET_FIELD_KEYS
        or (
            _ANDROID_ID_NAME.search(leaf)
            and not relative.startswith(_OWN_ANDROID_ID_DIR)
        )
    )


def _is_android_id_source(leaf: str, relative: str) -> bool:
    """Whether `leaf` names a phone's Android ID or the table that holds it."""
    return leaf not in _NOT_AN_ADDRESS and bool(
        _PHONE_TARGET_NAME.search(leaf)
        or _spelling_key(leaf) == _ANDROID_ID_FIELD
        or (
            _ANDROID_ID_NAME.search(leaf)
            and not relative.startswith(_OWN_ANDROID_ID_DIR)
        )
    )


def _leaf_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return str(node.slice.value)
    return None


def _call_name(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _is_value_free(node: ast.AST) -> bool:
    return (
        _call_name(node) in _VALUE_FREE
        or isinstance(node, ast.Compare)
        or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not))
    )


def _carrier_key(node: ast.AST) -> str | None:
    """`name` or `name.attr` for a plain name or an attribute chain on one."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _carrier_key(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _call_carries(node: ast.Call, carriers: AbstractSet[str], relative: str) -> bool:
    name = _call_name(node)
    if name in _ANDROID_ID_FUNCTIONS:
        return True
    passed = [*node.args, *(kw.value for kw in node.keywords)]
    if isinstance(node.func, ast.Attribute):
        if name not in _PASS_THROUGH_METHODS:
            return False
        passed.append(node.func.value)
    elif name not in _PASS_THROUGH_FUNCTIONS:
        return False
    return any(_carries(part, carriers, relative) for part in passed)


def _carries(node: ast.AST, carriers: AbstractSet[str], relative: str) -> bool:
    """Whether the value of `node` can hold a phone's Android ID.

    True when the expression reads a source name or a carrier outside a masker
    and outside a value-free call. A call hands the value on only when it is a
    source function or listed as pass-through; the condition of
    `a if c else b` does not count.
    """
    if _call_name(node) in _MASKERS or _is_value_free(node):
        return False
    if isinstance(node, ast.Call):
        return _call_carries(node, carriers, relative)
    if isinstance(node, ast.IfExp):
        return _carries(node.body, carriers, relative) or _carries(
            node.orelse, carriers, relative
        )
    leaf = _leaf_of(node)
    if leaf and _is_android_id_source(leaf, relative):
        return True
    if _carrier_key(node) in carriers:
        return True
    return any(
        _carries(child, carriers, relative) for child in ast.iter_child_nodes(node)
    )


def _bind(target: ast.AST, value: ast.AST) -> Iterator[tuple[str, ast.AST]]:
    """(carrier key, expression it is bound from) for one assignment target."""
    if isinstance(target, (ast.Tuple, ast.List)):
        pairwise = (
            isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
            and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
        )
        if pairwise:
            assert isinstance(value, (ast.Tuple, ast.List))
            for element, part in zip(target.elts, value.elts, strict=True):
                yield from _bind(element, part)
        else:
            # `cid, aid = target`: which element holds the value is not known
            # here, so every name of the pattern becomes a carrier.
            for element in target.elts:
                yield from _bind(element, value)
    elif isinstance(target, ast.Starred):
        yield from _bind(target.value, value)
    elif isinstance(target, ast.Subscript):
        # `table[key] = value` stores both in the container.
        yield from _bind(target.value, value)
        yield from _bind(target.value, target.slice)
    else:
        key = _carrier_key(target)
        if key:
            yield key, value


def _pattern_names(pattern: ast.AST) -> Iterator[str]:
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest


def _bindings(nodes: Iterable[ast.AST]) -> Iterator[tuple[str, ast.AST]]:
    """Every place in `nodes` where a name receives the value of an expression."""
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from _bind(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            if node.value is not None:
                yield from _bind(node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            yield from _bind(node.target, node.iter)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                yield from _bind(node.optional_vars, node.context_expr)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                for name in _pattern_names(case.pattern):
                    yield name, node.subject
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
        ):
            for stored in (*node.args, *(kw.value for kw in node.keywords)):
                yield from _bind(node.func.value, stored)


def _carriers(
    nodes: Iterable[ast.AST], inherited: frozenset[str], relative: str
) -> frozenset[str]:
    """Names that can hold a phone's Android ID without being named like one.

    A name becomes a carrier when it is bound from an expression that reads a
    source (`_is_android_id_source`, `_ANDROID_ID_FUNCTIONS`) or another carrier: assignment, unpacking, subscript,
    `.values()`/`.items()`/`.get()`, loop and comprehension targets, `with … as`,
    `match` captures, the walrus, and a container that such a value is stored in.
    Order of statements is ignored (a name that carried the value once counts
    for the whole unit), so the set is computed to a fixed point.
    """
    bindings = list(_bindings(nodes))
    carriers = set(inherited)
    grown = True
    while grown:
        grown = False
        for key, value in bindings:
            if key not in carriers and _carries(value, carriers, relative):
                carriers.add(key)
                grown = True
    return frozenset(carriers)


_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def _outside_functions(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, _FUNCTION):
            yield child
            yield from _outside_functions(child)


def _outermost_functions(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FUNCTION):
            yield child
        else:
            yield from _outermost_functions(child)


def _units(
    tree: ast.AST, relative: str
) -> Iterator[tuple[list[ast.AST], frozenset[str]]]:
    """(nodes, carriers) per unit: the module level, then each outermost function.

    A function is one unit together with the functions nested in it, because a
    closure reads and fills the names of its enclosing function. Carriers of
    the module level are visible in every function.
    """
    module_level = list(_outside_functions(tree))
    shared = _carriers(module_level, frozenset(), relative)
    yield module_level, shared
    for function in _outermost_functions(tree):
        nodes = list(ast.walk(function))
        yield nodes, _carriers(nodes, shared, relative)


def _leaks(
    node: ast.AST, relative: str = "", carriers: AbstractSet[str] = frozenset()
) -> list[str]:
    """Watched names and carriers reachable without a masker."""
    if _call_name(node) in _MASKERS:
        return []
    inner = frozenset() if _is_value_free(node) else carriers
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        found.extend(_leaks(child, relative, inner))
    leaf = _leaf_of(node)
    key = _carrier_key(node)
    if leaf and _is_watched(leaf, relative):
        found.append(leaf)
    elif key in carriers:
        assert key is not None
        found.append(key)
    return found


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_LEVELS:
        return False
    receiver = ast.unparse(func.value)
    return "LOG" in receiver.upper()


def _is_reviewed(
    reviewed: frozenset[tuple[str, str, str]], relative: str, leaf: str, fmt: str
) -> bool:
    # An empty prefix would excuse every line of the module; the exception is
    # meant to name one site, so it must carry a prefix.
    assert all(prefix for _path, _name, prefix in reviewed), (
        "empty prefix in reviewed list"
    )
    return any(
        relative == path and leaf == name and fmt.startswith(prefix)
        for path, name, prefix in reviewed
    )


def scan(
    root: Path = _PACKAGE_ROOT,
    reviewed: frozenset[tuple[str, str, str]] = _REVIEWED_ROTATING,
) -> tuple[list[tuple[str, int, str, str]], int]:
    """Return ((path, line, leaf, format string) offenders, scanned calls)."""
    offenders: list[tuple[str, int, str, str]] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if path.name.endswith("_pb2.py") or "vendor" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for nodes, carriers in _units(tree, relative):
            for node in nodes:
                if not isinstance(node, ast.Call) or not _is_log_call(node):
                    continue
                scanned += 1
                # `.log(level, msg, ...)` carries the format string at index 1.
                msg_index = 1 if _call_name(node) == "log" else 0
                first = node.args[msg_index] if len(node.args) > msg_index else None
                fmt = (
                    first.value
                    if isinstance(first, ast.Constant) and isinstance(first.value, str)
                    else ""
                )
                # The format string is scanned too: an f-string or a
                # `%`-formatted first argument leaks exactly as much as a
                # positional argument.
                arguments: list[ast.AST] = [
                    *node.args,
                    *(kw.value for kw in node.keywords),
                ]
                for argument in arguments:
                    for leaf in _leaks(argument, relative, carriers):
                        if _is_reviewed(reviewed, relative, leaf, fmt):
                            continue
                        offenders.append(
                            (relative, getattr(argument, "lineno", 0), leaf, fmt)
                        )
    return offenders, scanned


def test_no_log_call_passes_a_clear_text_address() -> None:
    offenders, scanned = scan()
    # Vacuum guard: the package carries well over 700 DEBUG calls alone; a
    # scan that sees fewer log calls did not walk the package (wrong root,
    # parse failure, renamed logger).
    assert scanned > 700, (
        f"only {scanned} log calls scanned; the guard would be vacuous"
    )
    assert offenders == [], (
        "Log calls pass a class (c) identifier in clear text "
        "(AGENTS.md section 5): mask it with `_mask_address_for_logs`, or, if the "
        "value rotates, add (path, leaf, format-string prefix) to `_REVIEWED_ROTATING` "
        "with the reason:\n"
        + "\n".join(f"{path}:{line}: {leaf}" for path, line, leaf, _fmt in offenders)
    )


def test_each_reviewed_site_matches_exactly_one_log_call() -> None:
    """The exception list excuses one vetted call per entry, no more, no less.

    Fewer than one: the entry outlived the site it excuses. More than one: a
    log call was copied or moved and kept the vetted prefix, so a foreign
    advertisement MAC could be logged in full while this guard stays green.
    """
    offenders, _ = scan(reviewed=frozenset())
    matches = {
        site: [
            (path, line)
            for path, line, leaf, fmt in offenders
            if _is_reviewed(frozenset({site}), path, leaf, fmt)
        ]
        for site in _REVIEWED_ROTATING
    }
    wrong = {site: hits for site, hits in matches.items() if len(hits) != 1}
    assert wrong == {}, (
        "each reviewed site must match exactly one clear-text log call "
        f"(0 = stale entry, >1 = copied site): {wrong}"
    )


def test_phone_secret_names_are_caught(tmp_path: Path) -> None:
    """Red probe for `_PHONE_SECRET_NAME`: each name in a log call is reported."""
    names = ["wifi_ssid", "locate_key", "phone_id_key", "ciphertext", "push_hex"]
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [f'_LOGGER.debug("x %s", {name})' for name in names]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == len(names)
    assert sorted(leaf for _p, _l, leaf, _f in offenders) == sorted(names)


def test_phone_target_and_android_id_are_caught(tmp_path: Path) -> None:
    """Red probe: the target table and an Android ID outside `Auth/` are reported."""
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [
        '_LOGGER.debug("x %s", self._phone_targets.get(device_id))',
        '_LOGGER.debug("x %s", phone_target[0])',
        '_LOGGER.debug("x %s", android_id)',
    ]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    auth = tmp_path / "Auth"
    auth.mkdir()
    (auth / "own.py").write_text(
        'import logging\n_LOGGER = logging.getLogger(__name__)\n_LOGGER.debug("x %s", android_id)\n',
        encoding="utf-8",
    )
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 4
    assert sorted((path, leaf) for path, _l, leaf, _f in offenders) == [
        ("probe.py", "_phone_targets"),
        ("probe.py", "android_id"),
        ("probe.py", "phone_target"),
    ]


def test_the_owned_phone_read_sites_are_caught(tmp_path: Path) -> None:
    """Red probe in the shapes of the sites that read a phone listed with a canonical ID.

    `decoder.owned_phone_target` reads `phoneInformationField1` into a local
    `android_id`; `api._process_device_list_response` keys `_phone_targets` by the
    canonical ID and the locate handler reads it back by `device_id`. Each shape in
    a log call outside `Auth/` is reported by its name alone.
    """
    caught = [
        "phone_info.phoneInformationField1",
        "android_id",
        "self._phone_targets[device_id]",
        "self._phone_targets.get(canonical_id)",
        "phone_targets",
    ]
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [f'_LOGGER.debug("x %s", {leaf})' for leaf in caught]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == len(caught)
    assert [line for _p, line, _leaf, _f in offenders] == list(
        range(3, 3 + len(caught))
    )
    assert sorted(leaf for _p, _l, leaf, _f in offenders) == sorted(
        [
            "phoneInformationField1",
            "android_id",
            "_phone_targets",
            "_phone_targets",
            "phone_targets",
        ]
    )


_PROBE_HEAD = (
    "import logging\n"
    "_LOGGER = logging.getLogger(__name__)\n"
    "def handler(self, metadata, device, device_id, canonical_id, pair, lookup):\n"
)

# The first lines of `api._process_device_list_response` that fill the local table.
_OWNED_TABLE = """\
owned = {}
target = owned_phone_target(metadata)
if target is not None:
    owned[target[0]] = target[1]
"""


def _probe_leaves(tmp_path: Path, body: str, relative: str = "probe.py") -> list[str]:
    """Reported leaves for `body` placed in a function of a one-module package."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    indented = "".join(f"    {line}\n" for line in body.splitlines())
    path.write_text(_PROBE_HEAD + indented, encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == body.count("_LOGGER."), "the probe's log calls were not all seen"
    return sorted(leaf for _p, _l, leaf, _f in offenders)


# Each case: a function body whose log calls pass the Android ID under a name
# the patterns do not know, and the leaves the guard has to report for it.
_NEUTRAL_NAME_LEAKS: dict[str, tuple[str, list[str]]] = {
    "subscript_of_the_target": (
        'target = owned_phone_target(metadata)\n_LOGGER.debug("x %s", target[1])',
        ["target"],
    ),
    "tuple_unpacking": (
        "target = owned_phone_target(metadata)\n"
        "cid, aid = target\n"
        '_LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
    "starred_unpacking": (
        '_cid, *rest = owned_phone_target(metadata)\n_LOGGER.debug("x %s", rest)',
        ["rest"],
    ),
    "local_table_by_key": (
        _OWNED_TABLE + '_LOGGER.debug("x %s", owned[canonical_id])',
        ["owned"],
    ),
    "values_loop": (
        _OWNED_TABLE + 'for x in owned.values():\n    _LOGGER.debug("x %s", x)',
        ["x"],
    ),
    "items_loop": (
        _OWNED_TABLE + 'for cid, aid in owned.items():\n    _LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
    "table_get": (
        'aid = self._phone_targets.get(device_id)\n_LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
    "table_pop": (
        _OWNED_TABLE + 'gone = owned.pop(canonical_id)\n_LOGGER.debug("x %s", gone)',
        ["gone"],
    ),
    "walrus": (
        "if (aid := canonicless_android_id(device)) is not None:\n"
        '    _LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
    "schema_field": (
        "aid = device.identifierInformation.phoneInformation.phoneInformationField1\n"
        '_LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
    "comprehension": (
        _OWNED_TABLE
        + "ids = [str(v) for v in owned.values()]\n"
        + '_LOGGER.debug("x %s", ids)',
        ["ids"],
    ),
    "comprehension_in_the_call": (
        _OWNED_TABLE + '_LOGGER.debug("x %s", [str(v) for v in owned.values()])',
        # The loop variable is named twice: as the target and as the element.
        ["owned", "v", "v"],
    ),
    "formatted_text": (
        "aid = canonicless_android_id(device)\n"
        'text = f"id={aid:x}"\n'
        'joined = ", ".join([text])\n'
        '_LOGGER.debug("x %s", text)\n'
        '_LOGGER.debug("x %s", joined)',
        ["joined", "text"],
    ),
    "chain_written_backwards": (
        "for _ in range(2):\n"
        '    _LOGGER.debug("x %s", third)\n'
        "    third = second\n"
        "    second = first\n"
        "    first = canonicless_android_id(device)",
        ["third"],
    ),
    "attribute_in_the_same_function": (
        'self._last = owned_phone_target(metadata)\n_LOGGER.debug("x %s", self._last)',
        ["self._last"],
    ),
    "stored_in_a_list": (
        "seen = []\n"
        "seen.append(canonicless_android_id(device))\n"
        '_LOGGER.debug("x %s", seen)',
        ["seen"],
    ),
    "closure": (
        "aid = canonicless_android_id(device)\n"
        "def report():\n"
        '    _LOGGER.debug("x %s", aid)\n'
        "report()",
        ["aid"],
    ),
    "with_target": (
        'with owned_phone_target(metadata) as held:\n    _LOGGER.debug("x %s", held)',
        ["held"],
    ),
    "keys_of_a_table": (
        "seen = {}\n"
        "seen[canonicless_android_id(device)] = device_id\n"
        'for key in seen.keys():\n    _LOGGER.debug("x %s", key)',
        ["key"],
    ),
    "match_capture": (
        "match owned_phone_target(metadata):\n"
        "    case (cid, aid):\n"
        '        _LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
    "wrapped_in_another_call": (
        'aid = canonicless_android_id(device)\n_LOGGER.debug("x %s", lookup(aid))',
        ["aid"],
    ),
    "pairwise_assignment_right_side": (
        "name, aid = device.name, canonicless_android_id(device)\n"
        '_LOGGER.debug("x %s", aid)',
        ["aid"],
    ),
}


@pytest.mark.parametrize("case", sorted(_NEUTRAL_NAME_LEAKS))
def test_an_android_id_under_a_neutral_name_is_caught(
    tmp_path: Path, case: str
) -> None:
    """Red probe: the value is followed from its source to the log call."""
    body, expected = _NEUTRAL_NAME_LEAKS[case]
    assert _probe_leaves(tmp_path, body) == expected


# Each case: the same neutral names without a source, or a use that does not
# show the value. Nothing may be reported.
_NEUTRAL_NAME_HARMLESS: dict[str, str] = {
    "subscript_without_a_source": (
        'target = lookup(device_id)\n_LOGGER.debug("x %s", target[1])'
    ),
    "unpacking_without_a_source": (
        'cid, aid = pair\n_LOGGER.debug("x %s %s", cid, aid)'
    ),
    "table_without_a_source": (
        "owned = {}\n"
        "owned[canonical_id] = device_id\n"
        'for x in owned.values():\n    _LOGGER.debug("x %s", x)\n'
        'for cid, aid in owned.items():\n    _LOGGER.debug("x %s %s", cid, aid)\n'
        '_LOGGER.debug("x %s", owned[canonical_id])'
    ),
    "count_of_the_table": (
        _OWNED_TABLE
        + "count = len(owned)\n"
        + '_LOGGER.debug("x %d", count)\n'
        + '_LOGGER.debug("x %d", len(owned))'
    ),
    "truth_value": (
        "aid = canonicless_android_id(device)\n"
        "known = aid is not None\n"
        "absent = not aid\n"
        '_LOGGER.debug("x %s %s %s", known, absent, aid is None)'
    ),
    "condition_only": (
        "aid = canonicless_android_id(device)\n"
        'label = "phone" if aid else "tracker"\n'
        '_LOGGER.debug("x %s", label)'
    ),
    "masked": (
        "aid = canonicless_android_id(device)\n"
        "masked = _mask_address_for_logs(aid)\n"
        '_LOGGER.debug("x %s %s", masked, _mask_address_for_logs(aid))'
    ),
    "result_of_another_call": (
        "aid = canonicless_android_id(device)\n"
        "row_id = lookup(aid)\n"
        '_LOGGER.debug("x %s", row_id)'
    ),
    "pairwise_assignment_left_side": (
        "name, aid = device.name, canonicless_android_id(device)\n"
        '_LOGGER.debug("x %s", name)'
    ),
}


@pytest.mark.parametrize("case", sorted(_NEUTRAL_NAME_HARMLESS))
def test_a_neutral_name_without_the_android_id_is_not_reported(
    tmp_path: Path, case: str
) -> None:
    """Counter-cases: a name alone is no finding, nor is a count or a truth value."""
    assert _probe_leaves(tmp_path, _NEUTRAL_NAME_HARMLESS[case]) == []


def test_the_own_gsf_id_is_not_followed_inside_auth(tmp_path: Path) -> None:
    """Counter-case: in `Auth/` an `android_id` is the integration's own ID."""
    body = 'aid = android_id\n_LOGGER.debug("x %s", aid)'
    assert _probe_leaves(tmp_path / "own", body, "Auth/own.py") == []
    assert _probe_leaves(tmp_path / "phone", body, "other/phone.py") == ["aid"]


def test_a_carrier_does_not_reach_into_another_function(tmp_path: Path) -> None:
    """Counter-case: `aid` carries the value in one function and a plain pair in the next."""
    source = (
        "import logging\n"
        "_LOGGER = logging.getLogger(__name__)\n"
        "def first(device):\n"
        "    aid = canonicless_android_id(device)\n"
        "    return aid\n"
        "def second(pair):\n"
        "    cid, aid = pair\n"
        '    _LOGGER.debug("x %s %s", cid, aid)\n'
        "class Holder:\n"
        "    def third(self, pair):\n"
        "        aid = pair[1]\n"
        '        _LOGGER.debug("x %s", aid)\n'
    )
    (tmp_path / "probe.py").write_text(source, encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 2
    assert offenders == []


def test_a_module_level_carrier_is_seen_in_a_function(tmp_path: Path) -> None:
    source = (
        "import logging\n"
        "_LOGGER = logging.getLogger(__name__)\n"
        "TABLE = dict([owned_phone_target(None)])\n"
        "def report():\n"
        '    _LOGGER.debug("x %s", TABLE)\n'
    )
    (tmp_path / "probe.py").write_text(source, encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 1
    assert [leaf for _p, _l, leaf, _f in offenders] == ["TABLE"]


def test_the_stated_limits_of_the_tracking(tmp_path: Path) -> None:
    """The limits named in the module docstring, pinned so that they stay true.

    Not seen: a parameter, the result of an unlisted call, an attribute set in
    another method. Over-reported: the canonical ID out of an unpacking.
    """
    source = (
        "import logging\n"
        "_LOGGER = logging.getLogger(__name__)\n"
        "class Api:\n"
        "    def fill(self, device):\n"
        "        self._last = canonicless_android_id(device)\n"
        "        self.report(self._last)\n"
        "        return helper(self._last)\n"
        "    def report(self, value):\n"
        '        _LOGGER.debug("x %s", value)\n'
        "    def later(self):\n"
        '        _LOGGER.debug("x %s", self._last)\n'
        "    def owned(self, metadata):\n"
        "        cid, aid = owned_phone_target(metadata)\n"
        '        _LOGGER.debug("x %s", cid)\n'
    )
    (tmp_path / "probe.py").write_text(source, encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 3
    assert [(line, leaf) for _p, line, leaf, _f in offenders] == [(14, "cid")]


def test_the_android_id_sources_exist_in_the_decoder() -> None:
    """The tracking starts at these names; a rename must fail here, not go blind."""
    for name in sorted(_ANDROID_ID_FUNCTIONS):
        assert callable(getattr(decoder, name, None)), name
    assert "phoneInformationField1" in decoder._SECRET_PROTO_FIELDS


_LIST_HANDLER_ANCHOR = "        phone_targets.update(owned)\n"


@pytest.mark.parametrize(
    ("leak", "leaf"),
    [("target[1]", "target"), ("owned", "owned"), ("list(owned.values())", "owned")],
)
def test_a_leak_through_the_list_handlers_locals_is_caught(
    tmp_path: Path, leak: str, leaf: str
) -> None:
    """Red probe on the real `api.py`: one log line added after the table is filled.

    The copy without the line is clean, so the report is the added line alone.
    """
    source = (_PACKAGE_ROOT / "api.py").read_text(encoding="utf-8")
    assert source.count(_LIST_HANDLER_ANCHOR) == 1
    (tmp_path / "api.py").write_text(source, encoding="utf-8")
    assert scan(root=tmp_path)[0] == []
    line = f'        _LOGGER.debug("x %s", {leak})\n'
    (tmp_path / "api.py").write_text(
        source.replace(_LIST_HANDLER_ANCHOR, _LIST_HANDLER_ANCHOR + line),
        encoding="utf-8",
    )
    offenders, _ = scan(root=tmp_path)
    assert [(path, found) for path, _l, found, _f in offenders] == [("api.py", leaf)]


def _snake(name: str) -> str:
    return re.sub(r"([A-Z])", r"_\1", name).lower()


def test_every_secret_proto_field_is_caught_in_both_spellings(tmp_path: Path) -> None:
    """Red probe: each field of the decoder's secret set, as attribute and snake name.

    Built from the set itself, so a field added there is probed here as well.
    """
    names = sorted(decoder._SECRET_PROTO_FIELDS)
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    for name in names:
        lines.append(f'_LOGGER.debug("x %s", record.{name})')
        lines.append(f'_LOGGER.debug("x %s", {_snake(name)})')
        lines.append(f'_LOGGER.debug("x %s", data["{name}"])')
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 3 * len(names)
    reported = {line for _p, line, _leaf, _f in offenders}
    assert reported == set(range(3, 3 + 3 * len(names))), sorted(
        set(range(3, 3 + 3 * len(names))) - reported
    )


def test_the_schema_spellings_found_in_review_are_caught(tmp_path: Path) -> None:
    """Red probe for the spellings a name list had missed, and `androidId`."""
    leaves = [
        "record.simDisplayName",
        "x.simInformation",
        "phone_info.phoneInformationField1",
        "meta.metadataField11",
        "androidId",
        "self.android_id",
    ]
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [f'_LOGGER.debug("x %s", {leaf})' for leaf in leaves]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, _ = scan(root=tmp_path, reviewed=frozenset())
    assert sorted(leaf for _p, _l, leaf, _f in offenders) == sorted(
        [
            "simDisplayName",
            "simInformation",
            "phoneInformationField1",
            "metadataField11",
            "androidId",
            "android_id",
        ]
    )


def test_a_secret_field_name_matches_the_whole_leaf_only(tmp_path: Path) -> None:
    """Counter-case: `iv` and `email` are in the set; longer names that contain them are not."""
    assert "iv" in decoder._SECRET_PROTO_FIELDS
    assert "email" in decoder._SECRET_PROTO_FIELDS
    lines = ["import logging", "_LOGGER = logging.getLogger(__name__)"]
    lines += [
        '_LOGGER.debug("x %s", active)',
        '_LOGGER.debug("x %s", self.email_count)',
        '_LOGGER.debug("x %s", _mask_email_for_logs(email))',
    ]
    (tmp_path / "probe.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    offenders, scanned = scan(root=tmp_path, reviewed=frozenset())
    assert scanned == 3
    assert offenders == []
