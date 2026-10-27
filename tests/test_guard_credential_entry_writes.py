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

Both checks follow the grammar rather than the forms seen so far and reject what
they cannot classify:

* The fingerprint body may only call the functions on ``_FINGERPRINT_CALLS`` and
  may not delete, filter in a comprehension, assign into a subscript or name the
  member, so a filter written as ``del`` or ``pop`` fails as an unknown statement
  or call. A behaviour probe pins that every credential key and every sampled
  bundle member moves the fingerprint.
* Every occurrence of the member key in ``Auth/fcm_receiver_ha.py`` is
  classified. The guard knows a key in four spellings only: a string constant
  (``"fcm_credentials"``), a keyword argument, a constant of ``const.py``
  reached by import (``from ..const import X``, ``const.X``, also behind a
  module alias ``C2 = const``) and a module name every binding of which in
  module scope is an assignment of one of these (``K = "fcm_credentials"``,
  ``K2 = K``). A constant of ``const.py`` that holds the member and is
  imported, or named as an attribute, in any other way fails
  (``except ImportError: FC = None``, ``del FC``, ``from ..const import *``,
  an import inside a function, ``import custom_components.googlefindmy``, an
  attribute of that name on another object such as ``self.FCM_CREDENTIALS``),
  and so does a known key that flows into a name the guard does not resolve
  (``K3 = K``); reflection on the module is not followed (see the limits
  below). Allowed kinds are a
  hash read (``.get`` or subscript load whose value goes straight into
  ``credential_member_digest``), an ``in`` test, a token cache read or write
  (``.get``/``.set`` on a receiver named in ``_CACHE_RECEIVERS``; ``TokenCache``
  detaches values itself, pinned in
  ``tests/test_token_cache_value_isolation.py``), or a mapping write (subscript
  store, dict display key, keyword argument). Any other occurrence fails as
  unclassified, so a read whose value is kept or written through
  (``bundle[K]["gcm"] = v``, ``self.creds[e] = bundle.get(K)``) fails too.
* Inside a function, a module name the function binds itself, in any binding
  form, is its local there; a comprehension target does not hide the module
  name.
* A mapping write must take a detached value: a ``copy.deepcopy(x)`` call with
  a single argument (a memo can return ``x`` itself), or a local name every
  binding of which in the enclosing function is a single-target assignment,
  annotated or not, of such a call. ``copy`` must be the module: every binding
  of the name anywhere in the file is ``import copy`` (a star import, a PEP 695
  type parameter or any other binding form fails), and every read of it calls
  ``copy.deepcopy``, so ``copy.deepcopy = f``, ``setattr(copy, ...)`` and
  ``copy.__dict__[...]`` fail. A file that binds ``dict`` fails too, because
  ``dict(x)`` then need not make a new container, and so does a file that
  reaches ``copy`` or ``dict`` another way (``sys.modules``, ``builtins``,
  ``__import__``, ``importlib``, ``import copy as c``, a store or ``setattr``
  of an attribute named ``deepcopy`` or ``dict``).
* Bindings of a local are counted in its own scope: the body of a nested
  ``def``, lambda or class belongs to that scope, while its name, decorators,
  defaults and bases do not; comprehension targets and a nested ``nonlocal`` or
  ``global`` of the name count (fail closed). Such a local may be written once
  and otherwise only hashed or copied again as the single argument of
  ``copy.deepcopy``; a second write or any other use, a memo included, would
  share it. One value written to several keys, or a write that shares its
  statement with another target (``keep = data[K] = copy.deepcopy(r)``), is
  shared and fails.
* The container a write goes into must be fresh (a dict display, ``dict(x)``
  or a deep copy, bound once, annotated or not) and local, and may only reach
  the entry: be written into by index, compared, put into a further such
  container, or handed to a call on ``_WRITER_CALLS``; an element may be read
  back only into ``credential_member_digest``. Kept on ``self``, returned, bound
  to a second name, read back or handed to any other call, it is shared and
  fails.
* ``_BROKEN`` pins forms that must fail, ``_ACCEPTED`` equivalent forms that
  must pass and ``_NOT_SEEN`` the key forms the guard does not see, each against
  a clean reference source; ``_DOCUMENTED_FORMS`` pins every form a docstring of
  this file names.

Detection limits, stated rather than implied. The guard protects against a
contributor who adds a write of the member without a copy; it is not built
against deliberate disguise. Not seen as a key, so a write keyed so is neither
checked nor counted: a key computed in any way (``+``, an f-string, a format, a
call, a subscript or slice, a class attribute), a module name with any binding
other than an assignment of a known key (``+=``, ``for``, ``global`` in a
function), a name bound in a class body, a name Python binds implicitly (``__doc__``),
and a key computed at runtime from a local or a parameter inside a function; a
local name bound to a module key is not followed, and a name imported from a
module other than ``const.py`` holds no value the guard sees. A t-string is not a
``str`` and so never the member as a key; ``t"fcm_credentials"`` still fails
as unclassified. The token cache is recognised by its receiver name, from a list
that is closed on purpose (``_CACHE_RECEIVERS``), and a writer on
``_WRITER_CALLS`` is trusted by its name; its body is not followed. Code in
another module that rebinds ``copy.deepcopy`` at runtime is outside what one
file can show; an object imported from ``copy`` is not followed, and reflection
(``globals()``, ``getattr``, a renamed ``sys``) is not followed. Writes of the
member outside the receiver (the config flow, ``TokenCache``) are out of scope.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import pathlib
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from typing import Protocol

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INTEGRATION = _REPO_ROOT / "custom_components" / "googlefindmy"
_INIT = _INTEGRATION / "__init__.py"
_RECEIVER = _INTEGRATION / "Auth" / "fcm_receiver_ha.py"
_CONST = _INTEGRATION / "const.py"
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

#: Calls a fresh container holding the member may be handed to. They store the
#: entry data and keep no reference of their own (V-5 (b)); closed like
#: ``_CACHE_RECEIVERS``, so another call fails instead of passing as a writer.
_WRITER_CALLS = frozenset(
    {"update", "async_update_entry", "hass.config_entries.async_update_entry"}
)

#: Scopes whose body does not run in the enclosing function.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: Scopes whose body has its own local names (``global`` aside).
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: PEP 695 type parameters, which bind a name as well.
_TYPE_PARAMS = (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)


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
    """``copy.deepcopy(x)`` with one argument; a memo can hand back ``x`` itself.

    That ``copy`` is the module is checked once per file
    (``_copy_is_the_module``), not here.
    """

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "deepcopy"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "copy"
        and len(node.args) == 1
        and not isinstance(node.args[0], ast.Starred)
        and not node.keywords
    )


def _is_fresh(node: ast.AST) -> bool:
    """A new outer container: a dict display, ``dict(...)`` or a deep copy."""

    if isinstance(node, ast.Dict) or _is_deepcopy(node):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and len(node.args) <= 1
        and not any(isinstance(arg, ast.Starred) for arg in node.args)
        and all(keyword.arg is not None for keyword in node.keywords)
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }


def _binds(node: ast.AST, name: str) -> bool:
    """Whether ``node`` binds ``name`` by itself, in one grammar form."""

    bound = _bound_by(node)
    # ``from x import *`` may bind any name.
    return name in bound or bound == ("*",)


def _bound_by(node: ast.AST) -> tuple[str, ...]:
    """The names ``node`` binds by itself; a star import binds the name star."""

    bound: tuple[str | None, ...] = ()
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Store | ast.Del):
            bound = (node.id,)
    elif isinstance(node, ast.arg):
        bound = (node.arg,)
    elif isinstance(
        node,
        ast.ExceptHandler
        | ast.MatchAs
        | ast.MatchStar
        | ast.FunctionDef
        | ast.AsyncFunctionDef
        | ast.ClassDef,
    ):
        bound = (node.name,)
    elif isinstance(node, ast.MatchMapping):
        bound = (node.rest,)
    elif isinstance(node, ast.alias):
        bound = ("*" if node.name == "*" else node.asname or node.name.split(".")[0],)
    elif isinstance(node, ast.Global | ast.Nonlocal):
        bound = tuple(node.names)
    return tuple(name for name in bound if name is not None)


def _outer_parts(node: ast.AST) -> list[ast.AST]:
    """The parts of a nested def, lambda or class evaluated in the enclosing scope."""

    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    parts: list[ast.AST] = []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        parts.extend(node.decorator_list)
        if node.returns is not None:
            parts.append(node.returns)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        parts.extend(node.args.defaults)
        parts.extend(d for d in node.args.kw_defaults if d is not None)
    return parts


def _parameter_names(arguments: ast.arguments) -> set[str]:
    names = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *(a for a in (arguments.vararg, arguments.kwarg) if a is not None),
    ]
    return {arg.arg for arg in names}


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes evaluated in ``scope`` itself, without the bodies of nested scopes.

    A nested def, lambda or class contributes its name, decorators, defaults and
    bases; its parameters and body belong to the nested scope. Comprehensions are
    walked in full on purpose, so a target such as
    ``[registration for registration in creds]`` counts (fail closed).
    """

    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        found.append(node)
        if isinstance(node, _NESTED_SCOPES):
            stack.extend(_outer_parts(node))
        else:
            stack.extend(ast.iter_child_nodes(node))
    return found


class _ScopeIndex:
    """Binding sites and loads of every name of one scope, each built in one walk.

    Following a chain of locals asked the whole function once per name and
    grew with the square of the chain (CA-GFMY-GUARD-CONTAINER-QUADRATISCH-001);
    the index answers each name from one walk. The order of the sites and loads
    is not part of the answer. A star import is no site: anywhere
    in the file it already fails the scan (``_copy_is_the_module``).
    """

    def __init__(self, scope: ast.AST) -> None:
        self._sites: dict[str, list[ast.AST]] = {}
        self._loads: dict[str, list[ast.Name]] = {}
        self._index_sites(scope)
        self._index_loads(scope)

    def _index_sites(self, scope: ast.AST) -> None:
        for node in _scope_nodes(scope):
            for name in _bound_by(node):
                self._sites.setdefault(name, []).append(node)
            if isinstance(node, _NESTED_SCOPES):
                # A nested ``nonlocal`` or ``global`` may rebind the local.
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Global | ast.Nonlocal):
                        for name in dict.fromkeys(inner.names):
                            self._sites.setdefault(name, []).append(inner)

    def _index_loads(self, scope: ast.AST) -> None:
        # A nested def or lambda with a parameter of a name reads its own
        # parameter: for those names only its outer parts count (``only``), for
        # all others its whole subtree (the parameters join ``skip``).
        stack: list[tuple[ast.AST, frozenset[str] | None, frozenset[str]]] = [
            (child, None, frozenset()) for child in ast.iter_child_nodes(scope)
        ]
        while stack:
            node, only, skip = stack.pop()
            if isinstance(node, _FUNCTION_SCOPES):
                params = frozenset(_parameter_names(node.args)) - skip
                if only is not None:
                    params &= only
                if params:
                    stack.extend((part, params, skip) for part in _outer_parts(node))
                    skip = skip | params
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and (only is None or node.id in only)
                and node.id not in skip
            ):
                self._loads.setdefault(node.id, []).append(node)
            stack.extend((child, only, skip) for child in ast.iter_child_nodes(node))

    def binding_sites(self, name: str) -> list[ast.AST]:
        """Every node that binds ``name`` in the scope itself, in any grammar form.

        A nested scope that declares the name ``nonlocal`` or ``global`` counts as
        a binding too, because it may rebind the local from inside.
        """

        return self._sites.get(name, [])

    def uses(self, name: str) -> list[ast.Name]:
        """Loads of ``name`` that read the local of the scope.

        A nested def or lambda with a parameter of that name reads its own
        parameter and is skipped; any other nested body is walked, because a
        closure there reads the local.
        """

        return self._loads.get(name, [])


def _bindings_anywhere(tree: ast.Module, name: str) -> list[ast.AST]:
    """Every binding of ``name`` in the file, type parameters and a star import included."""

    return [
        node
        for node in ast.walk(tree)
        if _binds(node, name) or (isinstance(node, _TYPE_PARAMS) and node.name == name)
    ]


def _copy_is_the_module(tree: ast.Module) -> bool:
    """``copy`` is the module: bound only by ``import copy``, read only to call ``copy.deepcopy``.

    Every binding anywhere in the file counts. Any other read (``copy.deepcopy = f``,
    ``setattr(copy, ...)``, ``copy.__dict__[...]``, ``copy._deepcopy_dispatch``)
    could replace what ``deepcopy`` does and fails as well.
    """

    sites = _bindings_anywhere(tree, "copy")
    if not sites or not all(
        isinstance(site, ast.alias) and site.name == "copy" and site.asname is None
        for site in sites
    ):
        return False
    parents = _parents(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == "copy"
            and isinstance(node.ctx, ast.Load)
        ):
            parent = parents.get(node)
            if not (
                isinstance(parent, ast.Attribute)
                and isinstance(parent.ctx, ast.Load)
                and parent.attr == "deepcopy"
                and isinstance(parents.get(parent), ast.Call)
                and parents[parent].func is parent  # type: ignore[attr-defined]
            ):
                return False
    return True


#: Ways to reach a module object other than by its import name; with any of
#: them a file can replace ``copy.deepcopy`` or ``dict`` behind the checks.
_MODULE_HANDLES = frozenset({"builtins", "__builtins__", "__import__", "sys.modules"})

#: Attributes whose replacement changes what a detached copy is.
_COPY_ATTRIBUTES = frozenset({"deepcopy", "dict"})


def _copy_reached_another_way(tree: ast.Module) -> bool:
    """True if the file may replace ``copy.deepcopy`` or ``dict`` without naming
    ``copy`` (Round 6 N4): through ``sys.modules``, ``builtins``,
    ``__import__``, an alias of ``copy``, ``importlib``, or a store, ``del``,
    ``setattr`` or ``delattr`` of an attribute named ``deepcopy`` or ``dict``
    on any object."""

    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            root = node.name.split(".")[0]
            if root in {"builtins", "importlib"} or (
                root == "copy"
                and (node.name != "copy" or node.asname not in (None, "copy"))
            ):
                return True
        elif isinstance(node, ast.Name | ast.Attribute):
            if _dotted(node) in _MODULE_HANDLES:
                return True
            if (
                isinstance(node, ast.Attribute)
                and not isinstance(node.ctx, ast.Load)
                and node.attr in _COPY_ATTRIBUTES
            ):
                return True
        elif (
            isinstance(node, ast.Call)
            and _dotted(node.func) in {"setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _COPY_ATTRIBUTES
        ):
            return True
    return False


def _assigned_value(site: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The value of a single-target assignment, annotated or not, binding ``site``."""

    statement = parents.get(site)
    if not (isinstance(site, ast.Name) and isinstance(site.ctx, ast.Store)):
        return None
    if isinstance(statement, ast.Assign) and statement.targets == [site]:
        return statement.value
    if isinstance(statement, ast.AnnAssign) and statement.target is site:
        return statement.value
    return None


def _is_detached(
    value: ast.AST, index: _ScopeIndex, parents: dict[ast.AST, ast.AST]
) -> bool:
    if _is_deepcopy(value):
        return True
    if not isinstance(value, ast.Name):
        return False
    sites = index.binding_sites(value.id)
    if not sites:
        return False
    for site in sites:
        assigned = _assigned_value(site, parents)
        if assigned is None or not _is_deepcopy(assigned):
            return False
    return True


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for a chain of names and attributes, else ``None``."""

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _bound_identifiers(node: ast.AST) -> list[str]:
    """Names a node binds by identifier rather than by an ``ast.Name`` target."""

    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _function_locals(function: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """``(locals, globals)`` of a def or lambda body (R7-F8 (ii)).

    Locals are the names the function itself binds, in any binding form of its
    own body, ``nonlocal`` included. A comprehension target does not count, so
    the module name stays visible (Review CA-F4); nested ``global`` or
    ``nonlocal`` do not count either.
    """

    arguments = function.args  # type: ignore[attr-defined]
    bound: set[str] = set(_parameter_names(arguments))
    declared: set[str] = set()
    body = function.body  # type: ignore[attr-defined]
    stack: list[ast.AST] = list(body) if isinstance(body, list) else [body]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Global):
            declared.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, _NESTED_SCOPES):
            if not isinstance(node, ast.Lambda):
                bound.add(node.name)
            stack.extend(_outer_parts(node))
        elif isinstance(node, ast.comprehension):
            stack.append(node.iter)
            stack.extend(node.ifs)
        else:
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                bound.add(node.id)
            elif isinstance(node, ast.alias) and node.name != "*":
                bound.add(node.asname or node.name.split(".")[0])
            bound.update(_bound_identifiers(node))
            stack.extend(ast.iter_child_nodes(node))
    return frozenset(bound - declared), frozenset(declared)


def _body_ids(scope: ast.AST) -> frozenset[int]:
    """Ids of the statements of a def body or of a lambda body expression."""

    if isinstance(scope, ast.Lambda):
        return frozenset({id(scope.body)})
    return frozenset(id(statement) for statement in scope.body)  # type: ignore[attr-defined]


def _cached[V](cache: dict[int, V], scope: ast.AST, build: Callable[[ast.AST], V]) -> V:
    """``build(scope)``, built once per scope."""

    if id(scope) not in cache:
        cache[id(scope)] = build(scope)
    return cache[id(scope)]


def _local_reads(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> frozenset[int]:
    """Ids of the ``Name`` loads that read a local or parameter, not a module name.

    The innermost enclosing def or lambda that binds the name or declares it
    ``global`` decides; class bodies are skipped, as Python skips them for the
    functions inside.
    """

    scopes: dict[int, tuple[frozenset[str], frozenset[str]]] = {}
    # One set per function: a search of the body per read grew with the square
    # of a long body (800 statements: 3.9 times the work of 400).
    bodies: dict[int, frozenset[int]] = {}
    found: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        child, current = node, parents.get(node)
        while current is not None:
            if isinstance(current, _FUNCTION_SCOPES) and id(child) in _cached(
                bodies, current, _body_ids
            ):
                if id(current) not in scopes:
                    scopes[id(current)] = _function_locals(current)
                bound, declared = scopes[id(current)]
                if node.id in declared:
                    break
                if node.id in bound:
                    found.add(id(node))
                    break
            child, current = current, parents.get(current)
    return frozenset(found)


#: The strings a module name or ``const`` path may hold.
Values = frozenset[str]


class _Keys(Protocol):
    """What the scan needs to know about keys: which nodes may be one, which is
    the member, and which keys of a write the resolver cannot bound."""

    key_nodes: tuple[type[ast.AST], ...]

    def is_member(self, node: ast.AST) -> bool: ...

    def unknown_keys(self, tree: ast.Module) -> list[ast.AST]: ...


#: Builds the key resolver of one file from its tree, parents, local reads and
#: the values of ``const.py``.
KeysFactory = Callable[
    [ast.Module, dict[ast.AST, ast.AST], frozenset[int], Mapping[str, Values]], _Keys
]


def _module_binding_sites(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Every binding site of every module name, collected in one walk.

    The same sites as ``_ScopeIndex(tree).binding_sites(name)``, but a
    ``nonlocal`` in a
    nested scope is left out, because it never names a module variable, and a
    star import is a site of every name.
    """

    sites: dict[str, list[ast.AST]] = {}
    stars: list[ast.AST] = []
    for node in _scope_nodes(tree):
        bound = _bound_by(node)
        if bound == ("*",):
            stars.append(node)
            continue
        for name in bound:
            sites.setdefault(name, []).append(node)
        if isinstance(node, _NESTED_SCOPES):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Global):
                    for name in dict.fromkeys(inner.names):
                        sites.setdefault(name, []).append(inner)
    for found in sites.values():
        found.extend(stars)
    return sites


def _import_site(
    site: ast.AST, parents: dict[ast.AST, ast.AST], constants: Mapping[str, Values]
) -> tuple[Values | None, str | None]:
    """``(values, const handle)`` of an import binding, ``(None, None)`` otherwise.

    ``from ..const import X`` gives the values of ``X``; an import of the module
    ``const`` itself, under any name, gives the handle of ``const.X``.
    """

    statement = parents.get(site)
    if not isinstance(site, ast.alias) or not _at_module_level(site, parents):
        return None, None
    if isinstance(statement, ast.ImportFrom):
        if (statement.module or "").split(".")[-1] == "const":
            return constants.get(site.name), None
        if site.name == "const":
            return None, site.asname or site.name
    if isinstance(statement, ast.Import) and site.name.split(".")[-1] == "const":
        return None, site.asname or site.name
    return None, None


def _assigned_site(site: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.expr | None:
    """The value of an assignment at module level that binds ``site`` as one of
    its targets (``K = K2 = "fcm_credentials"`` gives both names the string)."""

    statement = parents.get(site)
    if not isinstance(site, ast.Name) or not _at_module_level(site, parents):
        return None
    if isinstance(statement, ast.Assign) and site in statement.targets:
        return statement.value
    if isinstance(statement, ast.AnnAssign) and statement.target is site:
        return statement.value
    return None


class _LiteralKeys:
    """``V-10`` (c): a key is a string constant, a ``const.py`` constant reached
    by import, or a module name every module-scope binding of which is an
    assignment of one of these. Nothing is folded; any other expression is no
    key the guard sees. A ``const.py`` constant that holds the member must be
    reached in one of these spellings; ``unknown_keys`` reports every other
    place."""

    key_nodes: tuple[type[ast.AST], ...] = (ast.Constant, ast.Name, ast.Attribute)

    def __init__(
        self,
        tree: ast.Module,
        parents: dict[ast.AST, ast.AST],
        local: frozenset[int],
        constants: Mapping[str, Values],
    ) -> None:
        self.local = local
        self.parents = parents
        self.handles: set[str] = set()
        self.constants = constants
        bindings = _module_binding_sites(tree)
        sites: dict[str, list[ast.expr | Values | None]] = {}
        rebound: set[str] = set()
        for name, found_sites in bindings.items():
            found: list[ast.expr | Values | None] = []
            for site in found_sites:
                values, handle = _import_site(site, parents, constants)
                if handle is not None:
                    self.handles.add(handle)
                    found.append(frozenset())
                elif values is not None:
                    found.append(values)
                else:
                    found.append(_assigned_site(site, parents))
            sites[name] = found
            if not all(isinstance(site, ast.alias) for site in found_sites):
                rebound.add(name)
        # A handle whose root is bound other than by an import may be anything.
        self.handles = {h for h in self.handles if h.split(".")[0] not in rebound}
        self._alias_handles(bindings, parents)
        self.names: dict[str, Values] = {}
        self._resolve(sites)

    def _alias_handles(
        self, bindings: Mapping[str, list[ast.AST]], parents: dict[ast.AST, ast.AST]
    ) -> None:
        """A module name every binding of which assigns a handle is a handle too
        (``C2 = const``).

        A worklist: a name waits for the handles its bindings name and becomes
        one when the last of them does, so a chain of aliases is resolved in
        one pass in any order (R10-F2).
        """

        waiting: dict[str, set[str]] = {}
        readers: dict[str, list[str]] = {}
        for name, found_sites in bindings.items():
            if name in self.handles or not found_sites:
                continue
            paths: set[str] = set()
            for site in found_sites:
                value = _assigned_site(site, parents)
                path = None if value is None else _dotted(value)
                if path is None:
                    break
                paths.add(path)
            else:
                needed = paths - self.handles
                if not needed:
                    self.handles.add(name)
                    continue
                waiting[name] = needed
                for path in needed:
                    readers.setdefault(path, []).append(name)
        # Each handle, an import or an alias found above, releases its readers.
        ready = list(self.handles)
        while ready:
            path = ready.pop()
            for name in readers.pop(path, ()):
                needed = waiting[name]
                needed.discard(path)
                if not needed and name not in self.handles:
                    self.handles.add(name)
                    ready.append(name)

    def _resolve(self, sites: Mapping[str, list[ast.expr | Values | None]]) -> None:
        """Fill ``names``: the union of what every binding gives a name.

        A name is resolved once every binding is a string constant, a
        ``const.X`` path or a resolved name; a name with any other binding, or
        one that reads a name on a cycle (``A = B; B = A``), stays unresolved.
        The names are resolved in the order of their dependencies with a
        worklist, not by recursion, so a chain of any length is resolved in one
        pass (R10-F2).
        """

        partial: dict[str, Values] = {}
        waiting: dict[str, set[str]] = {}
        readers: dict[str, list[str]] = {}
        ready: list[str] = []
        for name, found in sites.items():
            values: Values = frozenset()
            needed: set[str] = set()
            for site in found:
                if isinstance(site, ast.Name) and site.id in sites:
                    needed.add(site.id)
                    continue
                direct = self._direct(site)
                if direct is None:
                    break
                values |= direct
            else:
                partial[name] = values
                waiting[name] = needed
                for read in needed:
                    readers.setdefault(read, []).append(name)
                if not needed:
                    ready.append(name)
        while ready:
            name = ready.pop()
            self.names[name] = partial[name]
            for reader in readers.get(name, ()):
                partial[reader] |= partial[name]
                waiting[reader].discard(name)
                if not waiting[reader]:
                    ready.append(reader)

    def _direct(self, site: ast.expr | Values | None) -> Values | None:
        """The strings of a binding that reads no module name, ``None`` if it is
        no key: an import's values, a string constant or ``const.X``."""

        if isinstance(site, frozenset):
            return site
        if isinstance(site, ast.Constant) and isinstance(site.value, str):
            return frozenset({site.value})
        if isinstance(site, ast.Attribute):
            return self._path(site)
        return None

    def _path(self, node: ast.Attribute) -> Values | None:
        """``const.X`` behind an import of ``const``."""

        path = _dotted(node.value)
        if path is None or path not in self.handles:
            return None
        return self.constants.get(node.attr)

    def values(self, node: ast.AST) -> Values | None:
        """The strings ``node`` holds as a key, ``None`` if it is no known key."""

        if isinstance(node, ast.Constant):
            return frozenset({node.value}) if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return None if id(node) in self.local else self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            root = _module_root(node)
            if root is None or id(root) in self.local:
                return None
            return self._path(node)
        return None

    def is_member(self, node: ast.AST) -> bool:
        values = self.values(node)
        return values is not None and _MEMBER in values

    def unknown_keys(self, tree: ast.Module) -> list[ast.AST]:
        """Places where a ``const.py`` constant holding the member is imported,
        or named as an attribute, in a way this resolver does not follow; each
        fails the scan: an import into a name that is no key
        (``except ImportError: FC = None``, ``del FC``), an import inside a
        function, a star import (``from ..const import *``), and an attribute
        of that name other than ``const.X`` behind a resolved handle
        (``import custom_components.googlefindmy``, ``self.FCM_CREDENTIALS``).
        Reflection on the module is not followed."""

        members = {name for name, values in self.constants.items() if _MEMBER in values}
        if not members:
            return []
        found: list[ast.AST] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[-1] == "const"
            ):
                for alias in node.names:
                    if alias.name == "*":
                        found.append(node)
                    elif alias.name in members and not (
                        _at_module_level(alias, self.parents)
                        and _MEMBER in self.names.get(alias.asname or alias.name, ())
                    ):
                        found.append(alias)
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in members
                and not (isinstance(node.ctx, ast.Load) and self.is_member(node))
            ):
                found.append(node)
        return found


def _module_root(node: ast.AST) -> ast.Name | None:
    """The name at the root of ``a.b.c``, else ``None``."""

    while isinstance(node, ast.Attribute):
        node = node.value
    return node if isinstance(node, ast.Name) else None


@functools.cache
def _project_constants() -> dict[str, Values]:
    """Values of the names of ``const.py``, by the rule of ``_LiteralKeys``."""

    tree = _parse(_CONST)
    parents = _parents(tree)
    keys = _LiteralKeys(tree, parents, _local_reads(tree, parents), {})
    return dict(keys.names)


def _at_module_level(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _NESTED_SCOPES):
            return False
        current = parents.get(current)
    return True


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
        and _dotted(parent.func) in _DIGEST_CALLS
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
        if _dotted(callee.value) in _CACHE_RECEIVERS:
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
        # Only a plain name bound at module level is a module constant (and
        # resolved as a key by ``_LiteralKeys``); one in a class body is
        # reached as ``self.NAME`` and an attribute target as ``obj.NAME``, both
        # of which would escape the scan.
        if _at_module_level(parent, parents) and all(
            isinstance(target, ast.Name)
            for target in (
                parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            )
        ):
            kind = ("module constant", None)
    return kind


def _has_other_targets(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True for a subscript store that shares its statement with another target.

    ``keep = data[K] = copy.deepcopy(r)`` binds one copy to the key and to
    ``keep``; the copy is fresh, but it is no longer the entry's alone.
    """

    parent = parents.get(node)
    statement = parents.get(parent) if parent is not None else None
    return (
        isinstance(parent, ast.Subscript)
        and isinstance(statement, ast.Assign)
        and len(statement.targets) > 1
    )


def _shared_uses(
    name: str,
    index: _ScopeIndex,
    parents: dict[ast.AST, ast.AST],
    written: set[int],
) -> list[ast.AST]:
    """Uses of a detached local that would share its container.

    A deep copy stays detached only while nothing else holds it: it may be
    written as the member once, hashed, or copied again as the single argument
    of ``copy.deepcopy``. A second write or any other use (``self.creds[e] =
    reg``, a call, an alias, a memo) shares the container.
    """

    bad: list[ast.AST] = []
    writes = 0
    for node in index.uses(name):
        parent = parents.get(node)
        if id(node) in written:
            writes += 1
            if writes > 1:
                bad.append(node)
        elif not (
            isinstance(parent, ast.Call)
            and (
                (_dotted(parent.func) in _DIGEST_CALLS and node in parent.args)
                or (_is_deepcopy(parent) and parent.args[0] is node)
            )
        ):
            bad.append(node)
    return bad


def _escape_step(
    expr: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[str | None, str | None]:
    """``(problem, local)`` for one container expression; both ``None`` when it
    reaches the entry.

    It may be handed to a call on ``_WRITER_CALLS``, be a value of a further
    display, or be bound by a single-target assignment to a local name or into
    a local container, which is then returned to be followed in turn.
    """

    parent = parents.get(expr)
    while isinstance(parent, ast.Dict) and expr in parent.values:
        expr, parent = parent, parents.get(parent)
    if isinstance(parent, ast.keyword):
        call = parents.get(parent)
        if isinstance(call, ast.Call) and _dotted(call.func) in _WRITER_CALLS:
            return None, None
    elif isinstance(parent, ast.Call):
        if expr in parent.args and _dotted(parent.func) in _WRITER_CALLS:
            return None, None
    elif (
        isinstance(parent, ast.Assign)
        and parent.value is expr
        and len(parent.targets) == 1
    ):
        target = parent.targets[0]
        if isinstance(target, ast.Name):
            return None, target.id
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            return None, target.value.id
    line = getattr(expr, "lineno", 0)
    return f"line {line}: the container {_describe(expr)[:60]} is shared", None


def _expression_escapes(
    expr: ast.AST,
    index: _ScopeIndex,
    parents: dict[ast.AST, ast.AST],
    seen: set[str],
) -> list[str]:
    """Where a container expression goes; empty when it only reaches the entry."""

    problem, local = _escape_step(expr, parents)
    if local is not None:
        return _name_escapes(local, index, parents, seen)
    return [] if problem is None else [problem]


def _name_escapes(
    name: str,
    index: _ScopeIndex,
    parents: dict[ast.AST, ast.AST],
    seen: set[str],
) -> list[str]:
    """Problems of a local container: a binding that is not fresh, or a use that shares it.

    A local the container is bound to is followed depth first with an explicit
    stack, not by recursion, so a chain of locals of any length is followed
    (R10-F2); the problems keep the order of a recursive walk.
    """

    problems: list[str] = []
    stack: list[tuple[str, Iterator[ast.Name]]] = []

    def enter(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        sites = index.binding_sites(name)
        problems.extend(
            f"line {getattr(site, 'lineno', 0)}: the container {name} is not a fresh local"
            for site in sites
            if (assigned := _assigned_value(site, parents)) is None
            or not _is_fresh(assigned)
        )
        if not sites:
            problems.append(f"the container {name} is not bound in the function")
        stack.append((name, iter(index.uses(name))))

    enter(name)
    while stack:
        current, uses = stack[-1]
        use = next(uses, None)
        if use is None:
            stack.pop()
            continue
        parent = parents.get(use)
        if isinstance(parent, ast.Subscript) and parent.value is use:
            if isinstance(parent.ctx, ast.Load) and not _feeds_the_digest(
                parent, parents
            ):
                problems.append(
                    f"line {use.lineno}: an element of the container {current} is read back"
                )
            continue
        if isinstance(parent, ast.Compare):
            continue
        problem, local = _escape_step(use, parents)
        if local is not None:
            enter(local)
        elif problem is not None:
            problems.append(problem)
    return problems


def _container_problems(
    node: ast.AST,
    index: _ScopeIndex,
    parents: dict[ast.AST, ast.AST],
    seen: set[str],
) -> list[str]:
    """R4-F2: the container a write goes into must be fresh and reach only the entry.

    ``seen`` holds the locals already followed in the function. A local's
    problems do not depend on the write that reached it, so each write of the
    function shares it and a chain is walked once (R11-F1).
    """

    parent = parents.get(node)
    if isinstance(node, ast.keyword):
        if isinstance(parent, ast.Call) and _is_fresh(parent):
            return _expression_escapes(parent, index, parents, seen)
        return [f"line {node.lineno}: written into a call that is not a fresh dict"]
    if isinstance(parent, ast.Subscript):
        if isinstance(parent.value, ast.Name):
            return _name_escapes(parent.value.id, index, parents, seen)
        return [
            f"line {parent.lineno}: the container {_describe(parent.value)} is not a local"
        ]
    if isinstance(parent, ast.Dict):
        return _expression_escapes(parent, index, parents, seen)
    return [f"line {getattr(node, 'lineno', 0)}: unknown container"]


def _describe(node: ast.AST) -> str:
    """``ast.unparse`` of ``node``, or its size when it is too deep for that."""

    size = sum(1 for _ in ast.walk(node))
    return ast.unparse(node) if size <= 200 else f"<expression of {size} nodes>"


def _is_outermost_key(
    node: ast.AST, parents: dict[ast.AST, ast.AST], keys: _Keys
) -> bool:
    """``node`` is not part of a larger expression that is the member key as well."""

    parent = parents.get(node)
    while isinstance(parent, ast.BinOp | ast.JoinedStr | ast.FormattedValue):
        if not isinstance(parent, ast.FormattedValue) and keys.is_member(parent):
            return False
        parent = parents.get(parent)
    return True


def _occurrences(
    tree: ast.Module, parents: dict[ast.AST, ast.AST], keys: _Keys
) -> list[ast.AST]:
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            if node.arg == _MEMBER:
                found.append(node)
        elif (
            isinstance(node, keys.key_nodes)
            and not (
                isinstance(node, ast.Name | ast.Attribute)
                and not isinstance(node.ctx, ast.Load)
            )
            and keys.is_member(node)
            and _is_outermost_key(node, parents, keys)
        ):
            found.append(node)
    return found


def _names_stay_keys(
    node: ast.AST, parents: dict[ast.AST, ast.AST], keys: _Keys
) -> bool:
    """A module constant is only fine when every name it binds is a key again.

    ``K3: str`` beside ``K3 = K`` makes ``K3`` no key the guard sees, so the
    member must not flow into such a name (fail closed).
    """

    statement = parents[node]
    targets = (
        statement.targets if isinstance(statement, ast.Assign) else [statement.target]  # type: ignore[attr-defined]
    )
    return all(
        keys.is_member(ast.Name(id=target.id, ctx=ast.Load())) for target in targets
    )


def _receiver_problems(
    tree: ast.Module,
    constants: Mapping[str, Values | str] | None = None,
    keys_factory: KeysFactory = _LiteralKeys,
) -> tuple[int, list[str], list[str]]:
    """Return ``(mapping writes, unclassified uses, shared writes)`` of ``tree``.

    ``constants`` stands for ``const.py``; the default reads the real one.
    ``keys_factory`` decides which expressions are keys (``_LiteralKeys``).
    """

    parents = _parents(tree)
    if constants is None:
        known: Mapping[str, Values] = _project_constants()
    else:
        known = {
            name: frozenset({value}) if isinstance(value, str) else value
            for name, value in constants.items()
        }
    local = _local_reads(tree, parents)
    keys = keys_factory(tree, parents, local, known)
    occurrences = _occurrences(tree, parents, keys)

    kinds = [_classify(node, parents) for node in occurrences]
    writes = [
        (node, value)
        for node, (kind, value) in zip(occurrences, kinds, strict=True)
        if kind == "mapping write"
    ]
    unclassified = [
        f"line {node.lineno}: {_describe(parents.get(node, node))}"  # type: ignore[attr-defined]
        for node, (kind, _value) in zip(occurrences, kinds, strict=True)
        if kind == "unclassified"
        or (kind == "module constant" and not _names_stay_keys(node, parents, keys))
    ] + [
        f"line {node.lineno}: unknown key {_describe(node)}"  # type: ignore[attr-defined]
        for node in keys.unknown_keys(tree)
    ]
    written = {id(value) for _node, value in writes if value is not None}
    targets_per_value = Counter(id(value) for _node, value in writes)
    shared: list[str] = []
    if writes and not _copy_is_the_module(tree):
        shared.append("copy is not only bound by 'import copy' and called as deepcopy")
    if writes and _copy_reached_another_way(tree):
        shared.append(
            "copy or dict is reached other than by its name and may be replaced"
        )
    if writes and _bindings_anywhere(tree, "dict"):
        shared.append("dict is bound in the module, so dict(x) may not be a new dict")
    # One index and one set of followed locals per function, shared by its writes.
    scopes: dict[int, tuple[_ScopeIndex, set[str]]] = {}
    for node, value in writes:
        line = node.lineno  # type: ignore[attr-defined]
        if targets_per_value[id(value)] > 1:
            shared.append(f"line {line}: one value written to several keys")
            continue
        if _has_other_targets(node, parents):
            shared.append(f"line {line}: one value bound to several targets")
            continue
        function = _enclosing_function(node, parents)
        if function is not None and id(function) not in scopes:
            scopes[id(function)] = (_ScopeIndex(function), set())
        if (
            value is None
            or function is None
            or not _is_detached(value, scopes[id(function)][0], parents)
        ):
            shared.append(f"line {line}: {_describe(parents.get(node, node))}")
            continue
        index, seen = scopes[id(function)]
        if isinstance(value, ast.Name):
            shared.extend(
                f"line {use.lineno}: {value.id} is used beside its write"
                for use in _shared_uses(value.id, index, parents, written)
            )
        shared.extend(_container_problems(node, index, parents, seen))
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
from custom_components.googlefindmy import const
from custom_components.googlefindmy.const import FCM_CREDENTIALS
K = "fcm_credentials"
K2 = K

class R:
    async def save(self, cache, entry_cache, bundle, data, creds, update, entry):
        known = credential_member_digest(
            bundle.get("fcm_credentials") if bundle else None
        )
        cached = await cache.get("fcm_credentials")
        await entry_cache.set("fcm_credentials", creds)
        registration = copy.deepcopy(dict(creds))
        digest = credential_member_digest(registration)
        updated_bundle = dict(bundle)
        updated_bundle["fcm_credentials"] = registration
        updated_data = dict(data)
        updated_data["bundle"] = updated_bundle
        if "fcm_credentials" in updated_data:
            updated_data[K2] = copy.deepcopy(registration)
        if updated_data == dict(data):
            return known, cached, digest
        update(entry, data=updated_data)
        return known, cached, digest
"""

#: Constants the snippets resolve as if they were in ``const.py``; the real
#: ``const.py`` has no constant for the member (pinned by the receiver test).
_SNIPPET_CONSTANTS = {"FCM_CREDENTIALS": _MEMBER}

#: Forms that must fail, each a one-place change to ``_CLEAN``.
_BROKEN: dict[str, tuple[str, ...]] = {
    "known key copied to a name with another binding": (
        "K2 = K\n",
        "K2 = K\nK3: str\nK3 = K\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
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
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K] = registration\n",
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
        '        updated_bundle["fcm_credentials"] = registration\n',
        '        updated_bundle["fcm_credentials"] = creds\n',
    ),
    "key bound to an attribute at module level": (
        "K2 = K\n",
        "K2 = K\nSETTINGS.key = K\n",
    ),
    "write through a module alias of the key": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K2] = creds\n",
    ),
    "one deep copy written to two keys": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        '            updated_bundle["fcm_credentials"] = updated_data[K2] = '
        "copy.deepcopy(registration)\n",
    ),
    "one deep copy bound to two names": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        registration = alias = copy.deepcopy(dict(creds))\n"
        "        updated_data[K] = alias\n",
    ),
    "deep copy also bound to an attribute in one statement": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            self.keep = updated_data[K2] = copy.deepcopy(registration)\n",
    ),
    "deep copy also bound to a local in one statement": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K2] = keep = copy.deepcopy(registration)\n",
    ),
    "deepcopy with a memo": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K] = copy.deepcopy(registration, {})\n",
    ),
    "copy shadowed by a parameter": (
        "    async def save(self, cache, entry_cache, bundle, data, creds, update, entry):\n",
        "    async def save(\n"
        "        self, cache, entry_cache, bundle, data, creds, update, entry, copy\n"
        "    ):\n",
    ),
    "dict display without copy": (
        '        updated_bundle["fcm_credentials"] = registration\n',
        '        updated_bundle = {**bundle, "fcm_credentials": dict(creds)}\n',
    ),
    "const attribute as key": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[const.FCM_CREDENTIALS] = creds\n",
    ),
    "name imported from const as key": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[FCM_CREDENTIALS] = creds\n",
    ),
    # R4-F3: copy rebound other than by a name binding.
    "star import may rebind copy": (
        "import copy\n",
        "import copy\nfrom helpers import *\n",
    ),
    "copy.deepcopy reassigned": (
        "import copy\n",
        "import copy\ncopy.deepcopy = lambda value: value\n",
    ),
    "copy.deepcopy replaced through setattr": (
        "import copy\n",
        'import copy\nsetattr(copy, "deepcopy", print)\n',
    ),
    # R4-F4: PEP 695 type parameters bind a name.
    "copy bound as a function type parameter": (
        "    async def save(self, cache, entry_cache, bundle, data, creds, update, entry):\n",
        "    async def save[copy](\n"
        "        self, cache, entry_cache, bundle, data, creds, update, entry\n"
        "    ):\n",
    ),
    "copy bound as a class type parameter": (
        "class R:\n",
        "class R[copy]:\n",
    ),
    # R4-F5: a binding in a nested scope is not one of the enclosing function.
    "deep copy bound only in a nested def": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        def inner():\n            registration = copy.deepcopy(dict(creds))\n",
    ),
    "deep copy bound only in a nested class body": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        class Holder:\n"
        "            registration = copy.deepcopy(dict(creds))\n",
    ),
    # R4-F2: the container around the copy is shared (V-5 (b)).
    "container kept by the receiver after the write": (
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = updated_bundle\n'
        "        self.last = updated_bundle\n",
    ),
    "display container kept by the receiver": (
        '        updated_bundle["fcm_credentials"] = registration\n',
        '        updated_bundle = {"fcm_credentials": registration}\n'
        "        self.last = updated_bundle\n",
    ),
    "container bound to a second name": (
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = updated_bundle\n'
        "        alias = updated_bundle\n",
    ),
    "container returned": (
        "        update(entry, data=updated_data)\n",
        "        update(entry, data=updated_data)\n        return updated_data\n",
    ),
    "outer container kept by the receiver": (
        "        update(entry, data=updated_data)\n",
        "        update(entry, data=updated_data)\n        self.last = updated_data\n",
    ),
    "container handed to a call outside the writer list": (
        "        update(entry, data=updated_data)\n",
        "        store(entry, data=updated_data)\n",
    ),
    "container handed positionally to a call outside the writer list": (
        "        update(entry, data=updated_data)\n",
        "        store(entry, updated_data)\n",
    ),
    # Round 5: module blocks, rebinding, reads back, memo, dict, full path.
    "key constant bound in a module-level for block": (
        "K2 = K\n",
        "K2 = K\nfor _ in range(1):\n    K3 = 'fcm_credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key constant rebound to the member": (
        "K2 = K\n",
        "K2 = K\nK3 = 'other'\nK3 = 'fcm_credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "fully qualified const constant as key": (
        "import copy\n",
        "import copy\nimport custom_components.googlefindmy.const\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[custom_components.googlefindmy.const.FCM_CREDENTIALS]"
        " = creds\n",
    ),
    "copy patched through its __dict__": (
        "import copy\n",
        "import copy\ncopy.__dict__['deepcopy'] = lambda value: value\n",
    ),
    "copy dispatch table patched": (
        "import copy\n",
        "import copy\ncopy._deepcopy_dispatch[dict] = lambda x, memo, deep: x\n",
    ),
    "detached local handed to deepcopy as a memo": (
        "        digest = credential_member_digest(registration)\n",
        "        digest = credential_member_digest(registration)\n"
        "        self.junk = copy.deepcopy(self.creds, registration)\n",
    ),
    "dict rebound by a parameter": (
        "    async def save(self, cache, entry_cache, bundle, data, creds, update, entry):\n",
        "    async def save(\n"
        "        self, cache, entry_cache, bundle, data, creds, update, entry, dict\n"
        "    ):\n",
    ),
    "inner container read back by index": (
        "        update(entry, data=updated_data)\n",
        "        update(entry, data=updated_data)\n"
        "        self.last = updated_data['bundle']\n",
    ),
    "container taken from receiver state": (
        "        updated_bundle = dict(bundle)\n",
        "        updated_bundle = self._bundle\n",
    ),
    "write into a parameter": (
        '        updated_bundle["fcm_credentials"] = registration\n',
        '        bundle["fcm_credentials"] = registration\n',
    ),
    "deepcopy replaced through sys.modules": (
        "import copy\n",
        "import copy\nimport sys\nsys.modules['copy'].deepcopy = lambda value: value\n",
    ),
    "dict replaced through builtins": (
        "import copy\n",
        "import copy\nimport builtins\nbuiltins.dict = lambda value: value\n",
    ),
    "copy imported under a second name": (
        "import copy\n",
        "import copy\nimport copy as c2\nc2.deepcopy = lambda value: value\n",
    ),
    # Pins for forms the docstring names (``_DOCUMENTED_FORMS``).
    "setdefault with the member": (
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = updated_bundle\n'
        '        updated_data.setdefault("fcm_credentials", creds)\n',
    ),
    "copy reached through __import__": (
        "import copy\n",
        "import copy\nC2 = __import__('copy')\n",
    ),
    "copy reached through importlib": (
        "import copy\n",
        "import copy\nimport importlib\nC2 = importlib.import_module('copy')\n",
    ),
    "copy.deepcopy removed through delattr": (
        "import copy\n",
        "import copy\ndelattr(copy, 'deepcopy')\n",
    ),
    "comprehension target of the copied name": (
        "        digest = credential_member_digest(registration)\n",
        "        digest = credential_member_digest(registration)\n"
        "        names = [registration for registration in creds]\n",
    ),
    "deep copy rebound through nonlocal": (
        "        digest = credential_member_digest(registration)\n",
        "        digest = credential_member_digest(registration)\n"
        "        def swap():\n            nonlocal registration\n"
        "            registration = creds\n",
    ),
    "t-string spelling the member": (
        '        updated_bundle["fcm_credentials"] = registration\n',
        '        updated_bundle[t"fcm_credentials"] = registration\n',
    ),
    "const constant imported with a fallback that is no key": (
        "from custom_components.googlefindmy.const import FCM_CREDENTIALS\n",
        "try:\n"
        "    from custom_components.googlefindmy.const import FCM_CREDENTIALS as FC\n"
        "except ImportError:\n    FC = None\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[FC] = creds\n",
    ),
    "const constant deleted and imported again": (
        "from custom_components.googlefindmy.const import FCM_CREDENTIALS\n",
        "from custom_components.googlefindmy.const import FCM_CREDENTIALS\n"
        "del FCM_CREDENTIALS\n"
        "from custom_components.googlefindmy.const import FCM_CREDENTIALS\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[FCM_CREDENTIALS] = creds\n",
    ),
    "const handle deleted and imported again": (
        "from custom_components.googlefindmy import const\n",
        "from custom_components.googlefindmy import const\ndel const\n"
        "from custom_components.googlefindmy import const\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[const.FCM_CREDENTIALS] = creds\n",
    ),
    # A star import may bind any name, ``copy`` and ``dict`` too, so this form
    # fails without the star report as well; that report alone is pinned by
    # ``test_a_star_import_from_const_fails_on_its_own`` (R10-F7).
    "star import from const": (
        "from custom_components.googlefindmy.const import FCM_CREDENTIALS\n",
        "from custom_components.googlefindmy.const import *\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[FCM_CREDENTIALS] = creds\n",
    ),
    "known key copied to a name that also reads a cycle": (
        "K2 = K\n",
        "K2 = K\nB = C\nC = B\nK2 = B\n",
    ),
    "const handle alias with a binding that is no handle": (
        "from custom_components.googlefindmy import const\n",
        "from custom_components.googlefindmy import const\nC2 = const\nC2 = None\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[C2.FCM_CREDENTIALS] = copy.deepcopy(registration)\n",
    ),
    "member constant name as an attribute of another object": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[self.FCM_CREDENTIALS] = copy.deepcopy(registration)\n",
    ),
    "const imported inside a function": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        from custom_components.googlefindmy import const as c2\n"
        "        registration = copy.deepcopy(dict(creds))\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[c2.FCM_CREDENTIALS] = creds\n",
    ),
    "const constant imported inside a function": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        from custom_components.googlefindmy.const import FCM_CREDENTIALS as F2\n"
        "        registration = copy.deepcopy(dict(creds))\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[F2] = creds\n",
    ),
    "const constant imported again inside a function under its name": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        from custom_components.googlefindmy.const import FCM_CREDENTIALS\n"
        "        registration = copy.deepcopy(dict(creds))\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[FCM_CREDENTIALS] = creds\n",
    ),
    "shared write through an alias of the const handle": (
        "K2 = K\n",
        "K2 = K\nC2 = const\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[C2.FCM_CREDENTIALS] = creds\n",
    ),
    "const reached through the package path": (
        "import copy\n",
        "import copy\nimport custom_components.googlefindmy\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[custom_components.googlefindmy.const.FCM_CREDENTIALS]"
        " = copy.deepcopy(registration)\n",
    ),
    "const constant rebound through its handle": (
        "K2 = K\n",
        "K2 = K\nconst.FCM_CREDENTIALS = 'x'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[const.FCM_CREDENTIALS] = copy.deepcopy(registration)\n",
    ),
    "const handle rebound in the module": (
        "K2 = K\n",
        "K2 = K\nclass const:\n    FCM_CREDENTIALS = 'x'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[const.FCM_CREDENTIALS] = creds\n",
    ),
}

#: Forms that must pass with both writes recognised, each a one-place change to
#: ``_CLEAN``. Two writes prove the key is still seen as the member.
_ACCEPTED: dict[str, tuple[str, ...]] = {
    "keyword write into a fresh dict call": (
        "        updated_bundle = dict(bundle)\n"
        '        updated_bundle["fcm_credentials"] = registration\n',
        "        updated_bundle = dict(bundle, fcm_credentials=registration)\n",
    ),
    "container put into a further display": (
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = {"inner": updated_bundle}\n',
    ),
    "nested def with a parameter of the same name": (
        "        digest = credential_member_digest(registration)\n",
        "        digest = credential_member_digest(registration)\n"
        "        def other(registration):\n"
        "            return registration\n",
    ),
    "container handed positionally to a writer": (
        "        update(entry, data=updated_data)\n",
        "        update(entry, updated_data)\n",
    ),
    "module constant rebound to another string": (
        "K2 = K\n",
        "K2 = K\nK3 = 'one'\nK3 = 'two'\n",
    ),
    "annotated container and annotated copy": (
        "        updated_bundle = dict(bundle)\n",
        "        updated_bundle: dict = dict(bundle)\n",
    ),
    "annotated deep copy": (
        "        registration = copy.deepcopy(dict(creds))\n",
        "        registration: dict = copy.deepcopy(dict(creds))\n",
    ),
    "member read back into the digest": (
        "        updated_data = dict(data)\n",
        "        again = credential_member_digest(updated_bundle['fcm_credentials'])\n"
        "        updated_data = dict(data)\n",
    ),
    "lambda parameter of the same name": (
        "        digest = credential_member_digest(registration)\n",
        "        digest = credential_member_digest(registration)\n"
        "        pick = lambda registration: registration\n",
    ),
    "type parameter of the same name": (
        "    async def save(self, cache, entry_cache, bundle, data, creds, update, entry):\n",
        "    async def save[registration](\n"
        "        self, cache, entry_cache, bundle, data, creds, update, entry\n"
        "    ):\n",
    ),
    "const attribute key with a deep copy": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[const.FCM_CREDENTIALS] = copy.deepcopy(registration)\n",
    ),
    # Round 7 (AP9c): no module knowledge, a lambda scope, a plain alias cycle and
    # a local that shadows a module name raise nothing.
    "method of a local as key": (
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = updated_bundle\n'
        '        name = "a"\n        updated_data[name.lower()] = creds\n',
    ),
    "receiver keys built from locals": (
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = updated_bundle\n'
        "        bundle.pop(f'{entry}:auth', None)\n"
        "        updated_data[entry.entry_id] = creds\n",
    ),
    "walrus inside a lambda binds in the lambda": (
        "K2 = K\n",
        "K2 = K\n_ = [(lambda: (A := 'fcm_'))() for _ in (0,)]\nK3 = A + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n"
        "            updated_data[K3] = creds\n",
    ),
    "alias cycle with a value other than the member": (
        "K2 = K\n",
        "K2 = K\nA = B\nB = A\nA = 'one'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n"
        "            updated_data[B] = creds\n",
    ),
    "unknown module name only read by index": (
        "K2 = K\n",
        "K2 = K\nT = ''.join(('a',))\nU = list[T]\n",
    ),
    "module name rebound as a local of the function": (
        "K2 = K\n",
        "K2 = K\nfor key in ('a',):\n    pass\n",
        '        updated_data["bundle"] = updated_bundle\n',
        '        updated_data["bundle"] = updated_bundle\n'
        "        key = 'other'\n        updated_data[key] = creds\n",
    ),
    "write through an alias of the const handle": (
        "K2 = K\n",
        "K2 = K\nC2 = const\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[C2.FCM_CREDENTIALS] = copy.deepcopy(registration)\n",
    ),
    "const import with a literal fallback": (
        "from custom_components.googlefindmy.const import FCM_CREDENTIALS\n",
        "try:\n"
        "    from custom_components.googlefindmy.const import FCM_CREDENTIALS as FC\n"
        "except ImportError:\n    FC = 'fcm_credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[FC] = copy.deepcopy(registration)\n",
    ),
    "one literal bound to two module names": (
        'K = "fcm_credentials"\nK2 = K\n',
        'K = K2 = "fcm_credentials"\n',
    ),
    "nonlocal of a function local that shadows a module key": (
        "K2 = K\n",
        "K2 = K\ndef _outer():\n    K2 = 1\n    def _inner():\n"
        "        nonlocal K2\n        K2 = 2\n",
    ),
}


def _snippet_problems(source: str) -> tuple[int, list[str], list[str]]:
    return _receiver_problems(ast.parse(source), _SNIPPET_CONSTANTS)


def _changed(changes: tuple[str, ...], label: str) -> str:
    """``_CLEAN`` with each ``(old, new)`` pair of ``changes`` applied once."""

    source = _CLEAN
    for old, new in zip(changes[::2], changes[1::2], strict=True):
        assert source.count(old) == 1, label
        source = source.replace(old, new)
    return source


def test_the_guard_accepts_the_clean_form() -> None:
    """The reference source passes, so every form of ``_BROKEN`` fails for its change."""

    assert _snippet_problems(_CLEAN) == (2, [], [])


#: Forms whose syntax an older Python does not parse, with the first version
#: that does. The CI job on Python 3.13 collects this file (R15-F8): a t-string
#: there is a ``SyntaxError``, not a form the guard has to see.
_NEWER_SYNTAX: dict[str, tuple[int, int]] = {"t-string spelling the member": (3, 14)}


def _skip_newer_syntax(label: str) -> None:
    """Skip a form the running Python cannot parse."""

    needed = _NEWER_SYNTAX.get(label)
    if needed is not None and sys.version_info < needed:
        pytest.skip(f"{label} needs Python {needed[0]}.{needed[1]}")


def test_every_form_of_newer_syntax_is_a_known_form() -> None:
    """A stale entry would skip nothing and hide the rename of a form."""

    assert set(_NEWER_SYNTAX) <= set(_BROKEN) | set(_ACCEPTED)


@pytest.mark.parametrize("label", sorted(_BROKEN))
def test_the_guard_rejects_a_shared_or_unknown_form(label: str) -> None:
    """Each form the reviews found escapes neither classification nor copy check."""

    _skip_newer_syntax(label)
    _writes, unclassified, shared = _snippet_problems(_changed(_BROKEN[label], label))
    assert unclassified or shared, label


def test_a_star_import_from_const_fails_on_its_own() -> None:
    """A star import from ``const`` is reported even where no write is seen."""

    source = "from custom_components.googlefindmy.const import *\n"
    assert _snippet_problems(source)[1], source


def test_a_problem_names_the_container_it_was_found_in() -> None:
    """A container followed into a further local is named in that local's own
    problems, not by the name the walk started from."""

    source = _changed(
        (
            '        updated_data["bundle"] = updated_bundle\n',
            '        updated_data["bundle"] = updated_bundle\n'
            '        kept = updated_data["bundle"]\n',
        ),
        "read back",
    )
    read_back = [p for p in _snippet_problems(source)[2] if "read back" in p]
    assert read_back, source
    assert all("container updated_data " in p for p in read_back), read_back


#: Links of each chain in ``_CHAINS``, more than ``_shallow_stack`` leaves frames.
_CHAIN_LINKS = 150


def _chain(template: str) -> str:
    return "".join(template.format(i + 1, i) for i in range(_CHAIN_LINKS))


#: Chains of names the scan follows (R10-F2): label, changes to ``_CLEAN``, and
#: whether the result is a copy problem (else the clean result).
_CHAINS: dict[str, tuple[tuple[str, ...], bool]] = {
    "module alias chain": (
        ("K2 = K\n", f"A0 = K\n{_chain('A{} = A{}\n')}K2 = A{_CHAIN_LINKS}\n"),
        False,
    ),
    "const handle alias chain": (
        (
            "from custom_components.googlefindmy import const\n",
            "from custom_components.googlefindmy import const\nH0 = const\n"
            + _chain("H{} = H{}\n"),
            "            updated_data[K2] = copy.deepcopy(registration)\n",
            f"            updated_data[H{_CHAIN_LINKS}.FCM_CREDENTIALS]"
            " = copy.deepcopy(registration)\n",
        ),
        False,
    ),
    "local container chain": (
        (
            '        updated_data["bundle"] = updated_bundle\n',
            '        updated_data["bundle"] = updated_bundle\n'
            "        d0 = updated_data\n" + _chain("        d{} = d{}\n"),
        ),
        True,
    ),
}


@contextlib.contextmanager
def _shallow_stack(frames: int) -> Iterator[None]:
    """Leave only ``frames`` frames above the caller, so a walk that recursed
    once per link of a chain raises ``RecursionError``."""

    depth = 0
    frame = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(depth + frames)
    try:
        yield
    finally:
        sys.setrecursionlimit(limit)


@pytest.mark.parametrize("label", sorted(_CHAINS))
def test_a_chain_of_any_length_is_followed(label: str) -> None:
    """R10-F2: module names, ``const`` handles and local containers are
    followed with worklists, so a chain longer than the stack still gives the
    result of a short one instead of a crash."""

    changes, shared = _CHAINS[label]
    source = _changed(changes, label)
    with _shallow_stack(120):
        writes, unclassified, problems = _snippet_problems(source)
    assert (writes, unclassified, bool(problems)) == (2, [], shared), label


_SCOPE_SOURCE = """
def outer(a, b):
    x = a
    def inner(a, k=a):
        return a + b
    g = lambda b, m=(lambda b=b: b): b
    h = lambda b, m=(lambda a=a: a): b
    def rebinding():
        nonlocal x
        x = 1
    return x, g, inner
"""


def test_the_scope_index_reads_a_nested_parameter_as_its_own() -> None:
    """A nested def or lambda with a parameter of the name reads its own
    parameter in its body; only its defaults read the local. A nested
    ``nonlocal`` is a binding site of the local."""

    outer = ast.parse(_SCOPE_SOURCE).body[0]
    index = _ScopeIndex(outer)
    assert sorted(n.lineno for n in index.uses("a")) == [3, 4, 7]
    assert sorted(n.lineno for n in index.uses("b")) == [5, 6]
    sites = index.binding_sites("x")
    assert sorted(type(site).__name__ for site in sites) == ["Name", "Nonlocal"]
    assert [type(site).__name__ for site in index.binding_sites("a")] == ["arg"]
    twice = ast.parse("def f():\n    x = 1\n    def g():\n        nonlocal x, x\n")
    sites = _ScopeIndex(twice.body[0]).binding_sites("x")
    assert sorted(type(site).__name__ for site in sites) == ["Name", "Nonlocal"]
    module = ast.parse("x = 1\ndef g():\n    global x, x\n")
    found = _module_binding_sites(module)["x"]
    assert sorted(type(site).__name__ for site in found) == ["Global", "Name"]


def _container_chain(links: int, writes: int) -> ast.Module:
    """``writes`` deep copies into ``d0``, wrapped in ``links`` fresh displays."""

    lines = [
        "import copy",
        'K = "fcm_credentials"',
        "class R:",
        "    def save(self, creds, update, entry):",
        "        d0 = {}",
        *(f"        d0[K] = copy.deepcopy(creds{i})" for i in range(writes)),
        *(f'        d{i + 1} = {{"a": d{i}}}' for i in range(links)),
        f"        update(entry, data=d{links})",
    ]
    return ast.parse("\n".join(lines) + "\n")


class _EventCounter:
    """The callback of ``_executed_work``: counts the events of the thread that
    made it. ``sys.monitoring`` reports every thread (R14-F5)."""

    def __init__(self) -> None:
        self.count = 0
        self.thread = threading.get_ident()

    def __call__(self, *_: object) -> None:
        if threading.get_ident() == self.thread:
            self.count += 1


def _executed_work(run: Callable[[], object]) -> int:
    """Lines and loop jumps ``run`` executes in any Python file.

    Counting the calls of one helper missed any loop it did not call (F-F of
    round 12), and counting this file only missed a loop in a module it calls,
    such as ``ast.walk`` (R13-F3); a jump counts each pass of a loop, even one
    inside a single line. A loop inside a builtin, such as a membership test
    on a list, is not counted.
    """

    monitoring = sys.monitoring
    tool = next(i for i in range(6) if monitoring.get_tool(i) is None)
    events = (monitoring.events.LINE, monitoring.events.JUMP)
    counted = _EventCounter()
    monitoring.use_tool_id(tool, "credential guard work")
    try:
        for event in events:
            monitoring.register_callback(tool, event, counted)
        monitoring.set_events(tool, events[0] | events[1])
        run()
    finally:
        monitoring.set_events(tool, 0)
        for event in events:
            monitoring.register_callback(tool, event, None)
        monitoring.free_tool_id(tool)
        monitoring.restart_events()
    return counted.count


def _work(tree: ast.Module) -> int:
    """The work of the scan of ``tree`` in this module."""

    def scan() -> None:
        assert _receiver_problems(tree, {}) == (_count_writes(tree), [], [])

    return _executed_work(scan)


def _count_writes(tree: ast.Module) -> int:
    return sum(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(tree)
    )


def test_the_event_counter_counts_the_loops_of_other_modules() -> None:
    """R13-F3: a loop in ``ast.walk`` is work of the scan. Code inside a
    monitoring callback is not traced, so the coverage of ``_EventCounter``
    comes from the direct call."""

    counter = _EventCounter()
    counter(ast.parse.__code__, 0)
    assert counter.count == 1
    elsewhere = threading.Thread(target=counter, args=(ast.parse.__code__, 0))
    elsewhere.start()
    elsewhere.join()
    assert counter.count == 1
    small, large = ast.parse("x = 1\n" * 100), ast.parse("x = 1\n" * 200)
    walked = [
        _executed_work(functools.partial(list, ast.walk(tree)))
        for tree in (small, large)
    ]
    assert 0 < walked[0] < walked[1] <= 2.1 * walked[0], walked


def _second_differences(work: list[int]) -> tuple[list[int], list[int]]:
    """The added work between equally spaced sizes, and how it changes."""

    first = [b - a for a, b in zip(work, work[1:], strict=False)]
    return first, [b - a for a, b in zip(first, first[1:], strict=False)]


def _is_linear(work: list[int]) -> bool:
    """Whether the work of four equally spaced sizes grows linearly.

    A linear scan adds the same work per step, so its second differences are
    zero up to a few events; a square part of ``q`` events per pair adds
    2 q step^2 to each. A ratio of the added work missed a small square
    part under a large linear one (R14-F2: 2.34 against a threshold of 3)."""

    first, second = _second_differences(work)
    return 0 < first[0] and all(abs(d) <= first[0] // 1000 for d in second)


def _toy_work(size: int, square: int) -> int:
    """Linear work plus ``square`` hundredths of a loop step per pair."""

    def run() -> None:
        for _ in range(300 * size):
            pass
        for _ in range(square * size * size // 100):
            pass

    return _executed_work(run)


def test_the_linearity_check_sees_a_small_square_part() -> None:
    """Positive control: the check tells one hundredth of a step per pair
    under 300 steps per item from the linear part alone."""

    sizes = (100, 200, 300, 400)
    assert _is_linear([_toy_work(n, 0) for n in sizes])
    assert not _is_linear([_toy_work(n, 1) for n in sizes])


def test_a_container_chain_is_followed_in_linear_time() -> None:
    """CA-GFMY-GUARD-CONTAINER-QUADRATISCH-001: each local of a chain is looked
    up in one index per function, and the writes of a function share the
    locals already followed. A walk of the function per name grew with the
    square of the chain and with each write (20 writes: 29 times one write);
    so did a search of the function body per read (``_body_ids``). The added
    work per hundred links stays the same."""

    # The first scan of a run also fills caches (54 to 532 events once).
    _work(_container_chain(100, 1))
    work = [_work(_container_chain(links, 1)) for links in (100, 200, 300, 400)]
    assert _is_linear(work), _second_differences(work)
    one = _work(_container_chain(60, 1))
    many = _work(_container_chain(60, 20))
    assert many <= 2 * one, (one, many)


@pytest.mark.parametrize("label", sorted(_ACCEPTED))
def test_the_guard_accepts_an_equivalent_form(label: str) -> None:
    """An equivalent rewrite keeps both writes recognised and raises nothing."""

    source = _changed(_ACCEPTED[label], label)
    assert _snippet_problems(source) == (2, [], []), label


#: Key forms the guard does not see (``V-10`` (c)), each a one-place change of
#: the second write of ``_CLEAN``. The write stays a deep copy, so a form that
#: became a key would count a second write; it must stay at one.
_K2_WRITE = "            updated_data[K2] = copy.deepcopy(registration)\n"
_NOT_SEEN: dict[str, tuple[str, ...]] = {
    "key built by concatenation": (
        "K2 = K\n", "K2 = K\nK3 = 'fcm_' + 'credentials'\n",
        _K2_WRITE, "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
    "key from an f-string": (
        "K2 = K\n", "K2 = K\nP = 'fcm_'\nK3 = f'{P}credentials'\n",
        _K2_WRITE, "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
    "key bound by augmented assignment": (
        "K2 = K\n", "K2 = K\nK3 = 'fcm_'\nK3 += 'credentials'\n",
        _K2_WRITE, "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
    "key bound in a module-level for block": (
        "K2 = K\n", "K2 = K\nfor K3 in ('fcm_' + 'credentials',):\n    pass\n",
        _K2_WRITE, "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
    "key bound through global in a function": (
        "K2 = K\n",
        "K2 = K\nK3 = 'x'\ndef _bind():\n    global K3\n    K3 = 'fcm_' + 'credentials'\n",
        _K2_WRITE, "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
    "key from a class attribute": (
        "K2 = K\n", "K2 = K\nclass NS:\n    A = 'fcm_' + 'credentials'\n",
        _K2_WRITE, "            updated_data[NS.A] = copy.deepcopy(registration)\n",
    ),
    "key imported from a module other than const": (
        "K2 = K\n", "K2 = K\nfrom custom_components.googlefindmy.other import K3\n",
        _K2_WRITE, "            updated_data[K3] = copy.deepcopy(registration)\n",
    ),
}  # fmt: skip


@pytest.mark.parametrize("label", sorted(_NOT_SEEN))
def test_a_key_form_outside_the_four_spellings_is_not_seen(label: str) -> None:
    """The stated limit holds in code: the write keyed so is not counted."""

    assert _snippet_problems(_changed(_NOT_SEEN[label], label)) == (1, [], []), label


#: Tokens that make a docstring literal claim a form. Kept as a list because
#: several of them do not parse on their own (``+=``, ``:=``, ``del``).
_FORM_TOKENS = (
    "+=", "+", "*", "%", ":=", "!s", "[", "f'", 't"', "chr(", ".lower", ".get",
    ".set", "sys.modules", "del", "pop", "for", "in", "with", "match", "global",
    "nonlocal", "except", "import", "def", "class", "lambda", "setdefault",
    "setattr", "delattr", "deepcopy", "dict", "builtins", "__import__",
    "importlib", "__dict__", "_deepcopy_dispatch",
)  # fmt: skip

#: Endings that mark a literal as a file path rather than a form.
_PATH_ENDINGS = (".py", ".md", ".toml")

#: Every form a docstring of this file names, mapped to the label in
#: ``_BROKEN``, ``_ACCEPTED`` or ``_NOT_SEEN``, or to the test that pins it.
_DOCUMENTED_FORMS: dict[str, str] = {
    "del": "test_the_fingerprint_filters_no_member",
    "pop": "test_the_fingerprint_filters_no_member",
    "from ..const import X": "name imported from const as key",
    "setdefault": "setdefault with the member",
    ".get": "test_the_guard_accepts_the_clean_form",
    "in": "test_the_guard_accepts_the_clean_form",
    ".set": "test_the_guard_accepts_the_clean_form",
    "x.get(K) if ... else None": "test_the_guard_accepts_the_clean_form",
    "dict(x)": "test_the_guard_accepts_the_clean_form",
    "_CACHE_RECEIVERS": "cache recognised by name only",
    'bundle[K]["gcm"] = v': "nested write through a read",
    "self.creds[e] = bundle.get(K)": "read handed to receiver state",
    "self.creds[e] = reg": "deep copy also kept by the receiver",
    "def": "deep copy bound only in a nested def",
    "copy.deepcopy(x)": "deepcopy with a memo",
    "import copy": "star import may rebind copy",
    "_copy_is_the_module": "star import may rebind copy",
    "copy.deepcopy": "copy.deepcopy reassigned",
    "copy.deepcopy = f": "copy.deepcopy reassigned",
    "deepcopy": "copy.deepcopy reassigned",
    "setattr(copy, ...)": "copy.deepcopy replaced through setattr",
    "setattr": "copy.deepcopy replaced through setattr",
    "delattr": "copy.deepcopy removed through delattr",
    "copy.__dict__[...]": "copy patched through its __dict__",
    "copy._deepcopy_dispatch": "copy dispatch table patched",
    "dict": "dict rebound by a parameter",
    "dict(...)": "dict rebound by a parameter",
    "sys.modules": "deepcopy replaced through sys.modules",
    "builtins": "dict replaced through builtins",
    "__import__": "copy reached through __import__",
    "importlib": "copy reached through importlib",
    "import copy as c": "copy imported under a second name",
    "nonlocal": "deep copy rebound through nonlocal",
    "keep = data[K] = copy.deepcopy(r)": "deep copy also bound to a local in one statement",
    "_WRITER_CALLS": "container handed to a call outside the writer list",
    "self": "container kept by the receiver after the write",
    't"fcm_credentials"': "t-string spelling the member",
    "[registration for registration in creds]": "comprehension target of the copied name",
    "_BROKEN": "test_the_guard_rejects_a_shared_or_unknown_form",
    "+": "key built by concatenation",
    "+=": "key bound by augmented assignment",
    "for": "key bound in a module-level for block",
    "global": "key bound through global in a function",
    "import": "name imported from const as key",
    "K3 = K": "known key copied to a name with another binding",
    "C2 = const": "write through an alias of the const handle",
    "except ImportError: FC = None": "const constant imported with a fallback that is no key",
    "del FC": "const constant deleted and imported again",
    "from ..const import *": "star import from const",
    "import custom_components.googlefindmy": "const reached through the package path",
    "self.FCM_CREDENTIALS": "member constant name as an attribute of another object",
    'K = "fcm_credentials"': "write through a module alias of the key",
    "K2 = K": "write through a module alias of the key",
    "_NOT_SEEN": "test_a_key_form_outside_the_four_spellings_is_not_seen",
}

#: Words that claim a form is detected; a sentence with one names a pinned form.
_CLAIM = re.compile(r"\b(closed|fails|rejected|rejects)\b")


def _claims_a_form(literal: str) -> bool:
    if "/" in literal or literal.endswith(_PATH_ENDINGS):
        return False
    return any(
        re.search(rf"(?<!\w){re.escape(token)}(?!\w)", literal)
        if token[0].isalpha() or token[0] == "_"
        else token in literal
        for token in _FORM_TOKENS
    )


def _docstring_sentences(path: pathlib.Path) -> list[str]:
    """Every sentence of every docstring in ``path``, literals kept whole."""

    tree = _parse(path)
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, _NESTED_SCOPES))]
    sentences: list[str] = []
    for node in nodes:
        doc = ast.get_docstring(node) if not isinstance(node, ast.Lambda) else None
        if not doc:
            continue
        current = ""
        for piece in re.split(r"(``.+?``)", " ".join(doc.split())):
            if piece.startswith("``"):
                current += piece
                continue
            parts = re.split(r"(?<=[.;])\s+", piece)
            for part in parts[:-1]:
                sentences.append(current + part)
                current = ""
            current += parts[-1]
        sentences.append(current)
    return sentences


def _docstring_problems(
    path: pathlib.Path, documented: Mapping[str, str], pins: set[str]
) -> list[str]:
    """Literals naming a form without a pin, and claims without a pinned form."""

    problems = [
        f"pin {pin!r} of ``{literal}`` does not exist"
        for literal, pin in documented.items()
        if pin not in pins
    ]
    for sentence in _docstring_sentences(path):
        literals = re.findall(r"``(.+?)``", sentence)
        problems += [
            f"unpinned form ``{literal}``"
            for literal in literals
            if _claims_a_form(literal) and literal not in documented
        ]
        if _CLAIM.search(sentence) and not any(
            literal in documented for literal in literals
        ):
            problems.append(f"claim without a pinned form: {sentence[:90]}")
    return problems


def test_the_docstring_names_only_pinned_forms() -> None:
    """Docstrings equal code, mechanically: every literal that names a form has
    a pin, and every sentence that claims detection names a pinned form."""

    pins = {*_BROKEN, *_ACCEPTED, *_NOT_SEEN}
    pins |= {name for name in globals() if name.startswith("test_")}
    problems = _docstring_problems(pathlib.Path(__file__), _DOCUMENTED_FORMS, pins)
    assert not problems, problems
