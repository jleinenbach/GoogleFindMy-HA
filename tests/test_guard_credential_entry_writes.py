# tests/test_guard_credential_entry_writes.py
"""Guard for two credential rules of the agent contract.

Both rules are stated in prose and both were broken once by a change that passed
the rest of the suite:

* **R2, a full fingerprint** (``agents/runtime_patterns/AGENTS.md``, "One owner
  per entry reload"). The update listener reloads the entry when
  ``_credential_fingerprint`` changes. Leaving the receiver-owned
  ``fcm_credentials`` out of it silenced the receiver's own write, but it also
  hid a user import that changes only that member until the next restart. A
  receiver self-write is told apart by a one-shot receipt (``entry_self_write``)
  instead.
* **R1, detached copies** (``Auth/AGENTS.md``, "Credential ownership across
  receiver, cache and entry"). The receiver edits its registration in place, so a
  registration it hands to ``entry.data`` must be a deep copy; a shared container
  would rewrite the entry behind Home Assistant's back.

Both checks are closed against the grammar rather than against the forms seen so
far, and fail on anything they cannot classify:

* The fingerprint body may only call the functions on ``_FINGERPRINT_CALLS`` and
  may not delete, filter in a comprehension, assign into a subscript or name the
  member. A filter written as ``del``, ``pop``, a helper call or a delegation
  therefore fails as an unknown call or statement. A behaviour probe pins that
  every credential key and every sampled bundle member moves the fingerprint.
* Every occurrence of the member key in ``Auth/fcm_receiver_ha.py`` (string
  constant, module-body constant bound to it, or keyword argument) is classified
  as a hash read (``.get`` or subscript load whose value goes straight into
  ``credential_member_digest``), an ``in`` test, a token cache read or write
  (``.get``/``.set`` on a receiver named in ``_CACHE_RECEIVERS``; ``TokenCache``
  detaches values itself, pinned in ``tests/test_token_cache_value_isolation.py``),
  or a mapping write (subscript store, dict display key, keyword argument). Any
  other occurrence fails as unclassified, so a read whose value is kept or
  written through (``bundle[K]["gcm"] = v``, ``self.creds[e] = bundle.get(K)``)
  fails too.
* A mapping write must take a detached value: a ``copy.deepcopy(...)`` call, or
  a local name every binding of which in the enclosing function is a plain
  assignment of such a call. Every binding form of the grammar counts
  (assignment and unpacking targets, augmented and annotated assignment, walrus,
  loop, ``with`` and comprehension targets, parameters, ``except ... as``,
  ``match`` captures, imports, ``global``/``nonlocal``, nested definitions); any
  but the plain deepcopy assignment fails. Such a local may then be written
  once and otherwise only hashed or copied again; a second write or any other
  use would share the container.
* ``_BROKEN`` pins the forms found in review against a clean reference source.

Detection limits, stated rather than implied. A key built at runtime (string
concatenation, a constant imported from another module) is not recognised as the
member. The token cache is recognised by its receiver name, from a closed list.
Writes of the member outside the receiver (the config flow, ``TokenCache``) are
out of scope.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INTEGRATION = _REPO_ROOT / "custom_components" / "googlefindmy"
_INIT = _INTEGRATION / "__init__.py"
_RECEIVER = _INTEGRATION / "Auth" / "fcm_receiver_ha.py"
_MEMBER = "fcm_credentials"

#: Calls ``_credential_fingerprint`` may make, as ``ast.unparse`` of the callee.
#: Anything else (a helper, ``pop``, ``filter``) could drop a member.
_FINGERPRINT_CALLS = frozenset(
    {
        "isinstance",
        "json.dumps",
        "hashlib.sha256",
        "material.encode",
        "hashlib.sha256(material.encode('utf-8')).hexdigest",
        "container.get",
    }
)


#: Calls that only hash a registration (``entry_self_write``).
_DIGEST_CALLS = frozenset({"credential_member_digest"})

#: Comparison operators of an ``in`` test, which only reads the member.
_MEMBERSHIP_TESTS = (ast.In, ast.NotIn)  # codespell:ignore

#: Receiver names of the token cache in the receiver module. ``TokenCache``
#: hands out and stores detached copies itself
#: (``tests/test_token_cache_value_isolation.py``). Closed on purpose: another
#: name fails as unclassified instead of passing as a cache.
_CACHE_RECEIVERS = frozenset({"cache", "entry_cache"})


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(found) == 1, f"expected one module-level {name}, found {len(found)}"
    return found[0]


def _credential_keys(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == "_CREDENTIAL_KEYS":
            assert isinstance(value, ast.Tuple | ast.List), "unexpected shape"
            return [ast.unparse(element) for element in value.elts]
    raise AssertionError("_CREDENTIAL_KEYS not found in __init__.py")


def test_the_fingerprint_filters_no_member() -> None:
    """R2: ``_credential_fingerprint`` hashes every credential key in full."""

    function = _function(_parse(_INIT), "_credential_fingerprint")
    problems: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            if callee not in _FINGERPRINT_CALLS:
                problems.append(f"line {node.lineno}: call {callee}")
        elif isinstance(node, ast.comprehension) and node.ifs:
            problems.append(f"line {node.lineno}: filtering comprehension")
        elif isinstance(node, ast.Delete):
            problems.append(f"line {node.lineno}: del")
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            problems.append(f"line {node.lineno}: subscript assignment")
        elif isinstance(node, ast.Constant) and node.value == _MEMBER:
            problems.append(f"line {node.lineno}: names {_MEMBER!r}")
    assert not problems, (
        "_credential_fingerprint may drop a member; tell a writer apart by a "
        f"receipt (entry_self_write) instead: {problems}"
    )


def test_the_fingerprint_covers_the_secrets_bundle() -> None:
    """R2: the bundle that carries the registration is a fingerprinted key."""

    assert "DATA_SECRET_BUNDLE" in _credential_keys(_parse(_INIT))


def test_every_credential_key_and_bundle_member_moves_the_fingerprint() -> None:
    """R2, behaviour: a change of any single key or bundle member is visible."""

    from custom_components.googlefindmy import (
        _CREDENTIAL_KEYS,
        _credential_fingerprint,
    )
    from custom_components.googlefindmy.const import DATA_SECRET_BUNDLE

    bundle = {
        "username": "u",
        _MEMBER: {"gcm": {"a": 1}},
        "owner_key": "k",
        "shared_key": "s",
    }
    base = {key: "v" for key in _CREDENTIAL_KEYS}
    base[DATA_SECRET_BUNDLE] = bundle
    reference = _credential_fingerprint(base)
    for key in _CREDENTIAL_KEYS:
        if key == DATA_SECRET_BUNDLE:
            continue
        changed = {**base, key: "w"}
        assert _credential_fingerprint(changed) != reference, key
    for member in bundle:
        changed = {**base, DATA_SECRET_BUNDLE: {**bundle, member: "changed"}}
        assert _credential_fingerprint(changed) != reference, member


def _is_deepcopy(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "deepcopy"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "copy"
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }


def _binding_sites(function: ast.AST, name: str) -> list[ast.AST]:
    """Every node in ``function`` that binds ``name``, in any grammar form."""

    sites: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, ast.Store | ast.Del):
                sites.append(node)
        elif isinstance(node, ast.arg) and node.arg == name:
            sites.append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            sites.append(node)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name == name:
            sites.append(node)
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            sites.append(node)
        elif isinstance(node, ast.alias):
            if (node.asname or node.name.split(".")[0]) == name:
                sites.append(node)
        elif isinstance(node, ast.Global | ast.Nonlocal) and name in node.names:
            sites.append(node)
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node is not function
            and node.name == name
        ):
            sites.append(node)
    return sites


def _is_detached(
    value: ast.AST, function: ast.AST, parents: dict[ast.AST, ast.AST]
) -> bool:
    if _is_deepcopy(value):
        return True
    if not isinstance(value, ast.Name):
        return False
    sites = _binding_sites(function, value.id)
    if not sites:
        return False
    for site in sites:
        statement = parents.get(site)
        if not (
            isinstance(site, ast.Name)
            and isinstance(site.ctx, ast.Store)
            and isinstance(statement, ast.Assign)
            and site in statement.targets
            and _is_deepcopy(statement.value)
        ):
            return False
    return True


def _member_names(tree: ast.Module) -> set[str]:
    """Module-level names bound to the member key, used as a key elsewhere."""

    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and value.value == _MEMBER:
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _is_member_key(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == _MEMBER
    return isinstance(node, ast.Name) and node.id in names


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.AST | None:
    current = parents.get(node)
    while current is not None and not isinstance(
        current, ast.FunctionDef | ast.AsyncFunctionDef
    ):
        current = parents.get(current)
    return current


def _feeds_the_digest(expr: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when ``expr`` is an argument of ``credential_member_digest``.

    A conditional expression in between (``x.get(K) if ... else None``) is
    looked through; any other use of the value read is not a hash.
    """

    node, parent = expr, parents.get(expr)
    while isinstance(parent, ast.IfExp) and node is not parent.test:
        node, parent = parent, parents.get(parent)
    return (
        isinstance(parent, ast.Call)
        and ast.unparse(parent.func) in _DIGEST_CALLS
        and node in parent.args
    )


def _classify_subscript(
    parent: ast.Subscript, parents: dict[ast.AST, ast.AST]
) -> tuple[str, ast.AST | None]:
    """Classify the member key used as the index of ``parent``."""

    if isinstance(parent.ctx, ast.Load):
        if _feeds_the_digest(parent, parents):
            return "read", None
        return "unclassified", None
    statement = parents.get(parent)
    if isinstance(statement, ast.Assign) and parent in statement.targets:
        return "mapping write", statement.value
    if isinstance(statement, ast.AnnAssign) and statement.target is parent:
        return "mapping write", statement.value
    return "unclassified", None


def _classify_call_argument(parent: ast.Call, parents: dict[ast.AST, ast.AST]) -> str:
    """Classify the member key passed as the first argument of ``parent``."""

    callee = parent.func
    if isinstance(callee, ast.Attribute):
        if ast.unparse(callee.value) in _CACHE_RECEIVERS:
            if callee.attr == "get":
                return "cache read"
            if callee.attr == "set":
                return "cache write"
        if callee.attr == "get" and _feeds_the_digest(parent, parents):
            return "read"
    return "unclassified"


def _classify(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[str, ast.AST | None]:
    """Return ``(kind, written value)`` for one occurrence of the member key."""

    parent = parents.get(node)
    kind: tuple[str, ast.AST | None] = ("unclassified", None)
    if isinstance(node, ast.keyword):
        kind = ("mapping write", node.value)
    elif isinstance(parent, ast.Subscript) and node is parent.slice:
        kind = _classify_subscript(parent, parents)
    elif isinstance(parent, ast.Dict) and node in parent.keys:
        kind = ("mapping write", parent.values[parent.keys.index(node)])  # type: ignore[arg-type]
    elif isinstance(parent, ast.Compare) and node is parent.left:
        if all(isinstance(op, _MEMBERSHIP_TESTS) for op in parent.ops):
            kind = ("read", None)
    elif isinstance(parent, ast.Call) and parent.args and parent.args[0] is node:
        kind = (_classify_call_argument(parent, parents), None)
    elif isinstance(parent, ast.Assign | ast.AnnAssign):
        # Only a statement of the module body binds a module constant; one in a
        # class body is reached as ``self.NAME`` and would escape the scan.
        if isinstance(parents.get(parent), ast.Module):
            kind = ("module constant", None)
    return kind


def _shared_uses(
    name: str,
    function: ast.AST,
    written: set[int],
    parents: dict[ast.AST, ast.AST],
) -> list[ast.AST]:
    """Uses of a detached local that would share its container.

    A deep copy stays detached only while nothing else holds it: it may be
    written as the member once, hashed, or copied again. A second write or any
    other use (``self.creds[e] = reg``, a call, an alias) shares the container.
    """

    bad: list[ast.AST] = []
    writes = 0
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        parent = parents.get(node)
        if id(node) in written:
            writes += 1
            if writes > 1:
                bad.append(node)
        elif not (
            isinstance(parent, ast.Call)
            and ast.unparse(parent.func) in _DIGEST_CALLS | {"copy.deepcopy"}
            and node in parent.args
        ):
            bad.append(node)
    return bad


def _occurrences(tree: ast.Module) -> list[ast.AST]:
    names = _member_names(tree)
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == _MEMBER:
            found.append(node)
        elif isinstance(node, ast.Constant | ast.Name) and _is_member_key(node, names):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                continue
            found.append(node)
    return found


def _receiver_problems(tree: ast.Module) -> tuple[int, list[str], list[str]]:
    """Return ``(mapping writes, unclassified uses, shared writes)`` of ``tree``."""

    parents = _parents(tree)
    occurrences = _occurrences(tree)
    kinds = [_classify(node, parents) for node in occurrences]
    writes = [
        (node, value)
        for node, (kind, value) in zip(occurrences, kinds, strict=True)
        if kind == "mapping write"
    ]
    unclassified = [
        f"line {node.lineno}: {ast.unparse(parents.get(node, node))}"
        for node, (kind, _value) in zip(occurrences, kinds, strict=True)
        if kind == "unclassified"
    ]
    written = {id(value) for _node, value in writes if value is not None}
    shared: list[str] = []
    for node, value in writes:
        function = _enclosing_function(node, parents)
        if (
            value is None
            or function is None
            or not _is_detached(value, function, parents)
        ):
            shared.append(f"line {node.lineno}: {ast.unparse(parents.get(node, node))}")
        elif isinstance(value, ast.Name):
            shared.extend(
                f"line {use.lineno}: {value.id} is used beside its write"
                for use in _shared_uses(value.id, function, written, parents)
            )
    return len(writes), unclassified, sorted(set(shared))


def test_every_receiver_write_of_the_registration_is_a_deep_copy() -> None:
    """R1: ``fcm_credentials`` written by the receiver is never shared state."""

    writes, unclassified, shared = _receiver_problems(_parse(_RECEIVER))
    # Two today: the bundle member and the top-level copy. An empty scan would
    # pass the checks below silently.
    assert writes >= 2, f"expected the receiver writes, found {writes}"
    assert not unclassified, (
        f"unclassified use of {_MEMBER!r}; extend the guard deliberately: "
        f"{unclassified}"
    )
    assert not shared, (
        "the receiver hands the entry a registration that is not a deep copy "
        f"(Auth/AGENTS.md, 'Detached copies'): {shared}"
    )


_CLEAN = """
import copy
K = "fcm_credentials"

class R:
    async def save(self, cache, entry_cache, bundle, data, creds):
        known = credential_member_digest(
            bundle.get("fcm_credentials") if bundle else None
        )
        cached = await cache.get("fcm_credentials")
        await entry_cache.set("fcm_credentials", creds)
        registration = copy.deepcopy(dict(creds))
        digest = credential_member_digest(registration)
        bundle["fcm_credentials"] = registration
        if "fcm_credentials" in data:
            data[K] = copy.deepcopy(registration)
        return known, cached, digest
"""

#: Forms that must fail, each a one-line change to ``_CLEAN``.
_BROKEN = {
    "nested write through a read": (
        '        cached = await cache.get("fcm_credentials")\n',
        '        bundle["fcm_credentials"]["gcm"] = creds\n',
    ),
    "read handed to receiver state": (
        '        cached = await cache.get("fcm_credentials")\n',
        '        self.creds["e"] = bundle.get("fcm_credentials")\n',
    ),
    "deep copy also kept by the receiver": (
        "        digest = credential_member_digest(registration)\n",
        "        self.creds['e'] = registration\n"
        "        digest = credential_member_digest(registration)\n",
    ),
    "deep copy written twice": (
        "            data[K] = copy.deepcopy(registration)\n",
        "            data[K] = registration\n",
    ),
    "class constant as key": (
        "class R:\n",
        'class R:\n    KEY = "fcm_credentials"\n',
    ),
    "cache recognised by name only": (
        '        await entry_cache.set("fcm_credentials", creds)\n',
        '        await self._bundle_cache.set("fcm_credentials", creds)\n',
    ),
    "shallow write": (
        '        bundle["fcm_credentials"] = registration\n',
        '        bundle["fcm_credentials"] = creds\n',
    ),
    "dict display without copy": (
        '        bundle["fcm_credentials"] = registration\n',
        '        bundle = {**bundle, "fcm_credentials": dict(creds)}\n',
    ),
}


def test_the_guard_accepts_the_clean_form() -> None:
    """The reference source passes, so every broken form below fails for its change."""

    writes, unclassified, shared = _receiver_problems(ast.parse(_CLEAN))
    assert (writes, unclassified, shared) == (2, [], [])


@pytest.mark.parametrize("label", sorted(_BROKEN))
def test_the_guard_rejects_a_shared_or_unknown_form(label: str) -> None:
    """Each form the reviews found escapes neither classification nor copy check."""

    old, new = _BROKEN[label]
    assert _CLEAN.count(old) == 1, label
    _writes, unclassified, shared = _receiver_problems(
        ast.parse(_CLEAN.replace(old, new))
    )
    assert unclassified or shared, label
