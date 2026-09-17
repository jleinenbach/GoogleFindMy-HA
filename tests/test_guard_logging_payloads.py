# tests/test_guard_logging_payloads.py
"""Guard: no log call in the integration passes raw payload, token or key bytes.

`AGENTS.md` section 5 forbids raw API payloads, tokens and key material in
log records at every level. The value that leaks is not an identifier but a
piece of the wire: a hex dump of a server response, the first bytes of an
undecodable protobuf, the prefix of a shared secret. This guard watches that
class; `test_guard_logging_identifiers.py` watches class (c) identifiers and
the two do not overlap (address-like leaves there, payload-like leaves here).

Seven shapes are recognised inside a log call's arguments:

  (A) a `.hex()` call, whatever the receiver is called;
  (B) a slice (`x[:n]`, `x[a:b]`) whose name chain carries a payload-like
      name (`hex`, `response`, `payload`, `raw`, `secret`, `identity_key`,
      `_bytes`, `body`, `blob`, `token`);
  (C) a serialised protobuf message (`MessageToJson(...)`,
      `MessageToDict(...)`, `MessageToString(...)`, `.SerializeToString()`),
      which dumps every field of a wire message including credentials;
  (D) a name bound in the same function from an HTTP response body
      (`x = await resp.text()`, `resp.read()`, `resp.json()`), whatever the
      name is called and whether or not it is sliced: `text[:400]` is the
      raw body under a neutral name. The taint follows assignments: a name
      bound from an expression that references a body name is a body too
      (`snippet = _decode(content)[:512]`), unless the reference sits under
      a log-safe wrapper (`len`, `_describe_body`, `_describe_error_response`),
      through tuple unpacking and `for` targets as well (`for line in
      text.splitlines(): key, _, value = line.partition("=")` taints
      `line`, `key` and `value`);
  (E) an unsliced argument whose own name is a whole body (`hex_response`,
      `response_hex`, `result_hex`, `hex_string`, `response_bytes`,
      `raw_data`, `raw_bytes`, `raw_response`, `payload`, `body`, `blob`,
      `merged_device_data`, `gcm_data`, `fcm_data`), passed whole to `%s`.
      The list is exact names, not the substring pattern of (B):
      `payload_len`, `raw_prefix` or `status_raw` carry a length, a prefix
      or a status, and a substring rule over them reported 31 sites of
      which two were payloads;
  (F) a mapping unpacked into a dict display that is a log argument
      (`{**gcm_data, "token": redacted}`), whatever the mapping is called:
      every key the mapping carries reaches the record, and redacting one
      of them by name leaves the others (the AidLogin pair `android_id`,
      `security_token` next to the token) in full;
  (G) a function parameter annotated with a protobuf message type (a name
      imported from a `_pb2` module in the same file, or the `MessageProto`
      alias), passed whole to the record: bare, under `str()`/`repr()` or
      inside an f-string. `%s` renders the text format with every
      server-supplied field value (`HeartbeatAck` carried three stream
      counters, a `DataMessageStanza` carries the push); `_msg_str` names
      the type and the fields instead.

The EID is allowed at any level, in full or truncated (`AGENTS.md`, class
(a)); the leaves that carry it are listed in `_ROTATING_LEAVES` and excused
by name. One further site logs the first four bytes of an FMDN advertisement
frame (`payload[:4].hex()` in the BLE scanner: frame byte plus three EID
bytes, rotating) and is excused by (path, leaf, format-string prefix), so the
exception covers that one call and not every `payload` line of the module.

Blind spot, stated on purpose: the guard sees only shapes (A) to (G). Shape
(E) knows an exact list of names: a whole payload under any other name,
or wrapped (`str(payload)`, `repr(payload)`, `payload.decode()`, an
f-string), is not reported. Shape (D) follows assignments inside one
function, not across calls: a helper that returns the body it was given
under a new name is trusted unless the name it is bound to is used in a log
call of the same function. Shape (F) sees the dict display in the call
itself: a copy bound first (`shown = {**gcm_data, ...}`) or built by
`dict(gcm_data, token=...)` and then logged is not followed. Shape (G) trusts
the annotation: a message bound locally (`msg = Stanza.FromString(raw)`), a
parameter without annotation, or a message reached through an attribute
(`self._last_msg`) is not reported. Shape (D) reads plain and annotated assignments, tuple unpacking (starred
included) and `for` targets, not a walrus, `with ... as` or an augmented
assignment, and follows them to a fixpoint within the function. It trusts names: any `.read()`
counts as a body (a file too), and the helpers in `_SAFE_WRAPPERS` are
trusted whatever they return. The fixpoint over-approximates in the other
direction: a scalar derived from a body (`empty = content == b""`) is a
body name too and would be reported if logged; fail-closed, no instance
today. A raw value under a neutral name that was not read from a
response in the same function (`data`, `msg`), a slice whose whole chain is
neutrally named (`page.read()[:16]`), a wrapping call whose arguments hide
the name (`str(response)[:16]`, `bytes(payload)[:16]`: the chain follows the
callee, not the arguments), `binascii.hexlify()`, `base64.b64encode()` or
`repr(bytes)` are not reported; nor is a message passed whole to `%s`
(`str(message)` is its text format). Arguments of a logging wrapper that is
not itself a log call are unchecked, whatever they carry; the two wrappers
the package has, `_log_verbose` and `_log_warn_with_limit`, are treated as
log calls by name, and so is a name bound in the same function from a log
method (`log_fn = _LOGGER.info if first else _LOGGER.warning`). Shape
(C) also reports `len(x.SerializeToString())`, which would log only a
length; the tree binds serialised bytes to a variable first, so that
false positive has no instance today. Section 5 and the
diff review remain the backstop for those; the remaining gap is not
countable. The scan is package-wide and covers all levels, written as an AST
walk rather than a pin on the sites that were found, because the leak
reappears with every new log line.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from custom_components import googlefindmy

_PACKAGE_ROOT = Path(googlefindmy.__file__).resolve().parent

# Leaf names that carry wire bytes, key material or a token. `hex` covers
# `response_hex`, `result_hex`, `hex_string`; `_bytes` covers `response_bytes`
# and `prefix_bytes`; `raw` covers `raw_encrypted_identity_key`; `token` covers
# the FCM push token. `key` is deliberately absent: `key[1][:8]` in the tree
# is a canonical-id tuple, and the key material sites all match `.hex()`.
_PAYLOAD_NAME = re.compile(
    r"hex|response|payload|raw|secret|identity_key|_bytes|body|blob|token",
    re.IGNORECASE,
)

# Leaves that carry the EID (class (a), allowed in full or truncated at any
# level). Checked against the base leaf of a `.hex()` receiver or a slice, so
# `eid[:8].hex()`, `eid_hex[:8]` and `truncated_eid_hex[:8]` pass while
# `identity_key[:8].hex()` does not.
_ROTATING_LEAVES: frozenset[str] = frozenset({"eid", "eid_hex", "truncated_eid_hex"})

# Reviewed sites that log a rotating value under a payload-like name:
# (path relative to the package, base leaf, format-string prefix).
# `payload[:4].hex()` in the BLE scanner is the FMDN frame byte plus the first
# three bytes of the EID of an advertisement that resolved to one of the
# user's own trackers; it rotates with the EID (`docs/FMDN.md`). The prefix
# keys the exception to that one call; `test_each_reviewed_site_matches_
# exactly_one_log_call` fails when the site disappears or is copied.
_REVIEWED: frozenset[tuple[str, str, str]] = frozenset(
    {("fmdn_finder/ble_scanner.py", "payload", "BLE scan: resolved")}
)

# Exact names of values that are a whole payload (shape (E)); an unsliced
# argument with one of these names is the payload itself.
_WHOLE_BODY_NAMES = frozenset(
    {
        "hex_response",
        "response_hex",
        "result_hex",
        "hex_string",
        "response_bytes",
        "raw_data",
        "raw_bytes",
        "raw_response",
        "payload",
        "body",
        "blob",
        "merged_device_data",
        "gcm_data",
        "fcm_data",
    }
)

# Methods that read an HTTP response body (shape (D)); a name bound from one
# of these in the same function is a body, whatever it is called.
_BODY_READERS = frozenset({"text", "read", "json"})

# Helpers whose return value is log-safe even when a body goes in: the
# guard does not descend into their arguments (shape (D)). `len` and `type`
# yield a number or a name; the describers yield a content class.
_SAFE_WRAPPERS = frozenset(
    {
        "len",
        "type",
        "_describe_body",
        "_classify_body",
        "_classify_error_body",
        "_classify_error_code",
        "_describe_error_response",
    }
)

# Aliases under which a protobuf message travels as a parameter type
# (shape (G)); the concrete message classes come from the `_pb2` imports of
# each module.
_MESSAGE_ALIASES = frozenset({"MessageProto", "RuntimeMessage"})

# Callables that serialise a whole protobuf message (shape (C)).
_SERIALISERS = frozenset(
    {"MessageToJson", "MessageToDict", "MessageToString", "SerializeToString"}
)

_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _chain(node: ast.AST) -> list[str]:
    """Every name on the way from a value down to the name it hangs on.

    `payload["web"]["endpoint"][:48]` -> `["payload"]`; `self.key[:8]` ->
    `["key"]`; `response.content[:100]` -> `["content", "response"]`;
    `resp.read()[:16]` -> `["read", "resp"]`; `eid[:8].hex()` (receiver of
    `.hex()`) -> `["eid"]`. The whole chain is matched against
    `_PAYLOAD_NAME`, so a payload-named object slices as a payload even when
    the last attribute or method has a neutral name.
    """
    names: list[str] = []
    while True:
        if isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Attribute):
            if node.attr != "hex":
                names.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Name):
            names.append(node.id)
            return names
        else:
            return names


def _base_leaf(node: ast.AST) -> str | None:
    """The outermost name of a chain (used for the rotating and reviewed lists)."""
    chain = _chain(node)
    return chain[0] if chain else None


def _payload_leaves(argument: ast.AST) -> list[tuple[str, str]]:
    """(shape, base leaf) for every payload-shaped node inside one argument."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(argument):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name == "hex" and isinstance(func, ast.Attribute):
                found.append(("hex", _base_leaf(func.value) or "?"))
            elif name in _SERIALISERS:
                target = node.args[0] if node.args else getattr(func, "value", func)
                found.append(("serialised", _base_leaf(target) or name))
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            chain = _chain(node.value)
            if any(_PAYLOAD_NAME.search(name) for name in chain):
                found.append(("slice", chain[0]))
    return found


# Logging wrappers of the package: a call to one of these is a log call even
# though the receiver is `self`.
_LOG_WRAPPERS = frozenset({"_log_verbose", "_log_warn_with_limit"})


def _names_in_target(target: ast.AST) -> list[str]:
    """Names bound by an assignment or loop target, unpacking included."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _names_in_target(element)]
    if isinstance(target, ast.Starred):
        return _names_in_target(target.value)
    return []


def _assignments(function: ast.AST) -> list[tuple[list[str], ast.AST]]:
    """(bound names, value) of every binding in `function`.

    Plain and annotated assignments, tuple unpacking
    (`key, _, value = line.partition("=")`) and `for` targets
    (`for line in text.splitlines()`), whose value is the iterable.
    """
    found: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            names = [n for target in node.targets for n in _names_in_target(target)]
            found.append((names, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            found.append((_names_in_target(node.target), node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            found.append((_names_in_target(node.target), node.iter))
    return found


def _is_body_read(value: ast.AST) -> bool:
    if isinstance(value, ast.Await):
        value = value.value
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr in _BODY_READERS
    )


def _body_names(function: ast.AST) -> frozenset[str]:
    """Names bound in `function` from a response-body read (shape (D)).

    Fixpoint over the function's assignments: a name bound from a body read,
    or from any expression that references a body name outside a log-safe
    wrapper, is a body name. The order of assignments in the source does not
    matter; the loop runs until no name is added.
    """
    names: set[str] = set()
    assignments = _assignments(function)
    changed = True
    while changed:
        changed = False
        for bound, value in assignments:
            if not (_is_body_read(value) or _body_leaves(value, frozenset(names))):
                continue
            for name in bound:
                if name not in names:
                    names.add(name)
                    changed = True
    return frozenset(names)


def _log_aliases(function: ast.AST) -> frozenset[str]:
    """Names bound in `function` from a log method (`log_fn = _LOGGER.info`)."""
    names: set[str] = set()
    for bound, value in _assignments(function):
        if any(
            isinstance(node, ast.Attribute)
            and node.attr in _LOG_LEVELS
            and "LOG" in ast.unparse(node.value).upper()
            for node in ast.walk(value)
        ):
            names.update(bound)
    return frozenset(names)


def _body_leaves(argument: ast.AST, bodies: frozenset[str]) -> list[tuple[str, str]]:
    """(shape, name) for every reference to a body name inside one argument.

    A reference under a log-safe wrapper (`len(text)`, `_describe_body(text)`)
    is not a leak; the walk stops there.
    """
    found: list[tuple[str, str]] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in _SAFE_WRAPPERS:
                return
        if isinstance(node, ast.Name) and node.id in bodies:
            found.append(("body", node.id))
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(argument)
    return found


def _whole_body_leaves(argument: ast.AST) -> list[tuple[str, str]]:
    """(shape, name) when the argument itself is a whole-body name (shape (E))."""
    if isinstance(argument, (ast.Name, ast.Attribute)):
        chain = _chain(argument)
        if chain and chain[0] in _WHOLE_BODY_NAMES:
            return [("whole", chain[0])]
    return []


def _expansion_leaves(argument: ast.AST) -> list[tuple[str, str]]:
    """(shape, name) for every mapping unpacked into a dict display (shape (F)).

    `ast.Dict` marks a `**` entry with a `None` key; the value is the mapping.
    """
    found: list[tuple[str, str]] = []
    for node in ast.walk(argument):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    found.append(("expansion", _base_leaf(value) or "?"))
    return found


def _message_type_names(tree: ast.AST) -> frozenset[str]:
    """Protobuf message types a module imports (`from ..._pb2 import A, B`)."""
    names = set(_MESSAGE_ALIASES)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.endswith("_pb2")
        ):
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


def _message_params(function: ast.AST, types: frozenset[str]) -> frozenset[str]:
    """Parameters of `function` annotated with a protobuf message type (shape (G)).

    The annotation may be a bare name, a dotted name, a union (`Msg | None`)
    or a subscript (`list[Msg]`, whose `repr` renders every element); the
    last segment of every name in it is matched against `types`. Declared
    imprecision: `type[Msg]` and `Callable[[Msg], ...]` would match too,
    though they carry a class or a callable, not a message; the package has
    no such parameter today (twelve message parameters, all message-typed).
    """
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return frozenset()
    args = function.args
    names: set[str] = set()
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if arg.annotation is None:
            continue
        parts = re.split(r"[^\w.]+", ast.unparse(arg.annotation))
        if any(part.split(".")[-1] in types for part in parts if part):
            names.add(arg.arg)
    return frozenset(names)


def _message_leaves(
    argument: ast.AST, messages: frozenset[str]
) -> list[tuple[str, str]]:
    """(shape, name) when a message parameter is passed whole (shape (G)).

    Whole means bare, wrapped in `str()`/`repr()`, or interpolated into an
    f-string; `_msg_str(msg)`, `type(msg).__name__` and field access are not
    the message.
    """
    node = argument
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"str", "repr"}
        and len(node.args) == 1
    ):
        node = node.args[0]
    if isinstance(node, ast.Name) and node.id in messages:
        return [("message", node.id)]
    if isinstance(node, ast.JoinedStr):
        return [
            ("message", value.value.id)
            for value in node.values
            if isinstance(value, ast.FormattedValue)
            and isinstance(value.value, ast.Name)
            and value.value.id in messages
        ]
    return []


def _is_log_call(node: ast.Call, aliases: frozenset[str] = frozenset()) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in aliases
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _LOG_WRAPPERS:
        return True
    if func.attr not in _LOG_LEVELS:
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
    reviewed: frozenset[tuple[str, str, str]] = _REVIEWED,
    rotating: frozenset[str] = _ROTATING_LEAVES,
) -> tuple[list[tuple[str, int, str, str]], int]:
    """Return ((path, line, leaf, format string) offenders, scanned calls).

    Offenders are deduplicated per log call and leaf: a ternary with two
    `.hex()` nodes on the same leaf is one finding, not two.
    """
    offenders: list[tuple[str, int, str, str]] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if path.name.endswith("_pb2.py") or "vendor" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found, count = _scan_tree(tree, relative, reviewed, rotating)
        offenders.extend(found)
        scanned += count
    return offenders, scanned


def _scan_tree(
    tree: ast.AST,
    relative: str,
    reviewed: frozenset[tuple[str, str, str]] = frozenset(),
    rotating: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, int, str, str]], int]:
    """Offenders and scanned log calls of one parsed module."""
    offenders: list[tuple[str, int, str, str]] = []
    scanned = 0
    # Body names (shape (D)) and message parameters (shape (G)) are
    # scoped to their function.
    bodies_by_call: dict[int, frozenset[str]] = {}
    messages_by_call: dict[int, frozenset[str]] = {}
    aliases_by_call: dict[int, frozenset[str]] = {}
    message_types = _message_type_names(tree)
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies = _body_names(function)
            messages = _message_params(function, message_types)
            aliases = _log_aliases(function)
            if bodies or messages or aliases:
                # Union, not overwrite: a nested function keeps the bodies
                # and message parameters of the function that encloses it.
                for inner in ast.walk(function):
                    if isinstance(inner, ast.Call):
                        bodies_by_call[id(inner)] = (
                            bodies_by_call.get(id(inner), frozenset()) | bodies
                        )
                        messages_by_call[id(inner)] = (
                            messages_by_call.get(id(inner), frozenset()) | messages
                        )
                        aliases_by_call[id(inner)] = (
                            aliases_by_call.get(id(inner), frozenset()) | aliases
                        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_log_call(
            node, aliases_by_call.get(id(node), frozenset())
        ):
            continue
        scanned += 1
        bodies = bodies_by_call.get(id(node), frozenset())
        messages = messages_by_call.get(id(node), frozenset())
        # `.log(level, msg, ...)` carries the format string at index 1.
        msg_index = 1 if getattr(node.func, "attr", "") == "log" else 0
        first = node.args[msg_index] if len(node.args) > msg_index else None
        fmt = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else ""
        )
        arguments: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
        seen: set[str] = set()
        for argument in arguments:
            for _shape, leaf in [
                *_payload_leaves(argument),
                *_body_leaves(argument, bodies),
                *_whole_body_leaves(argument),
                *_expansion_leaves(argument),
                *_message_leaves(argument, messages),
            ]:
                if leaf in seen or leaf in rotating:
                    continue
                if _is_reviewed(reviewed, relative, leaf, fmt):
                    continue
                seen.add(leaf)
                offenders.append((relative, node.lineno, leaf, fmt))
    return offenders, scanned


def test_no_log_call_passes_raw_payload_bytes() -> None:
    offenders, scanned = scan()
    # Vacuum guard: the package carries well over 1400 log calls across all
    # levels; a scan that sees fewer did not walk the package (wrong root,
    # parse failure, renamed logger).
    assert scanned > 1400, (
        f"only {scanned} log calls scanned; the guard would be vacuous"
    )
    assert offenders == [], (
        "Log calls pass raw payload, token or key bytes (AGENTS.md section 5): "
        "log the length or a content class instead, or, if the value is an EID, "
        "name the leaf after it (`eid`, `eid_hex`, `truncated_eid_hex`); a reviewed "
        "rotating site goes into `_REVIEWED` as (path, leaf, format-string prefix):\n"
        + "\n".join(f"{path}:{line}: {leaf}" for path, line, leaf, _fmt in offenders)
    )


def test_shape_f_reports_every_unpacked_mapping() -> None:
    """Shape (F) names the mapping behind each `**` entry, wherever it sits.

    Positive fixture: the package is clean, so without this case a detector
    that returned nothing would keep `test_no_log_call_passes_raw_payload_bytes`
    green. The last snippet is the fixed record and must stay silent.
    """
    cases = {
        'LOG.debug("x: %s", {**gcm_data, "token": redacted})': ["gcm_data"],
        'LOG.debug("x: %s", repr({"inner": {**creds}}))': ["creds"],
        'LOG.debug("x: %s", {**a, "k": 1, **self.b})': ["a", "b"],
        'LOG.debug("x: %s", {"k": v})': [],
        'LOG.debug("x: fields=%s token=%s", sorted(gcm_data), redacted)': [],
    }
    for source, expected in cases.items():
        call = ast.parse(source).body[0].value
        assert isinstance(call, ast.Call)
        leaves = [
            leaf
            for argument in call.args
            for _shape, leaf in _expansion_leaves(argument)
        ]
        assert leaves == expected, source


def test_shape_d_follows_assignments_and_log_aliases() -> None:
    """Shape (D) taints through assignments; a bound log method is a log call.

    Positive fixture: `snippet` is bound from a helper that received the body,
    two hops after `content = await resp.read()`, in source order that puts
    the hop before the read (the fixpoint is order-independent). The log
    call goes through `log_fn`, bound from `_LOGGER.info`/`.warning`.
    """
    source = """
async def fetch(self, resp, first):
    snippet = _redact(_decode(content, 503))
    content = await resp.read()
    described = _describe_error_response(content, 503)
    size = len(content)
    log_fn = _LOGGER.info if first else _LOGGER.warning
    log_fn("a: %s", snippet)
    log_fn("b: %s %s", described, size)
    _LOGGER.error("c: %s", content)
    for line in content.splitlines():
        key, _, value = line.partition("=")
        code = value.strip().upper()
    _LOGGER.warning("d: %s %s", key, code)
    _LOGGER.warning("e: %s", _classify_error_code(value))
"""
    function = ast.parse(source).body[0]
    assert _body_names(function) == frozenset(
        {"content", "snippet", "line", "key", "_", "value", "code"}
    )
    assert _log_aliases(function) == frozenset({"log_fn"})
    offenders, scanned = _scan_tree(ast.parse(source), "fixture.py")
    assert scanned == 5
    assert [(line, leaf) for _p, line, leaf, _f in offenders] == [
        (8, "snippet"),
        (10, "content"),
        (14, "key"),
        (14, "code"),
    ]


def test_shape_g_reports_message_parameters_passed_whole() -> None:
    """Shape (G) names a message-typed parameter passed whole, in any wrapping.

    Positive fixture with the same purpose as the shape (F) case. The module
    imports `HeartbeatAck` from a `_pb2` module and types `p` with the alias.
    """
    source = """
from .proto.mcs_pb2 import HeartbeatAck, DataMessageStanza as Stanza

async def handle(self, msg: HeartbeatAck, p: MessageProto | None, raw: bytes):
    LOG.debug("a: %s", msg)
    LOG.debug("b: %s", str(p))
    LOG.debug("c: %s", f"got {msg!r}")
    LOG.debug("d: %s", self._msg_str(msg))
    LOG.debug("e: %s %s", type(msg).__name__, msg.stream_id)
    LOG.debug("f: %s", raw)
"""
    tree = ast.parse(source)
    types = _message_type_names(tree)
    assert {"HeartbeatAck", "Stanza", "MessageProto"} <= types
    function = tree.body[1]
    messages = _message_params(function, types)
    assert messages == frozenset({"msg", "p"})
    calls = [node.value for node in function.body]
    reported = [
        leaf
        for call in calls
        for argument in call.args[1:]
        for _shape, leaf in _message_leaves(argument, messages)
    ]
    assert reported == ["msg", "p", "msg"]


def test_each_reviewed_site_matches_exactly_one_log_call() -> None:
    """The exception list excuses one vetted call per entry, no more, no less.

    Fewer than one: the entry outlived the site it excuses. More than one: a
    log call was copied or moved and kept the vetted prefix, so raw bytes
    could be logged while this guard stays green.
    """
    offenders, _ = scan(reviewed=frozenset())
    matches = {
        site: [
            (path, line)
            for path, line, leaf, fmt in offenders
            if _is_reviewed(frozenset({site}), path, leaf, fmt)
        ]
        for site in _REVIEWED
    }
    wrong = {site: hits for site, hits in matches.items() if len(hits) != 1}
    assert wrong == {}, (
        "each reviewed site must match exactly one raw-bytes log call "
        f"(0 = stale entry, >1 = copied site): {wrong}"
    )
