# tests/test_guard_logging_payloads.py
"""Guard: no log call in the integration passes raw payload, token or key bytes.

`AGENTS.md` section 5 forbids raw API payloads, tokens and key material in
log records at every level. The value that leaks is not an identifier but a
piece of the wire: a hex dump of a server response, the first bytes of an
undecodable protobuf, the prefix of a shared secret. This guard watches that
class; `test_guard_logging_identifiers.py` watches class (c) identifiers and
the two do not overlap (address-like leaves there, payload-like leaves here).

Eight shapes are recognised inside a log call's arguments:

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
      `line`, `key` and `value`). Two more seeds: a call that names a
      library producer of a server mapping (`gpsoauth.exchange_token`,
      `perform_oauth`, also through a nested `_run` that calls one), and
      the text of a message of shape (G) (`device_str = str(device)`);
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
  (G) a protobuf message passed whole to the record: bare, under
      `str()`/`repr()` or inside an f-string. A message is a parameter
      annotated with a message type (a name imported from a `_pb2` module,
      the module itself as in `DeviceUpdate_pb2.DevicesList`, or the
      `MessageProto` alias), a name bound from a constructor (`Msg()`,
      `Msg.FromString(raw)`), a bare alias or subscript of a message, or
      the target of a `for` over a message field (`for device in
      getattr(device_list, "deviceMetadata", [])`). `%s` renders the text
      format with every server-supplied field value (`HeartbeatAck` carried
      three stream counters, a `DataMessageStanza` carries the push);
      `_msg_str` names the type and the fields instead.

The EID is allowed at any level, in full or truncated (`AGENTS.md`, class
(a)); the leaves that carry it are listed in `_ROTATING_LEAVES` and excused
by name. One further site logs the first four bytes of an FMDN advertisement
frame (`payload[:4].hex()` in the BLE scanner: frame byte plus three EID
bytes, rotating) and is excused by (path, leaf, format-string prefix), so the
exception covers that one call and not every `payload` line of the module.

Blind spot, stated on purpose: the guard sees only shapes (A) to (I). Shape
(E) knows an exact list of names: a whole payload under any other name,
or wrapped (`str(payload)`, `repr(payload)`, `payload.decode()`, an
f-string), is not reported. Shape (D) follows assignments inside one
function, not across calls: a helper that returns the body it was given
under a new name is trusted unless the name it is bound to is used in a log
call of the same function. Shape (F) sees the dict display in the call
itself: a copy bound first (`shown = {**gcm_data, ...}`) or built by
`dict(gcm_data, token=...)` and then logged is not followed. Shape (G) does
not follow a parameter without annotation, a message reached through an
attribute (`self._last_msg`), or a field bound by assignment
(`code = msg.error.code`, `pid = getattr(msg, "persistent_id")`): a field
is a value, not the message, and whether it is a sub-message is not
knowable from the name. Shape (H) treats an
exception constructor as a sink, because its message is logged by a
caller one hop later: `raise X(f"... {body}")` and the two-step form
`err = X(f"... {body}"); raise err` (any call whose name ends in `Error`,
`Exception` or `Failed`, or that is raised in the same function). Shape (I), Auth package only (its AGENTS.md keeps raw exception text
out of the message): inside a handler whose types are not all defined in
this package (`except Exception as exc`, bare `except`, `except
(OSError, ssl.SSLError) as err`), a log call that carries the exception by name, wrapped
(`_clip(exc)`, `str(exc)`, an f-string) or through an attribute other
than a code (`errno`, `error_kind`, `status`, `code`); `describe_exception`,
`type` and `isinstance` clear their own arguments. A traceback is a sink of
its own, because its last rendered line is `str(exc)` and a chained cause is
rendered too: under `Auth/` every `exc_info=` value other than `False` or
`None` (a name, `True`, an alias bound outside the handler, a tuple,
`sys.exc_info()`, an own exception raised `from` a foreign one) and every
`logger.exception(...)` is reported under the leaf `exc_info`, whatever the
handler; `exception_origin` names the frame without the text. `fcm_receiver_ha.py` is deferred with its count pinned. Not seen:
an alias (`detail = exc; log(detail)`), `except builtins.Exception`,
`traceback.format_exc()`, an own exception built from a foreign one,
and every module outside `Auth/` (119 such sites counted on 2026-09-17). Shape (D) reads plain and annotated assignments, tuple unpacking (starred
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
from collections.abc import Mapping
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
        "bool",
        "isinstance",
        "_describe_body",
        "_classify_body",
        "_classify_error_body",
        "_classify_error_code",
        "_describe_error_response",
        "classify_gpsoauth_error",
        # Type and key count of a gpsoauth response, never names or values.
        "_summarize_response",
        "_msg_str",
        "_unknown_field_numbers",
        # Protobuf descriptor access: field names, not values.
        "ListFields",
        "HasField",
        "DESCRIPTOR",
    }
)

# Library calls that return a server response as a mapping (shape (D) seed
# next to the HTTP body readers): the gpsoauth exchange functions. A nested
# function that calls one of them is a producer too, so
# `resp = await loop.run_in_executor(None, _run)` is a body binding.
_BODY_PRODUCERS = frozenset({"exchange_token", "perform_oauth", "perform_master_login"})

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


def _is_body_read(value: ast.AST, producers: frozenset[str] = frozenset()) -> bool:
    """A body read (`resp.text()`) or a call that names a body producer."""
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    if isinstance(value.func, ast.Attribute) and value.func.attr in _BODY_READERS:
        return True
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(value)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    return bool(names & (_BODY_PRODUCERS | producers))


def _producer_names(function: ast.AST) -> frozenset[str]:
    """Nested functions of `function` that call a body producer."""
    names: set[str] = set()
    for node in ast.walk(function):
        if node is function or not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if any(
            isinstance(inner, ast.Call) and _is_body_read(inner)
            for inner in ast.walk(node)
        ):
            names.add(node.name)
    return frozenset(names)


def _body_names(
    function: ast.AST, messages: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Names bound in `function` from a response-body read (shape (D)).

    Fixpoint over the function's assignments: a name bound from a body read,
    from a body producer, from the text of a message (`str(msg)`, `repr`, an
    f-string; shape (G) turned into text), or from any expression that
    references a body name outside a log-safe wrapper, is a body name. The
    order of assignments in the source does not matter; the loop runs until
    no name is added.
    """
    names: set[str] = set()
    producers = _producer_names(function)
    assignments = _assignments(function)
    changed = True
    while changed:
        changed = False
        for bound, value in assignments:
            if not (
                _is_body_read(value, producers)
                or _body_leaves(value, frozenset(names))
                or _message_text_leaves(value, messages)
            ):
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
        if isinstance(node, ast.IfExp):
            # Only the value branches reach the sink; the condition
            # (`x if body else y`) tests the body without exposing it.
            walk(node.body)
            walk(node.orelse)
            return
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
    """Protobuf message types a module can name.

    `from ..._pb2 import A, B` contributes `A` and `B`; `import x_pb2` and
    `from pkg import x_pb2` contribute the module name, so a qualified
    annotation `x_pb2.Msg` is a message type by its first segment.
    """
    names = set(_MESSAGE_ALIASES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is not None and node.module.endswith("_pb2"):
                names.update(alias.asname or alias.name for alias in node.names)
            else:
                names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name.endswith("_pb2")
                )
        elif isinstance(node, ast.Import):
            names.update(
                (alias.asname or alias.name).split(".")[-1]
                for alias in node.names
                if alias.name.endswith("_pb2")
            )
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
        if any(
            part.split(".")[-1] in types or part.split(".")[0] in types
            for part in parts
            if part
        ):
            names.add(arg.arg)
    return frozenset(names)


def _message_names(function: ast.AST, types: frozenset[str]) -> frozenset[str]:
    """Message-typed names in `function`: parameters (shape (G)) and bindings.

    Fixpoint over the function's bindings: a name bound from a message
    constructor (`Msg()`, `Msg.FromString(raw)`), as a bare alias or subscript
    of a message name (`m = msg`, `first = items[0]`), or as the target of a
    `for` over a message-rooted chain or `getattr(message, ...)` (a repeated
    field yields sub-messages) is a message name too. An attribute or
    `getattr` bound by assignment is a field value, not a message
    (`code = msg.error.code`), and is not followed.
    """
    names: set[str] = set(_message_params(function, types))
    loop_bound = {
        name
        for node in ast.walk(function)
        if isinstance(node, (ast.For, ast.AsyncFor))
        for name in _names_in_target(node.target)
    }
    changed = True
    while changed:
        changed = False
        for bound, value in _assignments(function):
            is_loop = bool(set(bound) & loop_bound)
            if not (
                _is_message_constructor(value, types)
                or _is_message_derivation(value, frozenset(names), is_loop)
            ):
                continue
            for name in bound:
                if name not in names:
                    names.add(name)
                    changed = True
    return frozenset(names)


def _is_message_derivation(
    value: ast.AST, messages: frozenset[str], is_loop: bool
) -> bool:
    """`msg`, `items[0]`, or (as a loop iterable) `msg.field`/`getattr(msg, …)`."""
    if isinstance(value, ast.Await):
        value = value.value
    if isinstance(value, ast.Call):
        func = value.func
        if not (isinstance(func, ast.Name) and func.id == "getattr" and value.args):
            return False
        return is_loop and _root(value.args[0]) in messages
    if isinstance(value, ast.Name):
        return value.id in messages
    if isinstance(value, ast.Subscript) and not isinstance(value.slice, ast.Slice):
        return _root(value.value) in messages
    if isinstance(value, ast.Attribute):
        return is_loop and _root(value) in messages
    return False


def _root(node: ast.AST) -> str | None:
    """The name a chain hangs on (`device_list` for `device_list.meta[0]`)."""
    chain = _chain(node)
    return chain[-1] if chain else None


def _is_message_constructor(value: ast.AST, types: frozenset[str]) -> bool:
    """`Msg()` or `Msg.FromString(...)` for a message type of the module."""
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return False
    return any(name in types for name in _chain(value.func))


def _message_text_leaves(
    value: ast.AST, messages: frozenset[str]
) -> list[tuple[str, str]]:
    """Text conversions of a message anywhere inside `value`.

    `str(msg)`, `repr(msg)` or an f-string interpolating it; a bare reference
    (`helper(msg)`, `msg.field`) is not text and is not reported here.
    """
    found: list[tuple[str, str]] = []
    for node in ast.walk(value):
        if isinstance(node, (ast.Call, ast.JoinedStr)):
            found.extend(_message_leaves(node, messages))
    return found


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


_EXCEPTION_SUFFIXES = ("Error", "Exception", "Failed")

# Shape (I): a producer exception logged by name inside a broad handler.
# Scope is the Auth package, whose AGENTS.md keeps raw exception text out of
# the message; `fcm_receiver_ha.py` is deferred with its count pinned below
# (aiohttp producer, own follow-up), not silently excused.
_SHAPE_I_ROOT = "Auth/"
_SHAPE_I_DEFERRED: dict[str, int] = {"Auth/fcm_receiver_ha.py": 40}
_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})
# Wrappers that reduce an exception to a type, a kind or a count.
_SAFE_EXCEPTION_WRAPPERS = frozenset(
    {"describe_exception", "exception_origin", "type", "isinstance"}
)
# Attributes of an exception that are a code, not its text.
_SAFE_EXCEPTION_ATTRS = frozenset(
    {"errno", "error_kind", "status", "code", "__class__"}
)


def _own_type_names(tree: ast.AST) -> frozenset[str]:
    """Exception types this module defines or imports from this package."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom) and (
            node.level >= 1
            or (node.module or "").startswith("custom_components.googlefindmy")
        ):
            names.update(alias.asname or alias.name for alias in node.names)
    return frozenset(names)


def _is_foreign_handler(handler: ast.ExceptHandler, own: frozenset[str]) -> bool:
    """`except:`, a broad type, or any type not defined in this package.

    Fail-closed: a builtin (`ValueError`) or a library type (`ssl.SSLError`,
    `ECEException`) carries text this package did not write.
    """
    kind = handler.type
    if kind is None:
        return True
    types = list(kind.elts) if isinstance(kind, ast.Tuple) else [kind]
    for node in types:
        chain = _chain(node)
        name = chain[0] if chain else ""
        if name in _BROAD_EXCEPTIONS or name not in own:
            return True
    return False


def _exception_names_by_call(tree: ast.AST) -> dict[int, str]:
    """Map each call inside a foreign `except` to its `as` name, "" if none (innermost wins)."""
    names: dict[int, str] = {}
    own = _own_type_names(tree)
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        if not _is_foreign_handler(handler, own):
            continue
        # A handler without `as NAME` maps to "": still foreign, so an
        # implicit traceback (`logger.exception`, `exc_info=True`) is a sink.
        for inner in ast.walk(handler):
            if isinstance(inner, ast.Call):
                names[id(inner)] = handler.name or ""
    return names


def _exception_leaves(argument: ast.AST, name: str) -> list[tuple[str, str]]:
    """Shape (I): NAME, `str(NAME)`, `_clip(NAME)`, `f"{NAME}"`, `NAME.args`.

    Not reported: NAME inside a safe wrapper (`describe_exception(NAME)`,
    `type(NAME).__name__`, `isinstance(NAME, X)`), a code attribute
    (`NAME.errno`), and the keyword `exc_info` (the contract allows a
    traceback; that it carries the same text is a contract finding, not a
    guard one). The wrapper clears only its own arguments: in
    `f"{describe_exception(exc)} raw={exc}"` the second `exc` is reported.
    """
    cleared: set[int] = set()
    for node in ast.walk(argument):
        if isinstance(node, ast.Call):
            chain = _chain(node.func)
            if chain and chain[0] in _SAFE_EXCEPTION_WRAPPERS:
                for inner in node.args:
                    cleared.update(id(n) for n in ast.walk(inner))
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == name
            and node.attr in _SAFE_EXCEPTION_ATTRS
        ):
            cleared.add(id(node.value))
    for node in ast.walk(argument):
        if isinstance(node, ast.Name) and node.id == name and id(node) not in cleared:
            return [("I", name)]
    return []


def _is_exception_constructor(
    value: ast.AST, targets: list[ast.expr], raised_names: set[str]
) -> bool:
    """`X(...)` bound to a name that is raised later or is named like one."""
    if not isinstance(value, ast.Call):
        return False
    chain = _chain(value.func)
    if not chain:
        return False
    if chain[0].endswith(_EXCEPTION_SUFFIXES):
        return True
    return any(
        isinstance(target, ast.Name) and target.id in raised_names for target in targets
    )


def _scan_tree(
    tree: ast.AST,
    relative: str,
    reviewed: frozenset[tuple[str, str, str]] = frozenset(),
    rotating: frozenset[str] = frozenset(),
    deferred: Mapping[str, int] = _SHAPE_I_DEFERRED,
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
            messages = _message_names(function, message_types)
            bodies = _body_names(function, messages)
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
    in_scope_for_i = relative.startswith(_SHAPE_I_ROOT) and relative not in deferred
    exception_names = _exception_names_by_call(tree) if in_scope_for_i else {}
    raised_names = {
        node.exc.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            # Shape (H): an exception message is a log sink one hop later
            # (`_LOGGER.error("...: %s", err)` is the usual consumer), so the
            # constructor arguments are scanned like log arguments. The
            # format string is the marker `raise` so a reviewed pin can
            # single the site out.
            call = node.exc
            fmt = "raise"
        elif isinstance(node, ast.Assign) and _is_exception_constructor(
            node.value, node.targets, raised_names
        ):
            # Shape (H), two-step form: `err = X(...)` followed by
            # `raise err` (or a name that says it is an exception).
            call = node.value
            fmt = "raise"
        elif isinstance(node, ast.Call) and _is_log_call(
            node, aliases_by_call.get(id(node), frozenset())
        ):
            call = node
            # `.log(level, msg, ...)` carries the format string at index 1.
            msg_index = 1 if getattr(node.func, "attr", "") == "log" else 0
            first = node.args[msg_index] if len(node.args) > msg_index else None
            fmt = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else ""
            )
        else:
            continue
        scanned += 1
        bodies = bodies_by_call.get(id(call), frozenset())
        messages = messages_by_call.get(id(call), frozenset())
        exception_name = exception_names.get(id(call), "") if fmt != "raise" else ""
        arguments: list[ast.AST] = [*call.args, *(kw.value for kw in call.keywords)]
        # Shape (I) treats a traceback as a sink of its own: the last line of
        # a rendered traceback is `str(exc)`. `exc_info=<name>`, `exc_info=True`
        # and the implicit form `logger.exception(...)` inside a foreign
        # handler are reported under the leaf `exc_info`; `exc_info=False`
        # and `exc_info=None` are not. Shapes (A) to (H) scan the keyword's
        # value like any other argument.
        traceback_args = {id(kw.value) for kw in call.keywords if kw.arg == "exc_info"}
        if (
            in_scope_for_i
            and fmt != "raise"
            and not _is_reviewed(reviewed, relative, "exc_info", fmt)
        ):
            # Any handler, any value: an alias bound outside the handler
            # (`last_error = exc`), a tuple, `sys.exc_info()`, `1`, or an own
            # exception raised `from` a foreign one (the chained cause is
            # rendered too) all carry the producer's text; only `False` and
            # `None` do not. The rule is therefore positional, not semantic.
            implicit = getattr(call.func, "attr", "") == "exception"
            explicit = any(
                kw.arg == "exc_info"
                and not (
                    isinstance(kw.value, ast.Constant)
                    and kw.value.value in (False, None)
                )
                for kw in call.keywords
            )
            if implicit or explicit:
                offenders.append((relative, node.lineno, "exc_info", fmt))
        seen: set[str] = set()
        for argument in arguments:
            for _shape, leaf in [
                *_payload_leaves(argument),
                *_body_leaves(argument, bodies),
                *_whole_body_leaves(argument),
                *_expansion_leaves(argument),
                *_message_leaves(argument, messages),
                *(
                    _exception_leaves(argument, exception_name)
                    if exception_name and id(argument) not in traceback_args
                    else []
                ),
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


def test_message_taint_reaches_text_and_producers_seed_bodies() -> None:
    """Local message bindings, their text, and library producers are followed.

    (G) taints through a constructor, an alias, a subscript and a `for` over
    a message field; `str(device)` turns that into a body (D); a nested
    function that calls a gpsoauth producer makes `resp` a body, and the
    error value derived from it stays a body through `.get()` and `[:32]`.
    """
    source = """
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

async def decode(self, device_list: DeviceUpdate_pb2.DevicesList, raw):
    update = DeviceUpdate_pb2.DeviceUpdate.FromString(raw)
    alias = update
    for device in getattr(device_list, "deviceMetadata", []):
        names = [f.name for f, _ in device.ListFields()]
        device_str = str(device)
        unknown = [line for line in device_str.splitlines() if line[:1].isdigit()]
        _LOGGER.debug("a: %s %s", names, unknown)
        _LOGGER.debug("b: %s", _unknown_field_numbers(device))
    first = device_list.deviceMetadata[0]
    _LOGGER.debug("c: %s", first)
    code = alias.error.code
    _LOGGER.debug("d: %s", code)

async def exchange(self, username):
    def _run():
        return _gpsoauth().exchange_token(username)
    resp = await loop.run_in_executor(None, _run)
    error_value = resp.get("Error", "")
    kind = str(error_value)[:32]
    _LOGGER.warning("e: %s", kind, extra={"kind": kind, "n": len(resp)})
    _LOGGER.warning("f: %s", classify_gpsoauth_error(error_value))
"""
    tree = ast.parse(source)
    types = _message_type_names(tree)
    assert "DeviceUpdate_pb2" in types
    assert "pb" in _message_type_names(ast.parse("import pkg.sub.update_pb2 as pb"))
    assert "update_pb2" in _message_type_names(ast.parse("import pkg.update_pb2"))
    decode, exchange = tree.body[1], tree.body[2]
    assert _message_names(decode, types) == frozenset(
        {"device_list", "update", "alias", "device", "first"}
    )
    # `line` is a comprehension variable, not a binding the guard follows.
    assert _body_names(decode, _message_names(decode, types)) == frozenset(
        {"device_str", "unknown"}
    )
    assert _producer_names(exchange) == frozenset({"_run"})
    assert _body_names(exchange) == frozenset({"resp", "error_value", "kind"})
    offenders, scanned = _scan_tree(tree, "fixture.py")
    assert scanned == 6
    # Line numbers count from the leading newline of the fixture; the walk is
    # breadth-first, so the list is sorted before comparing.
    assert sorted((line, leaf) for _p, line, leaf, _f in offenders) == [
        (11, "unknown"),
        (14, "first"),
        (24, "kind"),
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


def test_shape_h_reports_bodies_in_exception_messages() -> None:
    """Shape (H) scans exception constructors: direct and two-step raise.

    Fixture in the shape of the two gpsoauth sites this shape was added for
    (`token_retrieval.py:231`, `aas_token_retrieval.py:360`): the body is a
    producer result, the message is built with the body's field and raised
    directly, or bound first and raised two lines later. Described values
    pass.
    """
    source = """
def exchange(username):
    resp = gpsoauth.exchange_token(username)
    detail = str(resp.get("Error", "")).strip()
    if detail:
        raise InvalidAasTokenError(f"rejected: {detail}")
    new_err = RuntimeError(f"invalid: {resp}")
    raise new_err
    err = make_error(f"kept: {detail}")
    raise err
    LOG.warning("safe: %s", classify_gpsoauth_error(detail))
    raise RuntimeError(f"safe: {len(resp)} keys")
"""
    tree = ast.parse(source)
    offenders, scanned = _scan_tree(tree, "fixture.py")
    # Five sinks: the direct raise, `new_err = RuntimeError(...)`,
    # `err = make_error(...)` (raised later, so a constructor by use), the
    # warning and the described raise. `raise new_err` is a name, not a
    # call, and is not a sink of its own.
    assert scanned == 5
    assert sorted((line, leaf, fmt) for _p, line, leaf, fmt in offenders) == [
        (6, "detail", "raise"),
        (7, "resp", "raise"),
        (9, "detail", "raise"),
    ]


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


_SHAPE_I_FIXTURE = """
def exchange(user):
    try:
        return call()
    except Exception as exc:
        _LOGGER.error("failed: %s", exc)
        _LOGGER.error("failed: %s", _clip(exc))
        _LOGGER.error("failed: %s", str(exc))
        _LOGGER.error(f"failed: {exc}")
        _LOGGER.error("failed: %s", exc.args)
        _LOGGER.error("failed: %s", describe_exception(exc))
        _LOGGER.error("failed: %s", type(exc).__name__)
        _LOGGER.error("failed: %s", exc.errno)
        _LOGGER.error("failed", exc_info=exc)
        _LOGGER.error(f"{describe_exception(exc)} raw={exc}")
    except ValueError as builtin_type:
        _LOGGER.error("failed: %s", builtin_type)
    except OwnError as own_type:
        _LOGGER.error("failed: %s", own_type, exc_info=own_type)


def unnamed():
    last_error = None
    try:
        return call()
    except Exception as exc:
        last_error = exc
    _LOGGER.exception("failed")
    _LOGGER.error("failed", exc_info=True)
    _LOGGER.error("failed", exc_info=False)
    _LOGGER.error("failed", exc_info=None)
    _LOGGER.error("failed", exc_info=last_error)
    _LOGGER.error("failed", exc_info=(type(last_error), last_error, None))


class OwnError(Exception):
    pass
"""


def test_shape_i_reports_producer_exceptions_in_broad_auth_handlers() -> None:
    """Shape (I): seven sinks carry a foreign exception, four are safe.

    A wrapper clears only its own argument (line 15), a builtin type is
    foreign (line 17), a type defined in the module is not (line 19, but its
    `exc_info` is: an own exception raised `from` a foreign one renders the
    cause). A traceback is a sink wherever it is attached: `exc_info=exc`
    (line 14), `logger.exception` (line 28), `exc_info=True` (29), an alias
    bound in the handler and logged outside it (32), a tuple (33);
    `exc_info=False` (30) and `exc_info=None` (31) are not. The same fixture
    outside `Auth/` and in the deferred file reports nothing, so the scope is
    measured and not assumed.
    """
    tree = ast.parse(_SHAPE_I_FIXTURE)
    offenders, _ = _scan_tree(tree, "Auth/fixture.py")
    by_line = sorted((line, leaf) for _, line, leaf, _ in offenders)
    assert by_line == [
        (6, "exc"),
        (7, "exc"),
        (8, "exc"),
        (9, "exc"),
        (10, "exc"),
        (14, "exc_info"),
        (15, "exc"),
        (17, "builtin_type"),
        (19, "exc_info"),
        (28, "exc_info"),
        (29, "exc_info"),
        (32, "exc_info"),
        (33, "exc_info"),
    ]
    outside, _ = _scan_tree(tree, "coordinator/fixture.py")
    assert outside == []
    deferred, _ = _scan_tree(tree, "Auth/fcm_receiver_ha.py")
    assert deferred == []


def test_exc_info_is_skipped_by_shape_i_only() -> None:
    """A payload passed as `exc_info=` is a shape-(D) finding, not only a traceback one."""
    source = """
def read(response):
    body = response.read()
    try:
        return parse(body)
    except Exception as exc:
        _LOGGER.error("failed", exc_info=body)
"""
    offenders, _ = _scan_tree(ast.parse(source), "Auth/fixture.py")
    assert sorted((line, leaf) for _, line, leaf, _ in offenders) == [
        (7, "body"),
        (7, "exc_info"),
    ]


def test_shape_i_deferred_file_count_is_pinned() -> None:
    """The deferred file carries exactly the pinned number of shape-(I) sites.

    A drop means sites were fixed and the pin (or the deferral) is stale; a
    rise means a new producer-text sink slipped in under the deferral.
    """
    for relative, expected in _SHAPE_I_DEFERRED.items():
        path = _PACKAGE_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders, _ = _scan_tree(tree, relative, deferred={})
        assert len(offenders) == expected, (relative, len(offenders), expected)
