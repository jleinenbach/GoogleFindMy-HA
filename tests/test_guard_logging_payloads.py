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
than a code (`errno`, `error_kind`, `status`, `status_code`, `code`,
`__class__`); `describe_exception`,
`type` and `isinstance` clear their own arguments. A traceback is a sink of
its own, because its last rendered line is `str(exc)` and a chained cause is
rendered too: under `Auth/` every `exc_info=` value other than `False` or
`None` (a name, `True`, an alias bound outside the handler, a tuple,
`sys.exc_info()`, an own exception raised `from` a foreign one) and every
`logger.exception(...)` is reported under the leaf `exc_info`, whatever the
handler; `exception_origin` names the frame without the text. A parameter
annotated with a broad or foreign exception type (`err: BaseException`,
`err: ClientError`, `err: ssl.SSLError | None`, `*errors: BaseException`, an import alias
resolved to its suffix) binds the name for the whole function, and so does a name
assigned, annotated or not, from `task.exception()` (the done callback of
`_track_task`), so a callee that
logs a caught exception it did not catch itself is a sink too (found in
`fcm_receiver_ha._classify_registration_exception`, 2026-09-18), and so does
a name the function derives from one of those by data flow inside the
same function, followed to a fixpoint: an alias, attribute or subscript
target (`detail = exc`, `self.last = err`, `self.errs["k"] = err`), its
text or a derivation of it (`shown = str(err)`, `msg += str(err)`, `"x " +
str(err)`, `"x %s" % err`, `str(err) or ""`, `str(err).lower()`, an
f-string, `", ".join(...)`, `err.args`, `err.strerror`, `getattr(err,
"msg")`, a conditional, a container display or a built-in that rearranges
one (`list(errors)`, `map(str, errors)`), `errors[0]`, a comprehension, a
container mutated by `append`/`extend`/`add`/`update`), tuple and list
unpacking matched by position (`err, count = t.exception(), 0` binds `err`
only, starred targets included) or, from a carrying value that is no
display, as a whole (`code, text = err.args`), a `for`,
`async for` or comprehension target whose iterable mentions a bound name
(`for err in errors` with `errors: list[ClientError]`, `enumerate(errors)`,
`errors.items()`, which binds the key too) and a `match` capture (`case
ClientError() as err`, `case ClientError(args=[first])`, or any capture
when the subject mentions a bound name); a handler name seeds the same
flow inside its handler (`except OSError as exc: text = str(exc)` binds
`text` there); `sys.exc_info()` is read like `task.exception()`, bound
or passed directly. Any call named `exception()` or `exc_info()` is
read as the task method or `sys.exc_info()`, `_LOGGER.exception(...)`
included (fail-closed).
A module bound by `from` (`from cryptography import exceptions as ce`)
resolves `ce.InvalidTag` like `import` does. Not seen: `except
builtins.Exception`, a helper's return value (`exc = _pick(task)`, `kind =
_classify(err)`: a call's result is not knowable from the name), a lambda
bound to a name and called (`g = lambda: str(err); log(g())`), a local
annotation with a foreign type whose value is a helper's result (`last:
str | ClientError | None = _pick(task)`) and a handler name aliased out
of its handler (`except Exception as exc: last_error = exc`): the package
narrows such a name before each of its three log calls (`fcmregister.py`:
one by `isinstance`, two by a `str` assignment in the same branch) and a
flow-insensitive binding would report all three,
`getattr(t, "exception")()`, a lambda default (`lambda e=err:`), a binding
in one method read in another (`self.last = err` in `m`, logged in `n`:
the flow stays inside one function), `with cm as err` (what `__enter__`
returns is the manager's business, not the name's),
a value forwarded to a helper whose parameter is unannotated
(`describe(err)`, `describe(cause=err)`: a parameter binds through its
annotation only, the caller is not followed into the callee), an alias
resolved to a symbol without a suffix in a module that cannot be imported
(`from mylib import Result as ClientError`: the symbol's name decides, not
the alias's, and `mylib` cannot be asked),
a local type alias used as an annotation (`type TransportFailure =
ClientError` or `Failure: TypeAlias = ClientError`, then `err:
TransportFailure`: the alias name is neither imported nor suffixed, and
resolving it needs a pass that collects the module's alias definitions
before the annotations are classified; deferred to a follow-up PR,
measured on 2026-09-20: four PEP 695 aliases under `Auth/` and seven in
the package, all of them `dict`, `Mapping`, `ConfigEntry`, `Coroutine`
or `Any` types, none an exception), a module alias whose name ends
in a suffix (`import errorlib as Error`, resolved to the module, a module is
not a type), a `TypeVar` bound to an exception, `traceback.format_exc()`,
an own exception built from a foreign one, and every module outside `Auth/`
(119 such sites counted on 2026-09-17). Reported although harmless, on purpose:
a rebinding to a harmless value (`err = "safe"; log(err)`), a
nested function whose own parameter shadows a bound name (`def inner(err:
str)`), a number or truth value from a method on the text or the container
(`errors.count(x)`, `str(err).startswith("auth")`) and the index of
`enumerate` or `range` stay reported; the guard is fail-closed there, a
false report costs one review line, and none has an instance today. Shape (D) reads plain and annotated assignments, tuple unpacking (starred
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
import builtins
import importlib
import re
import types
from pathlib import Path

import pytest

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

# Reviewed sites that log a value the guard reads as a payload or as
# exception text: (path relative to the package, base leaf, format-string
# prefix). `payload[:4].hex()` in the BLE scanner is the FMDN frame byte plus
# the first three bytes of the EID of an advertisement that resolved to one
# of the user's own trackers; it rotates with the EID (`docs/FMDN.md`).
# `missing_key` in `fcm_refresh_install_token` is `KeyError.args[0]` from
# four literal lookups on the package's own credentials dict (`"fcm"`,
# `"installation"`, `"refresh_token"`, `"fid"`): the key the code asked
# for, not text a producer wrote (shape (I), 2026-09-18). The prefix keys
# the exception to that one call; `test_each_reviewed_site_matches_
# exactly_one_log_call` fails when the site disappears or is copied.
_REVIEWED: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("fmdn_finder/ble_scanner.py", "payload", "BLE scan: resolved"),
        (
            "Auth/firebase_messaging/fcmregister.py",
            "missing_key",
            "Cannot refresh FCM token: missing credentials key",
        ),
    }
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
    for arg in [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        *([args.vararg] if args.vararg else []),
        *([args.kwarg] if args.kwarg else []),
    ]:
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
# Every built-in `BaseException` subclass, including the ones without a
# conventional suffix (`ExceptionGroup`, `SystemExit`, `KeyboardInterrupt`,
# `StopIteration`, `GeneratorExit`, the warnings); read from the interpreter
# instead of a hand-kept list, so a new built-in is covered on upgrade.
_BUILTIN_EXCEPTIONS = frozenset(
    name
    for name, obj in vars(builtins).items()
    if isinstance(obj, type) and issubclass(obj, BaseException)
)

# Shape (I): a producer exception logged by name inside a broad handler.
# Scope is the whole Auth package, whose AGENTS.md keeps raw exception text
# out of the message; `fcm_receiver_ha.py` joined the scope on 2026-09-18
# (its 40 sites were rewritten, no file is excused).
_SHAPE_I_ROOT = "Auth/"
_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})
# Wrappers that reduce an exception to a type, a kind or a count.
_SAFE_EXCEPTION_WRAPPERS = frozenset(
    {"describe_exception", "exception_origin", "type", "isinstance"}
)
# Calls whose result renders the exception text: binding their result binds
# the name (`shown = str(err)`, `text = ", ".join(str(e) for e in errors)`);
# `_clip` is the package's own truncation.
_TEXT_WRAPPERS = frozenset(
    {
        "str",
        "repr",
        "ascii",
        "format",
        "join",
        "_clip",
        "format_exception",
        "format_exception_only",
    }
)
# Built-ins and stdlib containers that hand their arguments back, rearranged:
# `list(errors)`, `next(iter(errors))`, `map(str, errors)`, `dict(err=err)`,
# `dict.fromkeys(keys, err)`, `deque([err])`; `len()` is not one.
_TRANSPARENT_CALLS = frozenset(
    {
        "list",
        "tuple",
        "set",
        "frozenset",
        "sorted",
        "reversed",
        "iter",
        "next",
        "map",
        "filter",
        "zip",
        "enumerate",
        "dict",
        "min",
        "max",
        "vars",
        "fromkeys",
        "deque",
        "OrderedDict",
        "Counter",
        "defaultdict",
        "ChainMap",
    }
)
# Methods that put an argument into their receiver (`parts.append(str(err))`).
_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "add",
        "update",
        "setdefault",
        "appendleft",
        "__setitem__",
        "__iadd__",
        "__ior__",
    }
)
# Methods whose default argument comes back as the result (`d.get(k, err)`).
_DEFAULT_METHODS = frozenset({"get", "pop", "setdefault"})

# Attributes of an exception that are a code, not its text.
_SAFE_EXCEPTION_ATTRS = frozenset(
    {"errno", "error_kind", "status", "status_code", "code", "__class__"}
)
# `status_code`: `fcm_receiver_ha._raise_if_fatal_client_error` reads
# `getattr(err, "status_code", None)` and logs the number (2026-09-18).


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


def _imported_exception_types(tree: ast.AST) -> frozenset[str]:
    """Local names bound by `from x import Y [as Z]` that resolve to exception classes.

    The naming convention (`Error`/`Exception`/`Failed`) and the built-in
    list do not cover a third-party type such as
    `cryptography.exceptions.InvalidTag` (imported in `fcm_receiver_ha.py`);
    the module is importable in the test environment, so the class itself
    is asked. A module that cannot be imported, or a name that is not a
    class, contributes nothing and the convention decides (fail-open here is
    bounded: the convention still applies).
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        if node.module.startswith("custom_components.googlefindmy"):
            continue
        try:
            module = importlib.import_module(node.module)
        except Exception:  # noqa: BLE001 - optional or absent dependency
            continue
        for alias in node.names:
            try:
                obj = getattr(module, alias.name, None)
            except Exception:  # noqa: BLE001 - a module `__getattr__` may raise anything
                continue
            if isinstance(obj, type) and issubclass(obj, BaseException):
                names.add(alias.asname or alias.name)
    return frozenset(names)


def _module_bindings(tree: ast.AST) -> dict[str, str]:
    """Local name -> module path for `import a.b` (`a`), `import a.b as c` (`c`)
    and `from a import b [as c]` when `b` is a module (`c` or `b`).

    `from cryptography import exceptions as ce` binds a module, not a class:
    `_imported_exception_types` skips it (the object is no type), so
    `err: ce.InvalidTag` has to be resolved through this table. A `from`
    import whose module cannot be imported, or whose name is not a module,
    contributes nothing (the naming convention decides, as everywhere else).
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    bindings[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and node.module
            and not node.module.startswith("custom_components.googlefindmy")
        ):
            try:
                parent = importlib.import_module(node.module)
            except Exception:  # noqa: BLE001 - optional or absent dependency
                continue
            for alias in node.names:
                try:
                    member = getattr(parent, alias.name, None)
                except Exception:  # noqa: BLE001 - a module `__getattr__` may raise anything
                    continue
                if isinstance(member, types.ModuleType):
                    bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _is_qualified_exception(annotation: ast.AST, modules: dict[str, str]) -> bool:
    """`err: cryptography.exceptions.InvalidTag` or `err: ce.InvalidTag` resolved as a class.

    The first segment is looked up in the `import` table, the module is
    imported and the remaining segments are walked with `getattr`; the
    result must be a `BaseException` subclass. Anything that cannot be
    imported or resolved falls back to the naming convention.
    """
    if not isinstance(annotation, ast.Attribute):
        return False
    dotted = ast.unparse(annotation).split(".")
    head = modules.get(dotted[0])
    if head is None:
        return False
    parts = head.split(".") + dotted[1:]
    obj: object = None
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except Exception:  # noqa: BLE001 - not a module at this depth
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr, None)
            if obj is None:
                return False
        break
    return isinstance(obj, type) and issubclass(obj, BaseException)


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> imported name for `from x import Y as Z` and `import x.Y as Z`.

    `from aiohttp import ClientError as TransportFailure` hides the suffix
    behind the alias; the annotation check resolves the alias first.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name.rsplit(".", 1)[-1]
    return aliases


def _is_foreign_exception_annotation(
    annotation: ast.AST | None,
    own: frozenset[str],
    aliases: dict[str, str] | None = None,
    imported: frozenset[str] = frozenset(),
    modules: dict[str, str] | None = None,
) -> bool:
    """A parameter annotated with a broad or foreign exception type.

    `err: BaseException`, `err: ClientError`, `err: ssl.SSLError | None`,
    `err: Optional[OSError]`: the callee logs a producer's exception it did
    not catch itself, so the name is bound outside any `except` and the
    handler walk alone would not see it (found in
    `fcm_receiver_ha._classify_registration_exception`). An own type
    (`err: FatalRegistrationError`) is not foreign; `type[X]` and
    `Callable[..., X]` carry a class, not an instance.
    """
    members = _annotation_members(annotation, aliases)
    for member in members:
        if _is_qualified_exception(member, modules or {}):
            return True
        chain = _chain(member)
        # `_chain` lists the attribute first (`ssl.SSLError` ->
        # ["SSLError", "ssl"]), the same reading `_is_foreign_handler` uses.
        name = chain[0] if chain else ""
        if name in own:
            continue
        if name in imported:
            return True
        resolved = (aliases or {}).get(name, name)
        if (
            resolved in _BROAD_EXCEPTIONS
            or resolved in _BUILTIN_EXCEPTIONS
            or resolved.endswith(_EXCEPTION_SUFFIXES)
        ):
            return True
    return False


# Generic heads whose members are instances: a union, or a container whose
# `%s` renders every element with its text; a mapping renders keys and
# values (`{OSError('text'): 1}`), so both slots count, and so do the
# stdlib containers (`defaultdict[str, OSError]`, `deque[OSError]`,
# `Counter[OSError]`; Codex review on 714596f), a mapping view, a mapping
# proxy, an `asyncio` future, task or queue (`<Future finished
# result=OSError('text')>`, `<Queue maxsize=0 _queue=[OSError('text')]>`,
# `LifoQueue` and `PriorityQueue` inherit that rendering),
# `Container` and `Reversible` (a list satisfies both). `Annotated[X, ...]`
# carries `X` in its first slot. Not carriers, because their instances do
# not render their members: `Awaitable`, `Coroutine`, `Generator`,
# `AsyncIterator`, `weakref.ref`, `AbstractContextManager`. Any other head
# (`ExceptionGroup[OSError]`) is checked as a name itself; `type[X]` and
# `Callable[..., X]` fall through that check because neither head is an
# exception. `test_member_carriers_expose_their_members` pins every entry.
_MEMBER_CARRIERS = frozenset(
    {
        "Optional",
        "Union",
        "list",
        "tuple",
        "set",
        "frozenset",
        "List",
        "Tuple",
        "Set",
        "FrozenSet",
        "Sequence",
        "Iterable",
        "Collection",
        "Iterator",
        "dict",
        "Dict",
        "Mapping",
        "MutableMapping",
        "MutableSequence",
        "MutableSet",
        "AbstractSet",
        "KeysView",
        "ValuesView",
        "ItemsView",
        "defaultdict",
        "DefaultDict",
        "deque",
        "Deque",
        "OrderedDict",
        "Counter",
        "ChainMap",
        "MappingProxyType",
        "Future",
        "Task",
        "Queue",
        "LifoQueue",
        "PriorityQueue",
        "Container",
        "Reversible",
    }
)


def _annotation_members(
    annotation: ast.AST | None, aliases: dict[str, str] | None = None
) -> list[ast.AST]:
    """Flatten unions, `Optional[A]`, `Union[A, B]`, containers and `"A | B"`.

    A carrier head is resolved through the import-alias table first
    (`from typing import Optional as Maybe`, `Maybe[ClientError]`).
    """
    if annotation is None:
        return []
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return []
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_members(annotation.left, aliases) + _annotation_members(
            annotation.right, aliases
        )
    if isinstance(annotation, ast.Subscript):
        head = _chain(annotation.value)
        head_name = (aliases or {}).get(head[0], head[0]) if head else ""
        inner = (
            list(annotation.slice.elts)
            if isinstance(annotation.slice, ast.Tuple)
            else [annotation.slice]
        )
        if head_name == "Annotated":
            inner = inner[:1]
        elif head_name not in _MEMBER_CARRIERS:
            return [annotation.value]
        return [m for item in inner for m in _annotation_members(item, aliases)]
    return [annotation]


def _target_names(target: ast.AST) -> tuple[str, ...]:
    """Every name a binding target writes: `a`, `self.last`, `(a, *b)`, `d["k"]` and `d`."""
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Attribute):
        return (ast.unparse(target),)
    if isinstance(target, ast.Subscript):
        # An element write binds the container too (`d["k"] = err` renders
        # `d`), whether the target stands alone, in a tuple or under `+=`.
        return (ast.unparse(target), *_container_names(target.value))
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(n for elt in target.elts for n in _target_names(elt))
    return ()


def _mentions_exception(expr: ast.AST, bound: frozenset[str]) -> bool:
    """A bound name occurs in the expression outside a safe wrapper or code attribute."""
    return any(_exception_leaves(expr, name) for name in bound)


def _carries_exception(value: ast.AST, bound: frozenset[str]) -> bool:
    """The value renders a bound exception or `<task>.exception()` when logged.

    Followed: the name itself or its attribute other than a code
    (`err.args`, `err.strerror`, `getattr(err, "msg")`), a text wrapper
    (`str(err)`, `f"{err}"`, `"x %s" % err`, `"x " + str(err)`,
    `", ".join(...)`), `sys.exc_info()`, the default slot of `d.get(k,
    err)`, a built-in that hands its arguments back
    (`list(errors)`, `map(str, errors)`, `next(iter(errors))`), the
    receiver of any method (`str(err).strip()`, `errors.pop()`), `or`/`and`
    (`str(err) or ""`), a conditional (`err if err else "none"`), a
    container display, a subscript (`errors[0]`), a comprehension whose
    element carries, a starred or awaited value. Not followed, on purpose:
    any other function that takes the name as an argument (`kind =
    _classify(entry_id, err)`, `len(errors)`: a function's result is not
    knowable from the name, and a helper's return value is a stated blind
    spot); binding such a target would report every logged result. Any
    call named `exception()` or `exc_info()` is read as the task method or
    `sys.exc_info()`, `_LOGGER.exception(...)` included: fail-closed, one
    review line. `operator.setitem(d, k, err)` and the like are functions,
    not methods, and fall under the sentence above.
    """
    parts: list[ast.AST] = []
    if isinstance(value, ast.Call):
        if getattr(value.func, "attr", "") == "exception":
            return True
        if getattr(value.func, "attr", "") == "exc_info":
            return True
        chain = _chain(value.func)
        if chain and chain[0] in _TEXT_WRAPPERS | _TRANSPARENT_CALLS:
            parts = [*value.args, *(kw.value for kw in value.keywords)]
        elif chain and chain[0] in _DEFAULT_METHODS:
            # `d.get(k, err)` and `m.get(k, default=err)` hand the default back;
            # every keyword counts (`Mapping.get` names it, a wrapper may not).
            parts = [*value.args[1:], *(kw.value for kw in value.keywords)]
        elif chain == ["getattr"] and len(value.args) >= 2:
            # `getattr(err, "msg")` reads what `err.msg` reads; only a code
            # attribute named literally is safe (`auth_flow.py:108`).
            attr = value.args[1]
            parts = list(value.args[2:])  # the default slot carries too
            if not (
                isinstance(attr, ast.Constant) and attr.value in _SAFE_EXCEPTION_ATTRS
            ):
                parts.append(value.args[0])
        if isinstance(value.func, ast.Attribute):
            # The receiver of any method carries into the result
            # (`str(err).strip()`, `errors.pop()`, `err.args[0].lower()`);
            # `describe_exception(err).split()` does not, its receiver is a
            # safe wrapper, and `err.errno.bit_length()` does not either.
            parts.append(value.func.value)
    elif isinstance(value, ast.BoolOp):
        parts = list(value.values)
    elif isinstance(value, (ast.Name, ast.Attribute)):
        if ast.unparse(value) in bound:
            return True
        if isinstance(value, ast.Attribute) and value.attr not in _SAFE_EXCEPTION_ATTRS:
            parts = [value.value]
    elif isinstance(value, (ast.Subscript, ast.Starred, ast.Await, ast.NamedExpr)):
        parts = [value.value]
    elif isinstance(value, ast.BinOp):
        parts = [value.left, value.right]
    elif isinstance(value, ast.IfExp):
        parts = [value.body, value.orelse]
    elif isinstance(value, ast.JoinedStr):
        return _mentions_exception(value, bound)
    elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        parts = list(value.elts)
    elif isinstance(value, ast.Dict):
        parts = [part for part in [*value.keys, *value.values] if part is not None]
    elif isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        # The comprehension target is bound by `_flow_bound_names` (it is an
        # `ast.comprehension` node), so the element decides at the fixpoint.
        parts = (
            [value.key, value.value] if isinstance(value, ast.DictComp) else [value.elt]
        )
    return any(_carries_exception(part, bound) for part in parts)


def _unpacked_names(
    target: ast.AST, value: ast.AST, bound: frozenset[str]
) -> tuple[str, ...]:
    """Names a binding writes from a carrier, matched by position where it can be.

    `err, count = task.exception(), 0` binds `err` and not `count`;
    `[first, *rest] = [task.exception(), 0]` binds `first` (the starred
    target collects the remainder, which carries when any of its elements
    does). A tuple target fed by anything but a tuple or list display
    binds every name when the value carries (`code, text = err.args`,
    `first, *rest = errors`: which slot holds the text is not knowable,
    so all of them do) and none when it does not (`a, b = helper(task)`).
    """
    if not isinstance(target, (ast.Tuple, ast.List)):
        return _target_names(target) if _carries_exception(value, bound) else ()
    if not isinstance(value, (ast.Tuple, ast.List)):
        return _target_names(target) if _carries_exception(value, bound) else ()
    targets, values = list(target.elts), list(value.elts)
    star = next((i for i, t in enumerate(targets) if isinstance(t, ast.Starred)), None)
    if star is None:
        if len(targets) != len(values):
            return ()
        pairs = list(zip(targets, values, strict=True))
        return tuple(n for t, v in pairs for n in _unpacked_names(t, v, bound))
    tail = len(targets) - star - 1
    if len(values) < len(targets) - 1:
        return ()
    head = list(zip(targets[:star], values[:star], strict=True))
    rear = list(zip(targets[star + 1 :], values[len(values) - tail :], strict=True))
    names = [n for t, v in head + rear for n in _unpacked_names(t, v, bound)]
    rest = values[star : len(values) - tail]
    if any(_carries_exception(v, bound) for v in rest):
        names.extend(_target_names(targets[star].value))
    return tuple(names)


def _container_names(expr: ast.AST) -> tuple[str, ...]:
    """The names an element write reaches: `d[k]` and `d`, `d.setdefault(k, []).append`'s `d`.

    A container whose element carries renders it whole, so the write to
    `d[k]`, to `self.errs["k"]` or through a method chain on `d` binds the
    container text as well as the element text. A call at the base
    (`get_list().append(err)`) names nothing.
    """
    if isinstance(expr, ast.Name):
        return (expr.id,)
    if isinstance(expr, ast.Attribute):
        return (ast.unparse(expr),)
    if isinstance(expr, ast.Subscript):
        return (ast.unparse(expr), *_container_names(expr.value))
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        return _container_names(expr.func.value)
    return ()


def _capture_names(pattern: ast.AST) -> tuple[str, ...]:
    """Every name a `match` pattern captures (`as x`, `*rest`, `**rest`)."""
    return tuple(
        name
        for node in ast.walk(pattern)
        for name in (getattr(node, "name", None), getattr(node, "rest", None))
        if name
    )


def _flow_bound_names(
    function: ast.AST,
    seed: tuple[str, ...],
    own: frozenset[str],
    aliases: dict[str, str],
    imported: frozenset[str],
    modules: dict[str, str],
) -> tuple[str, ...]:
    """Fourth binding: names that receive a bound exception inside one function.

    Followed to a fixpoint, whole function body, nested scopes included:
    an assignment, annotated assignment, augmented assignment or walrus
    whose value carries a bound name or `<task>.exception()`
    (`_carries_exception`: `detail = exc`, `self.last = err`, `shown =
    str(err)`, `msg += str(err)`, `first = errors[0]`; the attribute or
    subscript target is read back by its dotted text), a mutation of a
    container by a method that stores its argument (`parts.append(str(err))`
    binds `parts`), tuple and list unpacking matched by position (starred
    included) or, from a carrying value that is no display, as a whole
    (`code, text = err.args`), a `for`, `async for` or comprehension
    target whose iterable mentions a bound name (`for err in errors`,
    `for i, err in enumerate(errors)`, `for e in self.errors`; the index
    of `enumerate` and of `range(len(errors))` is bound too, fail-closed),
    and a `match` capture inside a foreign class pattern (`case
    ClientError() as err`, `case ClientError(args=[first])`) or any capture
    when the subject mentions a bound name. Fail-closed on purpose: a
    rebinding to a harmless value (`err = "safe"`) and a nested function
    with its own parameter of the same name keep the name bound; neither
    has an instance today, and a false report there costs one review
    line, a missed one a producer's text in the log. A handler name seeds
    this flow only inside its handler (`_exception_names_by_call` passes
    the handler as the function); an alias that outlives the handler
    (`except Exception as exc: last_error = exc`) and a local annotation
    with a union (`last_error: str | Exception | None`) are not followed
    after it: the package narrows such a name before each of its three
    log calls (`fcmregister.py`: one by `isinstance`, two by a `str`
    assignment in the same branch), and a flow-insensitive binding would
    report all three.
    """
    bound = set(seed)
    while True:
        frozen = frozenset(bound)
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                if node.value is None:
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    bound.update(_unpacked_names(target, node.value, frozen))
            elif isinstance(node, ast.AugAssign):
                if _carries_exception(node.value, frozen):
                    bound.update(_target_names(node.target))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
            ):
                # A mutation is a binding of the receiver, wherever the call
                # sits (a statement, a comprehension): `parts.append(str(err))`
                # and `d.setdefault(k, []).append(err)` make `parts` and `d`
                # render the text when logged.
                if any(
                    _carries_exception(part, frozen)
                    for part in [*node.args, *(kw.value for kw in node.keywords)]
                ):
                    bound.update(_container_names(node.func.value))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                if _mentions_exception(node.iter, frozen):
                    bound.update(_target_names(node.target))
            elif isinstance(node, ast.Match):
                subject_bound = _mentions_exception(node.subject, frozen)
                for case in node.cases:
                    if subject_bound:
                        bound.update(_capture_names(case.pattern))
                        continue
                    for pattern in ast.walk(case.pattern):
                        if isinstance(pattern, ast.MatchClass) and (
                            _is_foreign_exception_annotation(
                                pattern.cls, own, aliases, imported, modules
                            )
                        ):
                            bound.update(_capture_names(pattern))
                        elif isinstance(pattern, ast.MatchAs) and pattern.name:
                            if any(
                                isinstance(cls, ast.MatchClass)
                                and _is_foreign_exception_annotation(
                                    cls.cls, own, aliases, imported, modules
                                )
                                for cls in ast.walk(pattern)
                            ):
                                bound.add(pattern.name)
        if frozenset(bound) == frozen:
            return tuple(sorted(bound - set(seed)))


def _exception_names_by_call(tree: ast.AST) -> dict[int, tuple[str, ...]]:
    """Map each call to every foreign exception name in scope, `()` if none.

    Four bindings, accumulated rather than replaced: every parameter annotated
    with a foreign exception type (whole function body, all of them, not the
    first: `def f(first: BaseException, second: OSError)` logs `second`), a
    name that receives such a parameter or `<task>.exception()` through data
    flow (`_flow_bound_names`: assignment, unpacking, `for`, `match`, whole
    function body), and a foreign `except … as NAME` handler (handler body,
    with the same data flow bounded to that body).
    A call inside a foreign handler without `as` keeps the names the
    enclosing function bound (`def f(err: BaseException): … except
    Exception: log(err)` keeps `err`); the implicit traceback of such a
    handler (`logger.exception`, `exc_info=True`) is reported positionally,
    not through this map.
    """
    names: dict[int, tuple[str, ...]] = {}
    own = _own_type_names(tree)
    aliases = _import_aliases(tree)
    imported = _imported_exception_types(tree)
    modules = _module_bindings(tree)
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
            *([function.args.vararg] if function.args.vararg else []),
            *([function.args.kwarg] if function.args.kwarg else []),
        ]
        bound = tuple(
            p.arg
            for p in params
            if _is_foreign_exception_annotation(
                p.annotation, own, aliases, imported, modules
            )
        )
        bound += _flow_bound_names(function, bound, own, aliases, imported, modules)
        if not bound:
            continue
        for inner in ast.walk(function):
            if isinstance(inner, ast.Call):
                names[id(inner)] = names.get(id(inner), ()) + bound
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        if not _is_foreign_handler(handler, own):
            continue
        # The handler name seeds the same data flow, bounded to the handler
        # body: `except OSError as exc: text = str(exc); log(text)` binds
        # `text` there and not after the handler (see the module docstring
        # on `fcmregister.py`, where the alias outlives the handler and is
        # narrowed before it is logged).
        bound = (handler.name,) if handler.name else ()
        if handler.name:
            bound += _flow_bound_names(handler, bound, own, aliases, imported, modules)
        for inner in ast.walk(handler):
            if isinstance(inner, ast.Call):
                names[id(inner)] = names.get(id(inner), ()) + bound
    return names


def _exception_leaves(argument: ast.AST, name: str) -> list[tuple[str, str]]:
    """Shape (I): NAME, `str(NAME)`, `_clip(NAME)`, `f"{NAME}"`, `NAME.args`.

    NAME may be dotted or subscripted text (`self.last`, `self.errs["k"]`)
    when the binding wrote such a target; it is matched by its unparsed text.

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
        if (
            isinstance(node, (ast.Attribute, ast.Subscript))
            and not name.isidentifier()
            and ast.unparse(node) == name
            and id(node) not in cleared
        ):
            return [("I", name)]
    return []


def _retrieved_exception_leaves(argument: ast.AST) -> list[tuple[str, str]]:
    """Shape (I): `task.exception()` or `sys.exc_info()` passed to the log call directly.

    The done callback has no name to bind when it writes
    `_LOGGER.error("%s", t.exception())`; the call itself is the leaf, and so
    is `sys.exc_info()` (its second slot is the exception).
    """
    cleared: set[int] = set()
    for node in ast.walk(argument):
        if isinstance(node, ast.Call):
            chain = _chain(node.func)
            if chain and chain[0] in _SAFE_EXCEPTION_WRAPPERS:
                for inner in node.args:
                    cleared.update(id(n) for n in ast.walk(inner))
    for node in ast.walk(argument):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") in {"exception", "exc_info"}
            and id(node) not in cleared
        ):
            return [("I", f"{node.func.attr}()")]
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
    in_scope_for_i = relative.startswith(_SHAPE_I_ROOT)
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
        bound_names = exception_names.get(id(call), ()) if fmt != "raise" else ()
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
                    leaf
                    for name in bound_names
                    if id(argument) not in traceback_args
                    for leaf in _exception_leaves(argument, name)
                ),
                *(
                    _retrieved_exception_leaves(argument)
                    if in_scope_for_i and id(argument) not in traceback_args
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


_SHAPE_I_PARAMETER_FIXTURE = """
import logging
import cryptography.exceptions
import cryptography.exceptions as ce
from typing import Optional as Maybe
from collections.abc import Sequence as Seq
from aiohttp import ClientError
from aiohttp import ClientError as TransportFailure
from cryptography.exceptions import InvalidTag
from .exceptions import FatalRegistrationError

_LOGGER = logging.getLogger(__name__)


def _foreign(entry_id: str, err: ClientError) -> None:
    _LOGGER.error("[entry=%s] client error: %s", entry_id, err)


def _broad(entry_id: str, err: BaseException) -> None:
    _LOGGER.info("[entry=%s] failed: %s", entry_id, err)
    _LOGGER.info("[entry=%s] failed: %s", entry_id, describe_exception(err))


def _own(entry_id: str, err: FatalRegistrationError) -> None:
    _LOGGER.error("[entry=%s] fatal: %s", entry_id, err)


def _union(err: "OSError | ValueError") -> None:
    _LOGGER.debug("io: %s", err)


def _attribute(err: ssl.SSLError | None) -> None:
    _LOGGER.debug("tls: %s", err)


def _handler_without_name(err: BaseException) -> None:
    try:
        pass
    except Exception:
        _LOGGER.debug("still bound: %s", err)


def _optional(err: Optional[ClientError]) -> None:
    _LOGGER.debug("maybe: %s", err)


def _right_member(err: None | ClientError) -> None:
    _LOGGER.debug("right: %s", err)


def _kind(kind: type[Exception]) -> None:
    _LOGGER.debug("class, not instance: %s", kind)


def _second(first: BaseException, second: OSError) -> None:
    _LOGGER.error("second: %s", second)


def _both(err: BaseException) -> None:
    try:
        pass
    except OSError as inner:
        _LOGGER.error("both: %s %s", err, inner)


def _retrieved(task, err: BaseException) -> None:
    exc = task.exception()
    if exc:
        _LOGGER.error("task: %s %s", err, exc)


def _variadic(*errors: BaseException, **named: OSError) -> None:
    _LOGGER.error("many: %s %s", errors, named)


def _aliased(err: TransportFailure) -> None:
    _LOGGER.error("alias: %s", err)


def _annotated(task) -> None:
    exc: BaseException | None = task.exception()
    if exc:
        _LOGGER.error("typed: %s", exc)


def _walrus(task) -> None:
    if (exc := task.exception()):
        _LOGGER.error("walrus: %s", exc)


def _direct(task) -> None:
    _LOGGER.error("direct: %s %s", task.exception(), describe_exception(task.exception()))


def _attribute_target(self, task) -> None:
    self._last = task.exception()
    _LOGGER.error("attr: %s", self._last)


def _container(errors: list[ClientError], pairs: tuple[OSError, ...]) -> None:
    _LOGGER.error("many: %s %s", errors, pairs)


def _no_suffix(group: ExceptionGroup, stop: SystemExit) -> None:
    _LOGGER.error("group: %s %s", group, stop)


def _mapping(errors: dict[str, BaseException], keys: dict[OSError, str]) -> None:
    _LOGGER.error("map: %s %s", errors, keys)


def _heads(group: ExceptionGroup[OSError], tagged: Annotated[ClientError, "x"]) -> None:
    _LOGGER.error("heads: %s %s", group, tagged)


def _no_convention(err: InvalidTag) -> None:
    _LOGGER.error("tag: %s", err)


def _qualified(
    err: cryptography.exceptions.InvalidSignature, other: ce.AlreadyFinalized
) -> None:
    _LOGGER.error("qualified: %s %s", err, other)


def _carrier_alias(err: Maybe[ClientError], many: Seq[OSError]) -> None:
    _LOGGER.error("carrier alias: %s %s", err, many)


def _stdlib(errors: defaultdict[str, OSError], queue: deque[ClientError], pending: Future[OSError]) -> None:
    _LOGGER.error("stdlib: %s %s %s", errors, queue, pending)
"""


def test_shape_i_reports_annotated_exception_parameters() -> None:
    """A foreign exception bound outside any `except` is a sink.

    `_classify_registration_exception(entry_id, err: BaseException)` in
    `fcm_receiver_ha.py` logged the producer's text without any `except`
    in sight; the handler-only binding did not see it (2026-09-18). Reported:
    a library type (line 16), a broad type (line 20), a string union (line
    29), an attribute in a union with `None` (line 33, `ssl.SSLError` is
    read like `_is_foreign_handler` reads it), a parameter logged inside a
    handler without `as` (line 40, the handler does not clear the binding),
    `Optional[...]` (line 44), a union whose foreign member is on the right
    (line 48), the second of two annotated parameters (line 56, every bound
    name is tracked, not the first), a parameter next to an `as` handler
    (line 63, both names, the handler accumulates, it does not replace), a
    name assigned from `task.exception()` next to a parameter (line 69,
    `err` and `exc`, the shape of `_track_task._done`), variadic parameters
    (line 73, `*errors` and `**named`), an import alias without a suffix
    (line 77, `TransportFailure` is `ClientError`) and an annotated
    assignment from `task.exception()` (line 83), a walrus (line 88), the
    call passed directly (line 92, leaf `exception()`; the wrapped second
    argument on the same line is not reported), an attribute target (line
    97, `self._last`) and container annotations (line 101, `list[...]` and
    `tuple[..., ...]`) and built-in types without a suffix (line 105,
    `ExceptionGroup` and `SystemExit`, read from `builtins`), a mapping
    whose values or keys are exceptions (line 109, `errors` and `keys`; a
    dict renders both), a generic exception head (`ExceptionGroup[OSError]`)
    and `Annotated[...]` (line 113), and an imported third-party type without
    a suffix, resolved as a class at import time (line 117, `InvalidTag`), and types
    reachable only module-qualified or through a module alias (line 123,
    `InvalidSignature` and `AlreadyFinalized`, neither imported by name), and
    carriers imported under an alias (line 127, `Maybe[...]`, `Seq[...]`),
    and the stdlib containers and an `asyncio` future (line 131,
    `defaultdict[str, OSError]`, `deque[...]`, `Future[...]`; Codex review
    on 714596f). Not reported:
    the wrapped
    argument (line 21), an own type (line 25), which carries text this
    package wrote, and `type[Exception]` (line 52, the only parameter of its
    function, so its absence from the list is a measurement, not a vacuum),
    which is a class, not an instance.
    """
    tree = ast.parse(_SHAPE_I_PARAMETER_FIXTURE)
    offenders, _ = _scan_tree(tree, "Auth/fixture.py")
    by_line = sorted((line, leaf) for _, line, leaf, _ in offenders)
    assert by_line == [
        (16, "err"),
        (20, "err"),
        (29, "err"),
        (33, "err"),
        (40, "err"),
        (44, "err"),
        (48, "err"),
        (56, "second"),
        (63, "err"),
        (63, "inner"),
        (69, "err"),
        (69, "exc"),
        (73, "errors"),
        (73, "named"),
        (77, "err"),
        (83, "exc"),
        (88, "exc"),
        (92, "exception()"),
        (97, "self._last"),
        (101, "errors"),
        (101, "pairs"),
        (105, "group"),
        (105, "stop"),
        (109, "errors"),
        (109, "keys"),
        (113, "group"),
        (113, "tagged"),
        (117, "err"),
        (123, "err"),
        (123, "other"),
        (127, "err"),
        (127, "many"),
        (131, "errors"),
        (131, "pending"),
        (131, "queue"),
    ]
    outside, _ = _scan_tree(tree, "coordinator/fixture.py")
    assert outside == []


_SHAPE_I_FLOW_FIXTURE = """
import logging
from aiohttp import ClientError
from cryptography import exceptions as cex

_LOGGER = logging.getLogger(__name__)


def _alias(err: ClientError) -> None:
    detail = err
    shown = str(err)
    _LOGGER.error("alias: %s %s", detail, shown)


def _attribute(self, err: ClientError) -> None:
    self.last = err
    _LOGGER.error("attr: %s", self.last)


def _positional(task) -> None:
    err, count = task.exception(), 0
    _LOGGER.error("tuple: %s %s", err, count)


def _starred(task) -> None:
    [first, *rest] = [task.exception(), 0, 1]
    *head, last = 0, 1, task.exception()
    lead, *mid, tail = 0, task.exception(), 1
    _LOGGER.error("star: %s %s %s %s %s %s %s", first, rest, head, last, lead, mid, tail)


def _helper_result(task) -> None:
    a, b = _pick(task)
    _LOGGER.error("helper: %s %s", a, b)


def _loop(errors: list[ClientError]) -> None:
    for err in errors:
        _LOGGER.error("loop: %s", err)
    _LOGGER.error("comp: %s", ", ".join(str(e) for e in errors))


def _match(value, err: ClientError) -> None:
    match value:
        case ClientError() as caught:
            _LOGGER.error("class: %s", caught)
        case str() as text:
            _LOGGER.error("text: %s", text)
    match err:
        case other:
            _LOGGER.error("subject: %s", other)


def _code(err: ClientError) -> None:
    code = err.errno
    kind = _classify(err)
    _LOGGER.error("code: %s %s", code, kind)


def _shadow(err: ClientError) -> None:
    err = "safe"
    _LOGGER.error("shadow: %s", err)


def _from_module(err: cex.InvalidTag) -> None:
    _LOGGER.error("module by from: %s", err)


def _nested_first(err: ClientError) -> None:
    if err:
        detail = err
    shown = str(detail)
    _LOGGER.error("fixpoint: %s", shown)


def _derived(err: ClientError, errors: list[ClientError]) -> None:
    msg = "prefix: "
    msg += str(err)
    joined = "x " + str(err)
    formatted = "x %s" % err
    chosen = err if err else "none"
    first = errors[0]
    pair = (err, 1)
    texts = [str(e) for e in errors]
    glued = ", ".join(str(e) for e in errors)
    reason = err.strerror
    _LOGGER.error("derived: %s %s %s %s %s %s %s %s %s", msg, joined, formatted, chosen, first, pair, texts, glued, reason)


async def _iterables(self, errors: dict[str, ClientError]) -> None:
    for key, err in errors.items():
        _LOGGER.error("items: %s %s", key, err)
    for i, err in enumerate(list(errors.values())):
        _LOGGER.error("enumerate: %s %s", i, err)
    self.errors = errors
    for e in self.errors:
        _LOGGER.error("attribute container: %s", e)
    async for item in _stream(errors):
        _LOGGER.error("async: %s", item)


def _captures(self, value, err: ClientError) -> None:
    match value:
        case ClientError(args=[first]):
            _LOGGER.error("sub-capture: %s", first)
    match str(err):
        case text:
            _LOGGER.error("subject text: %s", text)
    self.errs["k"] = err
    _LOGGER.error("subscript target: %s", self.errs["k"])


def _not_followed(err: ClientError, task) -> None:
    last: str | ClientError | None = _pick(task)
    render = lambda: str(err)
    _LOGGER.error("opaque: %s %s", last, render())


def _more_derived(err: ClientError, errors: list[ClientError]) -> None:
    chosen = getattr(err, "msg", None) or str(err) or ""
    lowered = str(err).lower()
    popped = errors.pop()
    parts = []
    parts.append(str(err))
    code, text = err.args
    head, *tail = errors
    copied = list(errors)
    mapped = ", ".join(map(str, errors))
    _LOGGER.error("more: %s %s %s %s %s %s %s %s %s", chosen, lowered, popped, parts, code, text, head, tail, copied)
    named = getattr(err, "msg", None)
    _LOGGER.error("mapped: %s %s", mapped, named)
    words = describe_exception(err).split()
    bits = err.errno.bit_length()
    count = len(errors)
    kind = getattr(err, "errno", None)
    _LOGGER.error("harmless: %s %s %s %s", words, bits, count, kind)


def _in_handler(task) -> None:
    try:
        pass
    except OSError as exc:
        text = str(exc)
        code, detail = exc.args
        _LOGGER.error("handler flow: %s %s %s", text, code, detail)
    _LOGGER.error("after handler: %s", text)


def _containers(err: ClientError, obj) -> None:
    by_key = {}
    by_key["k"] = err
    grouped = {}
    grouped.setdefault("k", []).append(err)
    collected = []
    [collected.append(str(e)) for e in [err]]
    fallback = getattr(obj, "x", err)
    fields = vars(err)
    _LOGGER.error("containers: %s %s %s %s %s", by_key, grouped, collected, fallback, fields)


def _element_writes(err: ClientError, d, e, f, g) -> None:
    d["k"] += str(err)
    e["a"], e["b"] = err, 1
    f.__setitem__(0, err)
    defaulted = g.get("x", err)
    popped = g.pop("y", err)
    _LOGGER.error("element writes: %s %s %s %s %s", d, e, f, defaulted, popped)


def _exc_info() -> None:
    try:
        pass
    except Exception:
        current = sys.exc_info()[1]
        _LOGGER.error("exc_info: %s %s", sys.exc_info(), current)


def _stdlib_containers(err: ClientError, keys, m, parts) -> None:
    keyed = dict.fromkeys(keys, err)
    queued = deque([err])
    by_keyword = m.get("k", default=err)
    parts.__iadd__([err])
    _LOGGER.error("stdlib: %s %s %s %s", keyed, queued, by_keyword, parts)
"""


def test_shape_i_follows_bound_exceptions_by_data_flow() -> None:
    """A name derived from a bound exception inside the function is bound too.

    Reported: an alias and the text of a bound parameter (line 12, `detail`
    and `shown`), an attribute target read back by its dotted name (line 17),
    the positional half of a tuple assignment (line 22, `err` and not
    `count`), starred unpacking matched from both ends with the star
    absorbing the surplus (line 29: `first`, `last` and `mid`, whose
    remainder holds the call; `rest` and `head` collect plain values and
    are not reported; three values against two targets, so a version that
    pairs by index alone binds nothing here), a `for` target and a
    comprehension target over a bound container (lines 39 and 40; line 40
    carries `errors` itself as well), a `match` capture with a foreign
    class pattern (line 46, `caught`; the `str()` capture on line 48 is
    not) and any capture when the subject is bound (line 51), a rebinding
    to a harmless value (line 62: fail-closed, stated in the module
    docstring), a type reached through a module bound by `from` (line 66,
    `cex.InvalidTag`; the Codex finding of 2026-09-18 on `_module_bindings`
    reading `ast.Import` only), a chain whose first link sits in a nested
    block that `ast.walk` visits after the second (line 73, `shown` from
    `detail` from `err`: the fixpoint, not one pass, binds it), nine
    derivations of the text or a container (line 87: `+=`, concatenation,
    `%` formatting, a conditional, an element, a tuple display, a
    comprehension, `join`, a non-code attribute; the review of 2026-09-18
    found each unfollowed), `for` targets whose iterable mentions a bound
    name through `.items()`, `enumerate(list(...))`, an attribute holding
    the container or an `async for` (lines 92, 94, 97, 99; `key` and `i`
    are reported too, fail-closed, the iterable is not typed), a capture
    nested in a foreign class pattern and a subject that is the text (lines
    105 and 108), a subscript target read back by its text and the
    container it writes into (line 110, `self.errs['k']` and `self.errs`),
    nine more derivations (line 129: `or` over `getattr(err, "msg")` and
    `str(err)`, the shape of `auth_flow._describe_lost_session`; a method on
    the text; `errors.pop()`; a list mutated by `append`; both names of an
    unpacking from `err.args` and from the container itself; `list(errors)`)
    with `map(str, errors)` under `join` and `getattr(err, "msg")` on its
    own (line 131; the second review of 2026-09-18 found each unfollowed),
    the text and the unpacked `args` of a handler name inside its handler
    (line 145, the third review's pre-existing gap: the handler seeds the
    same flow) and five container writes (line 158: an element assignment
    binds the mapping, `setdefault(...).append` binds the mapping behind
    the chain, `append` inside a comprehension binds the list, the default
    slot of `getattr`, `vars(err)`), five element writes (line 167: `+=`
    on an element, elements of a tuple target, `__setitem__`, the default
    slot of `.get` and `.pop` on an unbound mapping; the fourth review of
    2026-09-18), `sys.exc_info()` passed directly or through its second slot
    (line 175), and four more container forms (line 183: `dict.fromkeys`,
    `deque`, a keyword default, `__iadd__`; the fifth review). Not reported: a tuple target fed by a
    helper's result (line 34, `_pick` is not followed), a code attribute and
    a classifier's result (line 57, `code` and `kind`), a local annotation
    whose value is a helper's result and a lambda that is called (line 116:
    the package narrows such a name before logging, and a lambda's result
    is a call's), and four harmless derivations (line 136: a method on a
    safe wrapper's result, a method on a code attribute, `len()`, and
    `getattr` naming a code attribute), and the handler alias read after
    the handler (line 146: the flow of a handler name ends with its body;
    `fcmregister.py` narrows such a name before logging it).
    """
    tree = ast.parse(_SHAPE_I_FLOW_FIXTURE)
    offenders, _ = _scan_tree(tree, "Auth/fixture.py")
    by_line = sorted((line, leaf) for _, line, leaf, _ in offenders)
    assert by_line == [
        (12, "detail"),
        (12, "shown"),
        (17, "self.last"),
        (22, "err"),
        (29, "first"),
        (29, "last"),
        (29, "mid"),
        (39, "err"),
        (40, "e"),
        (40, "errors"),
        (46, "caught"),
        (51, "other"),
        (62, "err"),
        (66, "err"),
        (73, "shown"),
        (87, "chosen"),
        (87, "first"),
        (87, "formatted"),
        (87, "glued"),
        (87, "joined"),
        (87, "msg"),
        (87, "pair"),
        (87, "reason"),
        (87, "texts"),
        (92, "err"),
        (92, "key"),
        (94, "err"),
        (94, "i"),
        (97, "e"),
        (99, "item"),
        (105, "first"),
        (108, "text"),
        (110, "self.errs"),
        (110, "self.errs['k']"),
        (129, "chosen"),
        (129, "code"),
        (129, "copied"),
        (129, "head"),
        (129, "lowered"),
        (129, "parts"),
        (129, "popped"),
        (129, "tail"),
        (129, "text"),
        (131, "mapped"),
        (131, "named"),
        (145, "code"),
        (145, "detail"),
        (145, "text"),
        (158, "by_key"),
        (158, "collected"),
        (158, "fallback"),
        (158, "fields"),
        (158, "grouped"),
        (167, "d"),
        (167, "defaulted"),
        (167, "e"),
        (167, "f"),
        (167, "popped"),
        (175, "current"),
        (175, "exc_info()"),
        (183, "by_keyword"),
        (183, "keyed"),
        (183, "parts"),
        (183, "queued"),
    ]
    outside, _ = _scan_tree(tree, "coordinator/fixture.py")
    assert outside == []


_RENDERING_HEADS = (
    "AbstractSet",
    "ChainMap",
    "Collection",
    "Container",
    "Counter",
    "DefaultDict",
    "Deque",
    "Dict",
    "FrozenSet",
    "Future",
    "ItemsView",
    "Iterable",
    "Iterator",
    "KeysView",
    "LifoQueue",
    "List",
    "Mapping",
    "MappingProxyType",
    "MutableMapping",
    "MutableSequence",
    "MutableSet",
    "Optional",
    "OrderedDict",
    "PriorityQueue",
    "Queue",
    "Reversible",
    "Sequence",
    "Set",
    "Task",
    "Tuple",
    "Union",
    "ValuesView",
    "defaultdict",
    "deque",
    "dict",
    "frozenset",
    "list",
    "set",
    "tuple",
)


def test_member_carriers_expose_their_members() -> None:
    """Every rendering head flattens to its members; a dropped entry is caught here.

    The parameter fixture pins three heads; the other entries would vanish
    silently on a later clean-up (review of 2026-09-18, X-03). The list is
    spelled out on purpose: a test that iterated over `_MEMBER_CARRIERS`
    would lose the case together with the entry. One case per head, the
    member is `OSError` in every slot the head takes.
    """
    assert set(_RENDERING_HEADS) == _MEMBER_CARRIERS
    for head in _RENDERING_HEADS:
        annotation = ast.parse(f"{head}[OSError]", mode="eval").body
        members = [ast.unparse(m) for m in _annotation_members(annotation)]
        assert members == ["OSError"], head
    kept = ast.parse("ExceptionGroup[OSError]", mode="eval").body
    assert [ast.unparse(m) for m in _annotation_members(kept)] == ["ExceptionGroup"]


def test_import_helpers_survive_a_hostile_module_getattr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module whose `__getattr__` raises contributes nothing and breaks no scan.

    `getattr(module, name, None)` swallows `AttributeError` only; a module
    `__getattr__` (PEP 562) may raise anything, and both import helpers read
    attributes of modules they did not write. The read sits inside the
    `try` (review of 2026-09-18, F-15), so the naming convention decides and
    the scan goes on.
    """

    class _Hostile(types.ModuleType):
        def __getattr__(self, name: str) -> object:
            raise RuntimeError(name)

    def fake_import(name: str) -> types.ModuleType:
        if name == "hostile":
            return _Hostile("hostile")
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    tree = ast.parse("from hostile import sub\nfrom hostile import Boom as Error\n")
    assert _module_bindings(tree) == {}
    assert _imported_exception_types(tree) == frozenset()


def test_shape_i_reports_producer_exceptions_in_broad_auth_handlers() -> None:
    """Shape (I): seven sinks carry a foreign exception, four are safe.

    A wrapper clears only its own argument (line 15), a builtin type is
    foreign (line 17), a type defined in the module is not (line 19, but its
    `exc_info` is: an own exception raised `from` a foreign one renders the
    cause). A traceback is a sink wherever it is attached: `exc_info=exc`
    (line 14), `logger.exception` (line 28), `exc_info=True` (29), an alias
    bound in the handler and logged outside it (32), a tuple (33);
    `exc_info=False` (30) and `exc_info=None` (31) are not. The same fixture
    outside `Auth/` reports nothing, so the scope is measured and not assumed.
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


def test_former_pinned_file_is_in_shape_i_scope() -> None:
    """`fcm_receiver_ha.py` is scanned for shape (I) like any other Auth module.

    Three assertions in one test, none of which carries the proof alone: the
    real file has no offender, it is actually scanned (`scanned` counts the
    log calls regardless of scope, so an empty offender list on its own would
    be a vacuum), and the shape-(I) fixture under this very path reports the
    same 13 sites as under `Auth/fixture.py`, which is what proves that shape
    (I) applies on the path. A new pin would need a new follow-up plan; none
    exists.
    """
    relative = "Auth/fcm_receiver_ha.py"
    path = _PACKAGE_ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders, scanned = _scan_tree(tree, relative)
    assert offenders == []
    assert scanned > 100, scanned
    fixture = ast.parse(_SHAPE_I_FIXTURE)
    under_path, _ = _scan_tree(fixture, relative)
    under_fixture, _ = _scan_tree(fixture, "Auth/fixture.py")
    by_line = sorted((line, leaf) for _, line, leaf, _ in under_path)
    assert by_line == sorted((line, leaf) for _, line, leaf, _ in under_fixture)
    assert len(by_line) == 13
