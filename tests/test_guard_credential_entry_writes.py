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
  instead. This guard fails when the fingerprint filters a member again or stops
  covering the secrets bundle.
* **R1, detached copies** (``Auth/AGENTS.md``, "Credential ownership across
  receiver, cache and entry"). The receiver edits its registration in place, so a
  registration it hands to ``entry.data`` must be a deep copy; a shared container
  would rewrite the entry behind Home Assistant's back. This guard fails when a
  write of ``fcm_credentials`` in ``Auth/fcm_receiver_ha.py`` takes a value that
  is not produced by ``copy.deepcopy``.

Why ``ast`` and a behaviour probe side by side: the behaviour probe pins today's
key set, but a filter keyed on a different member would pass it; the structural
check names the shape (a filtering comprehension, a member literal) whatever the
member is.

Detection limits, stated rather than implied. The copy check follows a value
through local names bound in the same function, nothing further: a value
returned by a helper, an attribute, or a name bound by a loop or ``with`` counts
as not detached and fails, which is the safe direction. Writes of
``fcm_credentials`` outside the receiver (the config flow, ``TokenCache``) are
out of scope; ``tests/test_token_cache_value_isolation.py`` covers the cache.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INTEGRATION = _REPO_ROOT / "custom_components" / "googlefindmy"
_INIT = _INTEGRATION / "__init__.py"
_RECEIVER = _INTEGRATION / "Auth" / "fcm_receiver_ha.py"
_MEMBER = "fcm_credentials"


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
        target = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == "_CREDENTIAL_KEYS":
            assert isinstance(value, ast.Tuple | ast.List), "unexpected shape"
            return [ast.unparse(element) for element in value.elts]
    raise AssertionError("_CREDENTIAL_KEYS not found in __init__.py")


def test_the_fingerprint_filters_no_member() -> None:
    """R2: ``_credential_fingerprint`` hashes every credential key in full."""

    function = _function(_parse(_INIT), "_credential_fingerprint")
    filters = [
        ast.unparse(node)
        for node in ast.walk(function)
        if isinstance(node, ast.comprehension) and node.ifs
    ]
    assert not filters, (
        "_credential_fingerprint filters a member; tell a writer apart by a "
        f"receipt (entry_self_write) instead: {filters}"
    )
    literals = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and node.value == _MEMBER
    ]
    assert not literals, (
        f"_credential_fingerprint names {_MEMBER!r} (lines {literals}); the "
        "full fingerprint must not single out a member"
    )


def test_the_fingerprint_covers_the_secrets_bundle() -> None:
    """R2: the bundle that carries the registration is a fingerprinted key."""

    assert "DATA_SECRET_BUNDLE" in _credential_keys(_parse(_INIT))


def test_a_registration_change_alone_moves_the_fingerprint() -> None:
    """R2, behaviour: a change of only the bundle's registration is visible."""

    from custom_components.googlefindmy import _credential_fingerprint
    from custom_components.googlefindmy.const import DATA_SECRET_BUNDLE

    before = {DATA_SECRET_BUNDLE: {"username": "u", _MEMBER: {"gcm": {"a": 1}}}}
    after = {DATA_SECRET_BUNDLE: {"username": "u", _MEMBER: {"gcm": {"a": 2}}}}
    assert _credential_fingerprint(before) != _credential_fingerprint(after)


def _is_deepcopy(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "deepcopy"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "copy"
    )


def _bindings(function: ast.AST, name: str) -> list[ast.AST]:
    """Every binding of ``name`` in ``function``.

    A plain ``name = value`` contributes its value. Any other binding (an
    unpacking, an augmented or annotated assignment, a loop or ``with`` target,
    a parameter, a walrus) contributes the binding node itself, which is never a
    ``copy.deepcopy`` call and therefore fails the check: not followed means not
    shown to be detached.
    """

    bound: list[ast.AST] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == name:
                        bound.append(node.value)
                elif _binds(target, name):
                    bound.append(node)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign | ast.NamedExpr):
            if _binds(node.target, name):
                bound.append(node)
        elif isinstance(node, ast.For | ast.AsyncFor):
            if _binds(node.target, name):
                bound.append(node)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None and _binds(node.optional_vars, name):
                bound.append(node)
        elif isinstance(node, ast.arg) and node.arg == name:
            bound.append(node)
    return bound


def _binds(target: ast.AST, name: str) -> bool:
    """Whether an assignment target binds ``name`` (subscripts do not)."""

    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Tuple | ast.List | ast.Starred):
        children = (
            target.elts if isinstance(target, ast.Tuple | ast.List) else [target.value]
        )
        return any(_binds(child, name) for child in children)
    return False


def _is_detached(value: ast.AST, function: ast.AST) -> bool:
    if _is_deepcopy(value):
        return True
    if isinstance(value, ast.Name):
        bound = _bindings(function, value.id)
        return bool(bound) and all(_is_deepcopy(node) for node in bound)
    return False


def _member_writes(tree: ast.Module) -> list[tuple[ast.AST, ast.Assign]]:
    writes: list[tuple[ast.AST, ast.Assign]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == _MEMBER
                ):
                    writes.append((function, node))
    return writes


def test_every_receiver_write_of_the_registration_is_a_deep_copy() -> None:
    """R1: ``fcm_credentials`` written by the receiver is never shared state."""

    writes = _member_writes(_parse(_RECEIVER))
    # Two today: the bundle member and the top-level copy. An empty scan would
    # pass the loop below silently.
    assert len(writes) >= 2, f"expected the receiver writes, found {len(writes)}"
    shared = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for function, node in writes
        if not _is_detached(node.value, function)
    ]
    assert not shared, (
        "the receiver hands the entry a registration that is not a deep copy "
        f"(Auth/AGENTS.md, 'Detached copies'): {shared}"
    )
