# tests/test_guard_logging_payloads.py
"""Guard: no log call in the integration passes raw payload, token or key bytes.

`AGENTS.md` section 5 forbids raw API payloads, tokens and key material in
log records at every level. The value that leaks is not an identifier but a
piece of the wire: a hex dump of a server response, the first bytes of an
undecodable protobuf, the prefix of a shared secret. This guard watches that
class; `test_guard_logging_identifiers.py` watches class (c) identifiers and
the two do not overlap (address-like leaves there, payload-like leaves here).

Three shapes are recognised inside a log call's arguments:

  (A) a `.hex()` call, whatever the receiver is called;
  (B) a slice (`x[:n]`, `x[a:b]`) whose name chain carries a payload-like
      name (`hex`, `response`, `payload`, `raw`, `secret`, `identity_key`,
      `_bytes`, `body`, `blob`, `token`);
  (C) a serialised protobuf message (`MessageToJson(...)`,
      `MessageToDict(...)`, `MessageToString(...)`, `.SerializeToString()`),
      which dumps every field of a wire message including credentials.

The EID is allowed at any level, in full or truncated (`AGENTS.md`, class
(a)); the leaves that carry it are listed in `_ROTATING_LEAVES` and excused
by name. One further site logs the first four bytes of an FMDN advertisement
frame (`payload[:4].hex()` in the BLE scanner: frame byte plus three EID
bytes, rotating) and is excused by (path, leaf, format-string prefix), so the
exception covers that one call and not every `payload` line of the module.

Blind spot, stated on purpose: the guard sees only shapes (A) to (C). A raw
value copied into a neutrally named variable before the call
(`preview = resp[:200]; _LOGGER.debug("%s", preview)`), an unsliced raw value
under a neutral name (`text`, `data`, `msg`), a slice whose whole chain is
neutrally named (`page.read()[:16]`), a wrapping call whose arguments hide
the name (`str(response)[:16]`, `bytes(payload)[:16]`: the chain follows the
callee, not the arguments), `binascii.hexlify()`, `base64.b64encode()` or
`repr(bytes)` are not reported; nor is a message passed whole to `%s`
(`str(message)` is its text format). Arguments of a logging wrapper that is
not itself a log call are unchecked, whatever they carry; the two wrappers
the package has, `_log_verbose` and `_log_warn_with_limit`, are treated as
log calls by name. Shape
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


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
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
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_log_call(node):
                continue
            scanned += 1
            # `.log(level, msg, ...)` carries the format string at index 1.
            msg_index = 1 if node.func.attr == "log" else 0
            first = node.args[msg_index] if len(node.args) > msg_index else None
            fmt = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else ""
            )
            arguments: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
            seen: set[str] = set()
            for argument in arguments:
                for _shape, leaf in _payload_leaves(argument):
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
