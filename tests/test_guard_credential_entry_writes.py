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
  constant, module constant bound to it, or keyword argument) is classified as a
  read (``.get`` argument, ``in`` test, subscript load), a token cache write
  (``<...cache>.set``, isolated by ``TokenCache`` itself and pinned in
  ``tests/test_token_cache_value_isolation.py``), or a mapping write (subscript
  store, dict display key, keyword argument). A mapping write must take a
  detached value; any other occurrence fails as unclassified.
* A value is detached when it is a ``copy.deepcopy(...)`` call, or a local name
  every binding of which in the enclosing function is a plain assignment of such
  a call. Every binding form of the grammar counts (assignment and unpacking
  targets, augmented and annotated assignment, walrus, loop, ``with`` and
  comprehension targets, parameters, ``except ... as``, ``match`` captures,
  imports, ``global``/``nonlocal``, nested definitions); any but the plain
  deepcopy assignment fails.

Detection limits, stated rather than implied. A key built at runtime (string
concatenation, a constant imported from another module) is not recognised as the
member. Writes of the member outside the receiver (the config flow,
``TokenCache``) are out of scope.
"""

from __future__ import annotations

import ast
import pathlib

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


def _classify_subscript(
    parent: ast.Subscript, parents: dict[ast.AST, ast.AST]
) -> tuple[str, ast.AST | None]:
    """Classify the member key used as the index of ``parent``."""

    if isinstance(parent.ctx, ast.Load):
        return "read", None
    statement = parents.get(parent)
    if isinstance(statement, ast.Assign) and parent in statement.targets:
        return "mapping write", statement.value
    if isinstance(statement, ast.AnnAssign) and statement.target is parent:
        return "mapping write", statement.value
    return "unclassified", None


def _classify_call_argument(parent: ast.Call) -> str:
    """Classify the member key passed as the first argument of ``parent``."""

    callee = parent.func
    if isinstance(callee, ast.Attribute):
        if callee.attr == "get":
            return "read"
        if callee.attr == "set" and ast.unparse(callee.value).endswith("cache"):
            return "cache write"
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
        if all(isinstance(op, ast.In | ast.NotIn) for op in parent.ops):
            kind = ("read", None)
    elif isinstance(parent, ast.Call) and parent.args and parent.args[0] is node:
        kind = (_classify_call_argument(parent), None)
    elif isinstance(parent, ast.Assign | ast.AnnAssign):
        if _enclosing_function(node, parents) is None:
            kind = ("module constant", None)
    return kind


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


def test_every_receiver_write_of_the_registration_is_a_deep_copy() -> None:
    """R1: ``fcm_credentials`` written by the receiver is never shared state."""

    tree = _parse(_RECEIVER)
    parents = _parents(tree)
    occurrences = _occurrences(tree)
    kinds = [_classify(node, parents) for node in occurrences]
    writes = [
        (node, value)
        for node, (kind, value) in zip(occurrences, kinds, strict=True)
        if kind == "mapping write"
    ]
    # Two today: the bundle member and the top-level copy. An empty scan would
    # pass the checks below silently.
    assert len(writes) >= 2, f"expected the receiver writes, found {len(writes)}"
    unclassified = [
        f"line {node.lineno}: {ast.unparse(parents.get(node, node))}"
        for node, (kind, _value) in zip(occurrences, kinds, strict=True)
        if kind == "unclassified"
    ]
    assert not unclassified, (
        f"unclassified use of {_MEMBER!r}; extend the guard deliberately: "
        f"{unclassified}"
    )
    shared = []
    for node, value in writes:
        function = _enclosing_function(node, parents)
        if (
            value is None
            or function is None
            or not _is_detached(value, function, parents)
        ):
            shared.append(f"line {node.lineno}: {ast.unparse(parents.get(node, node))}")
    assert not shared, (
        "the receiver hands the entry a registration that is not a deep copy "
        f"(Auth/AGENTS.md, 'Detached copies'): {shared}"
    )
