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
  classified. A key is any expression ``_values`` may evaluate to the member:
  string constants, ``+``, f-strings without conversion or format spec, names
  bound at module level, constants of ``const.py`` reached by import
  (``from ..const import X``, ``const.X``, ``a.b.const.X``) and keyword
  arguments. A module name holds every value any of its bindings gives it, in
  any block that runs at import and in any order, so a write keyed by ``K`` after
  ``A = 'fcm_'; K = A + 'credentials'; A = 'x'`` fails. A binding that does not
  fold makes the name unknown: ``+=``, ``for``, ``with``, ``match``, ``:=`` at
  any depth of nested comprehensions, ``global`` in a function or class body. So
  does a cycle other than a plain alias (``A = A + 'a'``, ``B = A * 0``), or more
  than ``_VALUE_LIMIT`` values. Any other expression is unknown when it reads a
  module name (``P[0]``, ``'%s' % A``, ``f'{A!s}'``, ``chr(X)``, ``NS.A``) or
  only constants and builtins (``'FCM_'.lower()``); one that reads a local or a
  parameter holds no string. An unknown key of a subscript store or ``del``, a
  dict display, ``setdefault`` or ``pop`` fails as unclassified.
  ``test_python_is_the_oracle_for_module_keys`` runs generated module code in
  four families and holds the guard to what Python computes. Allowed kinds are a
  hash read (``.get`` or subscript load whose value goes straight into
  ``credential_member_digest``), an ``in`` test, a token cache read or write
  (``.get``/``.set`` on a receiver named in ``_CACHE_RECEIVERS``; ``TokenCache``
  detaches values itself, pinned in
  ``tests/test_token_cache_value_isolation.py``), or a mapping write (subscript
  store, dict display key, keyword argument). Any other occurrence fails as
  unclassified, so a read whose value is kept or written through
  (``bundle[K]["gcm"] = v``, ``self.creds[e] = bundle.get(K)``) fails too.
* Inside a function, a module name the function binds itself (a parameter, an
  assignment, ``for``, ``with``, ``except``, ``import``, a nested ``def`` or
  class, ``:=``) is its local there; a comprehension target does not hide the
  module name.
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
* ``_BROKEN`` pins forms that must fail and ``_ACCEPTED`` equivalent forms that
  must pass, each against a clean reference source; ``_DOCUMENTED_FORMS`` pins
  every form a docstring of this file names.

Detection limits, stated rather than implied. Not seen: a key computed at
runtime from a local or a parameter inside a function (a call, a format, a
join), because a name the module does not bind holds no string the guard sees;
a local name bound to a module key is not followed, and a name imported from a
module other than ``const.py`` holds no value the guard sees. A t-string is not
a ``str`` and so never the member as a key; ``t"fcm_credentials"`` still fails
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
import builtins
import contextlib
import functools
import pathlib
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping

import pytest
from hypothesis import assume, event, given, settings
from hypothesis import strategies as st

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

#: PEP 695 type parameters, which bind a name as well.
_TYPE_PARAMS = (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)

#: Expressions that may fold to the member key.
_FOLDABLE = (ast.Constant, ast.Name, ast.Attribute, ast.BinOp, ast.JoinedStr)


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
        # ``from x import *`` may bind any name.
        bound = (name if node.name == "*" else node.asname or node.name.split(".")[0],)
    elif isinstance(node, ast.Global | ast.Nonlocal):
        bound = tuple(node.names)
    return name in bound


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


def _binding_sites(scope: ast.AST, name: str) -> list[ast.AST]:
    """Every node that binds ``name`` in ``scope`` itself, in any grammar form.

    A nested scope that declares the name ``nonlocal`` or ``global`` counts as a
    binding too, because it may rebind the local from inside.
    """

    sites: list[ast.AST] = []
    for node in _scope_nodes(scope):
        if _binds(node, name):
            sites.append(node)
        if isinstance(node, _NESTED_SCOPES):
            sites.extend(
                inner
                for inner in ast.walk(node)
                if isinstance(inner, ast.Global | ast.Nonlocal) and name in inner.names
            )
    return sites


def _uses(scope: ast.AST, name: str) -> list[ast.Name]:
    """Loads of ``name`` that read the local of ``scope``.

    A nested def or lambda with a parameter of that name reads its own
    parameter and is skipped; any other nested body is walked, because a closure
    there reads the local.
    """

    found: list[ast.Name] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ) and name in _parameter_names(node.args):
            stack.extend(_outer_parts(node))
            continue
        if isinstance(node, ast.Name) and node.id == name:
            if isinstance(node.ctx, ast.Load):
                found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


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


#: A name whose value the guard cannot bound: bound by a form that does not
#: fold, in a cycle that is not a plain alias, or to more strings than
#: ``_VALUE_LIMIT``.
_UNKNOWN = None

#: The values a module name may hold, ``_UNKNOWN`` if not bounded.
Values = frozenset[str] | None

#: More values than this make a name unknown.
_VALUE_LIMIT = 32

#: Names Python resolves without any binding in the file.
_BUILTINS = frozenset(dir(builtins))

#: Scopes whose body has its own local names (``global`` aside).
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _join(left: Values, right: Values) -> Values:
    """Every concatenation of a value of ``left`` with one of ``right``."""

    if left is _UNKNOWN or right is _UNKNOWN:
        return _UNKNOWN
    joined = frozenset(a + b for a in left for b in right)
    return _UNKNOWN if len(joined) > _VALUE_LIMIT else joined


def _fold_children(node: ast.AST) -> list[ast.AST] | None:
    """The operands ``node`` folds over, or ``None`` if it does not fold."""

    if isinstance(node, ast.Constant | ast.Name | ast.Attribute):
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [node.left, node.right]
    if isinstance(node, ast.JoinedStr):
        children: list[ast.AST] = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return None
                children.append(part.value)
            else:
                children.append(part)
        return children
    return None


def _module_root(node: ast.AST) -> ast.Name | None:
    """The name at the root of ``a.b.c``, else ``None``."""

    while isinstance(node, ast.Attribute):
        node = node.value
    return node if isinstance(node, ast.Name) else None


def _opaque(node: ast.AST, env: Mapping[str, Values], local: frozenset[int]) -> Values:
    """Ansatz A (V-8) for an expression that does not fold.

    It is unknown when any of its names is a module name, whatever values that
    name holds (``chr(X)`` with ``X = 102``), and when it reads nothing but
    constants and builtins (``'FCM_'.lower()``), unless it is a plain literal.
    Otherwise it reads a local or a parameter: no string the guard can see.
    """

    inside: set[str] = set()
    reads: list[ast.Name] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg):
            inside.add(sub.arg)
        elif isinstance(sub, ast.Name):
            if isinstance(sub.ctx, ast.Load):
                reads.append(sub)
            else:
                inside.add(sub.id)
    if any(id(name) not in local and name.id in env for name in reads):
        return _UNKNOWN
    # A name bound inside the expression (a comprehension target, a lambda
    # parameter) derives from it; any other local or unknown name is runtime.
    if any(
        name.id not in inside and (id(name) in local or name.id not in _BUILTINS)
        for name in reads
    ):
        return frozenset()
    with contextlib.suppress(Exception):
        value = ast.literal_eval(node)  # type: ignore[arg-type]
        return frozenset({value}) if isinstance(value, str) else frozenset()
    return _UNKNOWN


def _fold_path(
    node: ast.Attribute, env: Mapping[str, Values], local: frozenset[int]
) -> Values:
    """``a.b.c``: a ``const`` path from ``env``; under any other module name
    unknown (``NS.A``); under a local, a parameter or nothing bound, no string."""

    path = _dotted(node)
    root = _module_root(node)
    if path is None or root is None:
        return _opaque(node, env, local)
    if path in env:
        return env[path]
    if id(root) in local or root.id not in env:
        return frozenset()
    return _UNKNOWN


def _fold_one(
    node: ast.AST,
    env: Mapping[str, Values],
    local: frozenset[int],
    memo: dict[int, Values],
) -> Values:
    if isinstance(node, ast.Constant):
        return frozenset({node.value}) if isinstance(node.value, str) else frozenset()
    if isinstance(node, ast.Name):
        return frozenset() if id(node) in local else env.get(node.id, frozenset())
    if isinstance(node, ast.Attribute):
        return _fold_path(node, env, local)
    if isinstance(node, ast.BinOp):
        return _join(memo[id(node.left)], memo[id(node.right)])
    if isinstance(node, ast.JoinedStr):
        joined: Values = frozenset({""})
        for child in _fold_children(node) or []:
            joined = _join(joined, memo[id(child)])
        return joined
    return _opaque(node, env, local)


def _values(
    node: ast.AST,
    env: Mapping[str, Values],
    local: frozenset[int] = frozenset(),
    memo: dict[int, Values] | None = None,
) -> Values:
    """The strings ``node`` may evaluate to, or ``_UNKNOWN``.

    Folds string constants, ``+``, f-strings without conversion or format
    spec, and names and ``a.b`` paths through ``env``; a name read as a local
    or parameter (its id in ``local``) is not a module name. Any other
    expression follows ``_opaque``. Iterative with a memo per node, so a long
    chain of terms neither recurses nor is folded twice.
    """

    memo = {} if memo is None else memo
    stack: list[tuple[ast.AST, bool]] = [(node, False)]
    while stack:
        current, ready = stack.pop()
        if id(current) in memo:
            continue
        children = _fold_children(current)
        if not ready and children:
            stack.append((current, True))
            stack.extend((child, False) for child in children if id(child) not in memo)
            continue
        if children is None:
            memo[id(current)] = _opaque(current, env, local)
        else:
            memo[id(current)] = _fold_one(current, env, local, memo)
    return memo[id(node)]


def _is_member(
    node: ast.AST,
    env: Mapping[str, Values],
    local: frozenset[int] = frozenset(),
    memo: dict[int, Values] | None = None,
) -> bool:
    values = _values(node, env, local, memo)
    return values is not _UNKNOWN and _MEMBER in values


#: What one module-level binding gives its name: an expression to fold, the
#: values of an imported ``const`` constant, or ``_UNKNOWN``.
_Site = ast.expr | frozenset[str] | None


def _own_globals(node: ast.AST) -> frozenset[str]:
    """Names a def or class body declares ``global`` itself, not in nested scopes."""

    names: set[str] = set()
    stack = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Global):
            names.update(current.names)
        elif not isinstance(current, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(current))
    return frozenset(names)


class _ModuleBindings:
    """Every binding of a module name, whatever statement or block it is in.

    Scopes follow the language reference: a def, lambda or class body binds its
    own names unless they are declared ``global`` there, and such a ``global``
    binding is unknown (its value comes from the body, R7-F2); a comprehension
    binds its targets, while ``:=`` at any depth of nested comprehensions binds
    in the nearest enclosing scope that is not one (PEP 572, R7-F3), a lambda
    being such a scope. Decorators, defaults and bases run in the enclosing
    scope. Iterative, so a deep expression does not recurse.
    """

    def __init__(self, constants: Mapping[str, Values]) -> None:
        self.constants = constants
        self.sites: dict[str, list[_Site]] = {}
        self.paths: dict[str, Values] = {}

    def bind(
        self, scope: str, declared: frozenset[str], name: str, site: _Site
    ) -> None:
        if scope == "module":
            self.sites.setdefault(name, []).append(site)
        elif name in declared:
            self.sites.setdefault(name, []).append(_UNKNOWN)

    def visit_module(self, tree: ast.Module) -> None:
        #: (node, scope, declared, inside a comprehension of that scope)
        stack: list[tuple[ast.AST, str, frozenset[str], bool]] = [
            (statement, "module", frozenset(), False)
            for statement in reversed(tree.body)
        ]
        while stack:
            node, scope, declared, inner = stack.pop()
            # Reversed onto the stack, so bindings are recorded in source order.
            stack.extend(reversed(self._step(node, scope, declared, inner)))

    def _step(
        self, node: ast.AST, scope: str, declared: frozenset[str], inner: bool
    ) -> list[tuple[ast.AST, str, frozenset[str], bool]]:
        Item = tuple[ast.AST, str, frozenset[str], bool]
        if inner:
            return self._step_in_comprehension(node, scope, declared)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            self.bind(scope, declared, node.name, _UNKNOWN)
            outer: list[ast.AST] = list(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                outer += [*node.bases, *(keyword.value for keyword in node.keywords)]
            else:
                outer += [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            own = _own_globals(node)
            items: list[Item] = [(child, scope, declared, False) for child in outer]
            items += [(statement, kind, own, False) for statement in node.body]
        elif isinstance(node, ast.Lambda):
            defaults = [d for d in [*node.args.defaults, *node.args.kw_defaults] if d]
            items = [(d, scope, declared, False) for d in defaults]
            items.append((node.body, "function", frozenset(), False))
        elif isinstance(node, _COMPREHENSION_NODES):
            first = node.generators[0].iter
            items = [(first, scope, declared, False)]
            items += [
                (child, scope, declared, True)
                for child in [
                    *ast.iter_child_nodes(node),
                    *ast.iter_child_nodes(node.generators[0]),
                ]
                if child is not node.generators[0] and child is not first
            ]
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            follow = self._step_assignment(node, scope, declared)
            items = [(child, scope, declared, False) for child in follow]
        elif isinstance(node, ast.Import | ast.ImportFrom):
            self._visit_import(node, scope, declared)
            items = []
        else:
            if isinstance(node, ast.Name) and not isinstance(
                node.ctx, ast.Load | ast.Del
            ):
                self.bind(scope, declared, node.id, _UNKNOWN)
            for name in _bound_identifiers(node):
                self.bind(scope, declared, name, _UNKNOWN)
            items = [
                (child, scope, declared, False) for child in ast.iter_child_nodes(node)
            ]
        return items

    def _step_assignment(
        self, node: ast.Assign | ast.AnnAssign, scope: str, declared: frozenset[str]
    ) -> list[ast.AST]:
        """Bind each plain name target to the value; the rest is visited."""

        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        follow: list[ast.AST] = []
        for target in targets:
            if isinstance(target, ast.Name):
                # An annotation without a value binds nothing (N7).
                if node.value is not None:
                    self.bind(scope, declared, target.id, node.value)
            else:
                follow.append(target)
        follow += [
            child
            for child in (node.value, getattr(node, "annotation", None))
            if child is not None
        ]
        return follow

    def _step_in_comprehension(
        self, node: ast.AST, scope: str, declared: frozenset[str]
    ) -> list[tuple[ast.AST, str, frozenset[str], bool]]:
        """Inside a comprehension owned by ``scope``: only ``:=`` binds there."""

        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            self.bind(scope, declared, node.target.id, _UNKNOWN)
            return [(node.value, scope, declared, True)]
        if isinstance(node, ast.Lambda):
            defaults = [d for d in [*node.args.defaults, *node.args.kw_defaults] if d]
            return [(d, scope, declared, True) for d in defaults] + [
                (node.body, "function", frozenset(), False)
            ]
        return [(child, scope, declared, True) for child in ast.iter_child_nodes(node)]

    def _visit_import(
        self, node: ast.Import | ast.ImportFrom, scope: str, declared: frozenset[str]
    ) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                self.bind(scope, declared, bound, frozenset())
                if alias.name.split(".")[-1] == "const":
                    self._add_paths(alias.asname or alias.name)
            return
        from_const = (node.module or "").split(".")[-1] == "const"
        for alias in node.names:
            bound = alias.asname or alias.name
            if alias.name == "*":
                # Any name may be rebound; ``_copy_is_the_module`` fails the
                # file, and the keys it reads stay as unknown as they are.
                continue
            if from_const:
                site = self.constants.get(alias.name, frozenset())
            elif alias.name == "const":
                site = frozenset()
                self._add_paths(bound)
            else:
                # A name from another module: not a string the guard can see.
                site = frozenset()
            self.bind(scope, declared, bound, site)

    def _add_paths(self, bound: str) -> None:
        for key, value in self.constants.items():
            self.paths[f"{bound}.{key}"] = value


def _bound_identifiers(node: ast.AST) -> list[str]:
    """Names a node binds by identifier rather than by an ``ast.Name`` target."""

    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _function_locals(function: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """``(locals, globals)`` of a def or lambda body (R7-F8 (ii)).

    Locals are the names the function itself binds: parameters, assignment,
    ``for``, ``with``, ``except``, ``import``, a nested def or class name,
    ``nonlocal``, and ``:=`` in a comprehension of the body. A comprehension
    target does not count, so after ``_ = [0 for K3 in ()]`` the module name
    stays visible (fail closed, Review CA-F4); nested ``global`` or
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


def _in_body(scope: ast.AST, child: ast.AST) -> bool:
    if isinstance(scope, ast.Lambda):
        return child is scope.body
    return any(child is statement for statement in scope.body)  # type: ignore[attr-defined]


def _local_reads(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> frozenset[int]:
    """Ids of the ``Name`` loads that read a local or parameter, not a module name.

    The innermost enclosing def or lambda that binds the name or declares it
    ``global`` decides; class bodies are skipped, as Python skips them for the
    functions inside.
    """

    scopes: dict[int, tuple[frozenset[str], frozenset[str]]] = {}
    found: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        child, current = node, parents.get(node)
        while current is not None:
            if isinstance(current, _FUNCTION_SCOPES) and _in_body(current, child):
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


def _site_names(site: ast.AST, local: frozenset[int]) -> frozenset[str]:
    """Every name ``site`` reads that is not a local, in folding parts or not."""

    return frozenset(
        node.id
        for node in ast.walk(site)
        if isinstance(node, ast.Name) and id(node) not in local
    )


def _components(graph: Mapping[str, frozenset[str]]) -> list[list[str]]:
    """Strongly connected components, dependencies first (Tarjan, iterative)."""

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []
    for start in graph:
        if start in index:
            continue
        work: list[tuple[str, list[str]]] = [(start, sorted(graph[start]))]
        index[start] = low[start] = len(index)
        stack.append(start)
        on_stack.add(start)
        while work:
            name, pending = work[-1]
            if pending:
                successor = pending.pop()
                if successor not in index:
                    index[successor] = low[successor] = len(index)
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, sorted(graph[successor])))
                elif successor in on_stack:
                    low[name] = min(low[name], index[successor])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[name])
            if low[name] == index[name]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == name:
                        break
                found.append(component)
    return found


def _module_values(
    tree: ast.Module,
    constants: Mapping[str, Values],
    local: frozenset[int] | None = None,
) -> dict[str, Values]:
    """The values every module name may hold.

    Flow-insensitive: a name holds every value any of its bindings gives it,
    in any order (``A = 'fcm_'; K = A + 'credentials'; A = 'x'`` keeps
    ``K`` the member). A binding that does not fold makes the name unknown.
    Names are evaluated by strongly connected components of the graph from a
    name to every name its bindings read, dependencies first, each binding at
    most once (R7-F5). A component whose inner bindings are all plain names
    (``A = B; B = A``) gives every member the values flowing in from outside;
    any other cycle (``A = A + 'a'``, ``B = A * 0``) is unknown.
    """

    bindings = _ModuleBindings(constants)
    bindings.visit_module(tree)
    if local is None:
        local = _local_reads(tree, _parents(tree))
    env: dict[str, Values] = dict(bindings.paths)
    reads: dict[int, frozenset[str]] = {}
    graph: dict[str, frozenset[str]] = {}
    for name, sites in bindings.sites.items():
        edges: set[str] = set()
        for site in sites:
            if isinstance(site, ast.expr):
                reads[id(site)] = frozenset(
                    read for read in _site_names(site, local) if read in bindings.sites
                )
                edges |= reads[id(site)]
        graph[name] = frozenset(edges)

    def union(sites: list[_Site]) -> Values:
        values: Values = frozenset()
        for site in sites:
            found = _values(site, env, local) if isinstance(site, ast.expr) else site
            if found is _UNKNOWN:
                return _UNKNOWN
            values = values | found
            if len(values) > _VALUE_LIMIT:
                return _UNKNOWN
        return values

    for component in _components(graph):
        members = frozenset(component)
        cyclic = len(component) > 1 or component[0] in graph[component[0]]
        sites = [site for name in component for site in bindings.sites[name]]
        inner = [
            site
            for site in sites
            if isinstance(site, ast.expr) and reads[id(site)] & members
        ]
        values: Values = _UNKNOWN
        if not cyclic or all(
            isinstance(site, ast.Name) and site.id in members for site in inner
        ):
            inner_ids = {id(site) for site in inner}
            values = union([site for site in sites if id(site) not in inner_ids])
        for name in component:
            env[name] = values
    return env


@functools.cache
def _project_constants() -> dict[str, Values]:
    """Values of the names of ``const.py``, as a receiver import would see them."""

    return _module_values(_parse(_CONST), {})


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
        # resolved as a key by ``_module_values``); one in a class body is
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
    function: ast.AST,
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
    parents = _parents(function)
    for node in _uses(function, name):
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


def _expression_escapes(
    expr: ast.AST,
    function: ast.AST,
    parents: dict[ast.AST, ast.AST],
    seen: set[str],
) -> list[str]:
    """Where a container expression goes; empty when it only reaches the entry.

    It may be handed to a call on ``_WRITER_CALLS``, be a value of a further
    display, or be bound by a single-target assignment to a local name or into
    a local container, which are then followed in turn.
    """

    parent = parents.get(expr)
    if isinstance(parent, ast.keyword):
        call = parents.get(parent)
        if isinstance(call, ast.Call) and _dotted(call.func) in _WRITER_CALLS:
            return []
    elif isinstance(parent, ast.Call):
        if expr in parent.args and _dotted(parent.func) in _WRITER_CALLS:
            return []
    elif isinstance(parent, ast.Dict):
        if expr in parent.values:
            return _expression_escapes(parent, function, parents, seen)
    elif (
        isinstance(parent, ast.Assign)
        and parent.value is expr
        and len(parent.targets) == 1
    ):
        target = parent.targets[0]
        if isinstance(target, ast.Name):
            return _name_escapes(target.id, function, parents, seen)
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            return _name_escapes(target.value.id, function, parents, seen)
    line = getattr(expr, "lineno", 0)
    return [f"line {line}: the container {_describe(expr)[:60]} is shared"]


def _name_escapes(
    name: str,
    function: ast.AST,
    parents: dict[ast.AST, ast.AST],
    seen: set[str],
) -> list[str]:
    """Problems of a local container: a binding that is not fresh, or a use that shares it."""

    if name in seen:
        return []
    seen.add(name)
    sites = _binding_sites(function, name)
    problems = [
        f"line {getattr(site, 'lineno', 0)}: the container {name} is not a fresh local"
        for site in sites
        if (assigned := _assigned_value(site, parents)) is None
        or not _is_fresh(assigned)
    ]
    if not sites:
        problems.append(f"the container {name} is not bound in the function")
    for use in _uses(function, name):
        parent = parents.get(use)
        if isinstance(parent, ast.Subscript) and parent.value is use:
            if isinstance(parent.ctx, ast.Load) and not _feeds_the_digest(
                parent, parents
            ):
                problems.append(
                    f"line {use.lineno}: an element of the container {name} is read back"
                )
            continue
        if isinstance(parent, ast.Compare):
            continue
        problems.extend(_expression_escapes(use, function, parents, seen))
    return problems


def _container_problems(
    node: ast.AST, function: ast.AST, parents: dict[ast.AST, ast.AST]
) -> list[str]:
    """R4-F2: the container a write goes into must be fresh and reach only the entry."""

    parent = parents.get(node)
    seen: set[str] = set()
    if isinstance(node, ast.keyword):
        if isinstance(parent, ast.Call) and _is_fresh(parent):
            return _expression_escapes(parent, function, parents, seen)
        return [f"line {node.lineno}: written into a call that is not a fresh dict"]
    if isinstance(parent, ast.Subscript):
        if isinstance(parent.value, ast.Name):
            return _name_escapes(parent.value.id, function, parents, seen)
        return [
            f"line {parent.lineno}: the container {_describe(parent.value)} is not a local"
        ]
    if isinstance(parent, ast.Dict):
        return _expression_escapes(parent, function, parents, seen)
    return [f"line {getattr(node, 'lineno', 0)}: unknown container"]


class _Scan:
    """One fixed module environment, the local reads and a shared fold memo."""

    def __init__(self, env: Mapping[str, Values], local: frozenset[int]) -> None:
        self.env = env
        self.local = local
        self.memo: dict[int, Values] = {}

    def values(self, node: ast.AST) -> Values:
        return _values(node, self.env, self.local, self.memo)

    def is_member(self, node: ast.AST) -> bool:
        values = self.values(node)
        return values is not _UNKNOWN and _MEMBER in values


def _describe(node: ast.AST) -> str:
    """``ast.unparse`` of ``node``, or its size when it is too deep for that."""

    size = sum(1 for _ in ast.walk(node))
    return ast.unparse(node) if size <= 200 else f"<expression of {size} nodes>"


def _is_outermost_key(
    node: ast.AST, parents: dict[ast.AST, ast.AST], scan: _Scan
) -> bool:
    """``node`` is not part of a larger expression that folds to the member as well."""

    parent = parents.get(node)
    while isinstance(parent, ast.BinOp | ast.JoinedStr | ast.FormattedValue):
        if not isinstance(parent, ast.FormattedValue) and scan.is_member(parent):
            return False
        parent = parents.get(parent)
    return True


def _occurrences(
    tree: ast.Module, parents: dict[ast.AST, ast.AST], scan: _Scan
) -> list[ast.AST]:
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            if node.arg == _MEMBER:
                found.append(node)
        elif (
            isinstance(node, _FOLDABLE)
            and not (
                isinstance(node, ast.Name | ast.Attribute)
                and not isinstance(node.ctx, ast.Load)
            )
            and scan.is_member(node)
            and _is_outermost_key(node, parents, scan)
        ):
            found.append(node)
    return found


def _write_keys(tree: ast.Module) -> list[ast.AST]:
    """Keys of a subscript store or ``del``, a dict display, ``setdefault`` or ``pop``."""

    keys: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
            keys.append(node.slice)
        elif isinstance(node, ast.Dict):
            keys.extend(key for key in node.keys if key is not None)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"setdefault", "pop"}
            and node.args
            and not isinstance(node.args[0], ast.Starred)
        ):
            keys.append(node.args[0])
    return keys


def _unknown_keys(tree: ast.Module, scan: _Scan) -> list[ast.AST]:
    """Keys of a write whose value the guard cannot bound, whatever their form.

    A key that may be the member and that nothing bounds (``str(K3)``) fails
    like an unclassified occurrence. A read keyed so (``list[Any]``, ``x[i]``) is not a
    write and stays out.
    """

    return [key for key in _write_keys(tree) if scan.values(key) is _UNKNOWN]


def _receiver_problems(
    tree: ast.Module, constants: Mapping[str, Values | str] | None = None
) -> tuple[int, list[str], list[str]]:
    """Return ``(mapping writes, unclassified uses, shared writes)`` of ``tree``.

    ``constants`` stands for ``const.py``; the default reads the real one.
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
    scan = _Scan(_module_values(tree, known, local), local)
    occurrences = _occurrences(tree, parents, scan)
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
    ] + [
        f"line {node.lineno}: unknown key {_describe(node)}"  # type: ignore[attr-defined]
        for node in _unknown_keys(tree, scan)
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
    for node, value in writes:
        line = node.lineno  # type: ignore[attr-defined]
        if targets_per_value[id(value)] > 1:
            shared.append(f"line {line}: one value written to several keys")
            continue
        if _has_other_targets(node, parents):
            shared.append(f"line {line}: one value bound to several targets")
            continue
        function = _enclosing_function(node, parents)
        if (
            value is None
            or function is None
            or not _is_detached(value, function, parents)
        ):
            shared.append(f"line {line}: {_describe(parents.get(node, node))}")
            continue
        if isinstance(value, ast.Name):
            shared.extend(
                f"line {use.lineno}: {value.id} is used beside its write"
                for use in _shared_uses(value.id, function, written)
            )
        shared.extend(_container_problems(node, function, parents))
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
    # R4-F6: keys written as constant expressions (V-7 (c)).
    "f-string key with a constant placeholder": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[f\"fcm_{'credentials'}\"] = creds\n",
    ),
    "key concatenated from constants": (
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        '            updated_data["fcm_" + "credentials"] = creds\n',
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
    "key name growing without bound": (
        "K2 = K\n",
        "K2 = K\nA = 'a'\nA = A + 'a'\nK3 = A\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
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
    # Round 6: a module name takes every value any of its bindings gives it,
    # and a binding that does not fold makes it unknown; an unknown key fails.
    "key built from a name rebound later": (
        "K2 = K\n",
        "K2 = K\nA = 'fcm_'\nK3 = A + 'credentials'\nA = 'x'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key built by augmented assignment": (
        "K2 = K\n",
        "K2 = K\nK3 = 'fcm_'\nK3 += 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key bound by a module-level call": (
        "K2 = K\n",
        "K2 = K\nK3 = ''.join(('fcm_', 'credentials'))\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    # Round 7 (AP9c): a value bound through global comes from the body, so it is
    # unknown; a parameter default shows the mechanism, a literal would not.
    "key bound through global in a function": (
        "K2 = K\n",
        "K2 = K\nK3 = 'x'\ndef _set(a='fcm_'):\n    global K3\n"
        "    K3 = a + 'credentials'\n_set()\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key bound through global in a class body": (
        "K2 = K\n",
        "K2 = K\nclass _K:\n    PART = 'fcm_'\n    global K3\n"
        "    K3 = PART + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    # Round 7 (AP9c, V-8 A): an expression that does not fold is unknown when it
    # reads a module name or only constants and builtins.
    "key part taken by subscript": (
        "K2 = K\n",
        "K2 = K\nP = ('fcm_', 'x')\nK3 = P[0] + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key part from a method of a constant": (
        "K2 = K\n",
        "K2 = K\nK3 = 'FCM_'.lower() + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key formatted with percent": (
        "K2 = K\n",
        "K2 = K\nA = 'fcm_'\nK3 = '%scredentials' % A\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key from an f-string with a conversion": (
        "K2 = K\n",
        "K2 = K\nA = 'fcm_'\nK3 = f'{A!s}credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key from an f-string with a format spec": (
        "K2 = K\n",
        "K2 = K\nA = 'fcm_'\nK3 = f'{A:s}credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key part from a class attribute": (
        "K2 = K\n",
        "K2 = K\nclass NS:\n    A = 'fcm_'\nK3 = NS.A + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "unknown module key inside a call": (
        "K2 = K\n",
        "K2 = K\nK3 = ''.join(('fcm_', 'credentials'))\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[str(K3)] = creds\n",
    ),
    "key from a call over a module number": (
        "K2 = K\n",
        "K2 = K\nX = 102\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[chr(X) + 'cm_credentials'] = creds\n",
    ),
    "key from a method of a module bytes value": (
        "K2 = K\n",
        "K2 = K\nX = b'fcm_' + b'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[X.decode()] = creds\n",
    ),
    # Round 7 (AP9c): := binds in the enclosing scope at any comprehension depth.
    "key part bound by walrus two comprehensions deep": (
        "K2 = K\n",
        "K2 = K\n_ = [[(A := 'fcm_') for _ in (0,)] for _ in (0,)]\n"
        "K3 = A + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    # Round 7 (AP9c): components of the binding graph, dependencies first.
    "key grown by a self-referencing binding": (
        "K2 = K\n",
        "K2 = K\nA = 'fcm'\nA = A + '_'\nA = A + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[A] = creds\n",
    ),
    "key sliced from a name bound later": (
        "K2 = K\n",
        "K2 = K\nK3 = P[0:4] + 'credentials'\nP = 'fcm_x'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
    ),
    "key through a cycle over a multiplication": (
        "K2 = K\n",
        "K2 = K\nX = 'fcm_'\nA = X\nB = A * 0\nA = B\nC = B + 'fcm_' + 'credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[C] = creds\n",
    ),
    "comprehension target does not hide a module key": (
        "K2 = K\n",
        "K2 = K\nK3 = ''.join(('fcm_', 'credentials'))\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            _ = [0 for K3 in ()]\n            updated_data[K3] = creds\n",
    ),
    # More values than the limit: unknown even with a deep copy written.
    "key with more values than the limit": (
        "K2 = K\n",
        "K2 = K\nP1 = 'a'\nP1 = 'fcm_'\nP2 = 'b'\nP2 = 'cr'\nP3 = 'c'\nP3 = 'ed'\n"
        "P4 = 'd'\nP4 = 'en'\nP5 = 'e'\nP5 = 'tia'\nP6 = 'f'\nP6 = 'ls'\n"
        "K3 = P1 + P2 + P3 + P4 + P5 + P6\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n"
        "            updated_data[K3] = copy.deepcopy(registration)\n",
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
}

#: Forms that must pass with both writes recognised, each a one-place change to
#: ``_CLEAN``. Two writes prove the key is still seen as the member.
_ACCEPTED: dict[str, tuple[str, ...]] = {
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
    "key constant built by concatenation": (
        'K = "fcm_credentials"\n',
        'K = "fcm_" + "credentials"\n',
    ),
    "f-string key without a runtime value": (
        '        updated_bundle["fcm_credentials"] = registration\n',
        "        updated_bundle[f\"fcm_{'credentials'}\"] = registration\n",
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


@pytest.mark.parametrize("label", sorted(_BROKEN))
def test_the_guard_rejects_a_shared_or_unknown_form(label: str) -> None:
    """Each form the reviews found escapes neither classification nor copy check."""

    _writes, unclassified, shared = _snippet_problems(_changed(_BROKEN[label], label))
    assert unclassified or shared, label


@pytest.mark.parametrize("label", sorted(_ACCEPTED))
def test_the_guard_accepts_an_equivalent_form(label: str) -> None:
    """An equivalent rewrite keeps both writes recognised and raises nothing."""

    source = _changed(_ACCEPTED[label], label)
    assert _snippet_problems(source) == (2, [], []), label


#: Names the generated module statements bind; ``C`` is the key of the write.
_ORACLE_NAMES = ("A", "B", "C")
#: Parts only: a statement that spells the member is already caught as an
#: unclassified occurrence, so the member must arise at runtime to test anything.
_ORACLE_PARTS = ("fcm_", "credentials", "x")

#: Every way a module-level statement binds a name (Python reference, "Naming
#: and binding"), one template each; ``{n}`` is the name, ``{e}`` a string
#: expression. Each part of the label is a node the template must contain.
_ORACLE_FORMS: dict[str, str] = {
    "Assign": "{n} = {e}",
    "Assign/Tuple": "{n}, _unused = {e}, 0",
    "Assign/Starred": "*{n}, = ({e},)",
    "AnnAssign": "{n}: str = {e}",
    "AugAssign": "{n} += {e}",
    "For": "for {n} in ('x', {e}):\n    pass",
    "While": "while True:\n    {n} = {e}\n    break",
    "If": "if True:\n    {n} = {e}",
    "With": "with contextlib.nullcontext({e}) as {n}:\n    pass",
    "NamedExpr": "({n} := {e})",
    "NamedExpr/comprehension": "[({n} := {e}) for _ in (0,)]",
    "ExceptHandler": "try:\n    raise ValueError({e})\nexcept ValueError as {n}:\n    pass",
    "ExceptHandler/TryStar": (
        "try:\n    raise ExceptionGroup('g', [ValueError({e})])\n"
        "except* ValueError as {n}:\n    pass"
    ),
    "MatchAs": "match {e}:\n    case {n}:\n        pass",
    "MatchAs/MatchClass": "match {e}:\n    case str({n}):\n        pass",
    "MatchAs/MatchOr": "match [{e}]:\n    case [{n}] | ({n}, 0):\n        pass",
    "MatchStar": "match [{e}]:\n    case [*{n}]:\n        pass",
    "MatchMapping": "match {{'k': {e}}}:\n    case {{**{n}}}:\n        pass",
    "Import": "import string as {n}",
    "FunctionDef": "def {n}():\n    return {e}",
    "AsyncFunctionDef": "async def {n}():\n    return {e}",
    "ClassDef": "class {n}:\n    value = {e}",
    "TypeAlias": "type {n} = str",
    "Global": "def _bind():\n    global {n}\n    {n} = {e}\n_bind()",
    "Delete": "del {n}",
}

#: Binding constructs the oracle cannot run, with the reason; each has a pinned
#: ``_BROKEN`` or ``_ACCEPTED`` form instead.
_ORACLE_EXCLUDED: dict[str, str] = {
    "ImportFrom": "needs the package; 'name imported from const as key' pins it",
    "AsyncFor": "not allowed at module level",
    "AsyncWith": "not allowed at module level",
    "Nonlocal": "not allowed at module level",
    "TypeVar": (
        "a type parameter binds in its own annotation scope, never at module level;"
        " 'copy bound as a function type parameter' pins it"
    ),
}

#: The binding constructs of the grammar, from the language reference.
_BINDING_CONSTRUCTS = frozenset(
    {
        "Assign",
        "AnnAssign",
        "AugAssign",
        "For",
        "AsyncFor",
        "With",
        "AsyncWith",
        "NamedExpr",
        "ExceptHandler",
        "TryStar",
        "MatchAs",
        "MatchStar",
        "MatchMapping",
        "Import",
        "ImportFrom",
        "FunctionDef",
        "AsyncFunctionDef",
        "ClassDef",
        "TypeAlias",
        "TypeVar",
        "Global",
        "Nonlocal",
        "Delete",
    }
)


def _oracle_expression() -> st.SearchStrategy[str]:
    atom = st.one_of(
        st.sampled_from(_ORACLE_PARTS).map(repr), st.sampled_from(_ORACLE_NAMES)
    )
    return st.one_of(
        atom,
        st.tuples(atom, atom).map(lambda pair: f"{pair[0]} + {pair[1]}"),
        st.tuples(atom, atom).map(lambda pair: f"f'{{{pair[0]}}}{{{pair[1]}}}'"),
    )


def _oracle_statement(form: str, name: str, expression: str) -> str:
    return _ORACLE_FORMS[form].format(n=name, e=expression)


def _identity(part: str) -> object:
    return part


#: Operand templates of the ``ausdruck`` family: ``{x}`` holds the stored value,
#: and the template turns it back into the part (``conversion r`` never does,
#: as a control).
_EXPRESSION_TEMPLATES: dict[str, tuple[str, Callable[[str], object]]] = {
    "plain": ("{x}", _identity),
    "subscript": ("{x}[0]", lambda part: (part, "x")),
    "slice": ("{x}[1:]", lambda part: "x" + part),
    "lower": ("{x}.lower()", str.upper),
    "strip": ("{x}.strip()", lambda part: f" {part} "),
    "percent": ("'%s' % ({x},)", _identity),
    "conversion s": ("f'{{{x}!s}}'", _identity),
    "conversion r": ("f'{{{x}!r}}'", _identity),
    "format s": ("f'{{{x}:s}}'", _identity),
    "format width": ("f'{{{x}:>4}}'", _identity),
}


@st.composite
def _ausdruck_case(draw: st.DrawFn) -> list[str]:
    """Two operands built from expressions over a module name, a class
    attribute or a literal; the first is held by a name by construction."""

    statements: list[str] = []
    operands: list[str] = []
    for slot, wanted in enumerate(("fcm_", "credentials")):
        part = draw(st.sampled_from((wanted, wanted, wanted, "x")))
        template, encode = _EXPRESSION_TEMPLATES[
            draw(st.sampled_from(sorted(_EXPRESSION_TEMPLATES)))
        ]
        value = repr(encode(part))
        holder = draw(
            st.sampled_from(("name", "class") if slot == 0 else ("name", "literal"))
        )
        if holder == "name":
            name = "AB"[slot]
            statements.append(f"{name} = {value}")
            held = name
        elif holder == "class":
            statements.append(f"class NS:\n    v = {value}")
            held = "NS.v"
        else:
            held = value
        operands.append(template.format(x=held))
    return [*statements, f"C = {operands[0]} + {operands[1]}"]


#: Templates of the ``global`` family: a module name bound through ``global``
#: from a function, a class body or a nested function, with a value the module
#: does not see (a parameter default or a class-body name).
_GLOBAL_TEMPLATES: dict[str, str] = {
    "function": "def _set(a={p}):\n    global {n}\n    {n} = a\n_set()",
    "class body": "class _K:\n    part = {p}\n    global {n}\n    {n} = part",
    "nested function": (
        "def _outer():\n    def _inner(a={p}):\n        global {n}\n        {n} = a\n"
        "    _inner()\n_outer()"
    ),
}


@st.composite
def _global_case(draw: st.DrawFn) -> list[str]:
    statements: list[str] = []
    for name, wanted in (("A", "fcm_"), ("B", "credentials")):
        part = draw(st.sampled_from((wanted, wanted, wanted, "x")))
        template = draw(st.sampled_from([*sorted(_GLOBAL_TEMPLATES), "plain"]))
        if template == "plain":
            statements.append(f"{name} = {part!r}")
        else:
            statements.append(_GLOBAL_TEMPLATES[template].format(n=name, p=repr(part)))
    statements[1:1] = draw(
        st.lists(st.sampled_from(("A = 'x'", "B = 'x'")), max_size=1)
    )
    return [*statements, "C = A + B"]


#: Comprehensions the ``walross`` family nests a ``:=`` in, as ``{x}`` the element.
_COMPREHENSIONS = (
    "[{x} for _ in (0,)]",
    "{{id({x}) for _ in (0,)}}",
    "{{0: {x} for _ in (0,)}}",
    "list({x} for _ in (0,))",
)


@st.composite
def _walross_case(draw: st.DrawFn) -> list[str]:
    """``A := part`` one to three comprehensions deep; a lambda around it binds
    in the lambda (PEP 572) and is the control that never reaches the module."""

    part = draw(st.sampled_from(("fcm_", "fcm_", "fcm_", "x")))
    element = f"(A := {part!r})"
    if draw(st.booleans()) and draw(st.booleans()):
        element = f"(lambda: {element})()"
    for _level in range(draw(st.integers(min_value=1, max_value=3))):
        element = draw(st.sampled_from(_COMPREHENSIONS)).format(x=element)
    second = draw(st.sampled_from(("'credentials'", "B")))
    statements = [f"_ = {element}"]
    if second == "B":
        statements.insert(0, "B = 'credentials'")
    return [*statements, f"C = A + {second}"]


@st.composite
def _bindung_case(draw: st.DrawFn) -> list[str]:
    """Some statements over all names, then ``A`` bound by any form and
    ``C = A + 'credentials'``."""

    noise = draw(
        st.lists(
            st.tuples(
                st.sampled_from(sorted(_ORACLE_FORMS)),
                st.sampled_from(_ORACLE_NAMES),
                _oracle_expression(),
            ),
            max_size=3,
        )
    )
    form = draw(st.sampled_from(sorted(_ORACLE_FORMS)))
    part = draw(st.sampled_from(("fcm_", "fcm_", "fcm_", "x")))
    return [
        *(_oracle_statement(*item) for item in noise),
        _oracle_statement(form, "A", repr(part)),
        "C = A + 'credentials'",
    ]


#: Rebindings drawn after every family's statements: the guard is flow-insensitive,
#: so a name rebound after the key was built must not hide the member.
_ORACLE_REBINDINGS = ("A = 'x'", "B = 'x'", "C = 'x'")

#: The oracle families (R7-F9, R7-F10): one test each, so that each reports its
#: own counterexample and its own statistics.
_ORACLE_FAMILIES: dict[str, st.SearchStrategy[list[str]]] = {
    "ausdruck": _ausdruck_case(),
    "global": _global_case(),
    "walross": _walross_case(),
    "bindung": _bindung_case(),
}


def _oracle_runs_to_the_member(statements: list[str]) -> bool:
    """Run ``statements`` one by one; True if ``C`` ever holds the member.

    A statement that raises is skipped; the guard folds every binding in any
    order, so it covers every subset as well.
    """

    namespace: dict[str, object] = {"contextlib": contextlib}
    exec("A = B = C = ''", namespace)  # noqa: S102 - generated test code only
    for statement in statements:
        try:
            exec(statement, namespace)  # noqa: S102 - generated test code only
        except Exception:  # noqa: BLE001 - a failing statement binds nothing
            continue
        if namespace.get("C") == _MEMBER:
            return True
    return False


def _oracle_source(statements: list[str]) -> str:
    """``_CLEAN`` with ``statements`` at module level and a write keyed by ``C``."""

    module = "\n".join(["A = B = C = ''", *statements])
    return _changed(
        (
            "K2 = K\n",
            f"K2 = K\nimport contextlib\n{module}\n",
            "            updated_data[K2] = copy.deepcopy(registration)\n",
            "            updated_data[C] = creds\n",
        ),
        "oracle",
    )


def test_the_oracle_covers_every_binding_construct() -> None:
    """Every binding construct is generated or excluded with a reason, and each
    template contains every node its label names."""

    generated = {part for label in _ORACLE_FORMS for part in label.split("/")}
    assert generated | set(_ORACLE_EXCLUDED) >= _BINDING_CONSTRUCTS
    for label, template in _ORACLE_FORMS.items():
        tree = ast.parse(template.format(n="C", e="'x'"))
        names = {type(node).__name__ for node in ast.walk(tree)}
        assert set(label.split("/")) <= names, label


@pytest.mark.parametrize("form", sorted(_ORACLE_FORMS))
def test_each_binding_form_of_a_key_part_is_seen(form: str) -> None:
    """Every form, once, binding the part ``A`` that ``C = A + 'credentials'``
    turns into the member: the guard must not pass the write keyed by ``C``."""

    statements = [_oracle_statement(form, "A", repr("fcm_")), "C = A + 'credentials'"]
    if not _oracle_runs_to_the_member(statements):
        pytest.skip(f"{form} binds no string that outlives the statement")
    _writes, unclassified, shared = _snippet_problems(_oracle_source(statements))
    assert unclassified or shared, form


@pytest.mark.hypothesis
@pytest.mark.parametrize("family", sorted(_ORACLE_FAMILIES))
@settings(max_examples=500, deadline=None, derandomize=True)
@given(data=st.data())
def test_python_is_the_oracle_for_module_keys(family: str, data: st.DataObject) -> None:
    """Whenever running the module binds the key ``C`` to the member, the guard fails.

    Python itself decides the value, so a form nobody listed is found as well.
    """

    statements = data.draw(_ORACLE_FAMILIES[family], label="statements")
    statements += data.draw(
        st.lists(st.sampled_from(_ORACLE_REBINDINGS), max_size=2), label="rebound"
    )
    assume(_MEMBER not in "\n".join(statements))
    reached = _oracle_runs_to_the_member(statements)
    event(f"tests the guard: {reached}")
    if not reached:
        return
    _writes, unclassified, shared = _snippet_problems(_oracle_source(statements))
    assert unclassified or shared, statements


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
#: ``_BROKEN``, ``_ACCEPTED``, ``_ORACLE_FORMS`` or ``_ORACLE_FAMILIES``, or to
#: the test that pins it.
_DOCUMENTED_FORMS: dict[str, str] = {
    "del": "test_the_fingerprint_filters_no_member",
    "pop": "test_the_fingerprint_filters_no_member",
    "+": "key constant built by concatenation",
    "from ..const import X": "name imported from const as key",
    "A = 'fcm_'; K = A + 'credentials'; A = 'x'": "key built from a name rebound later",
    "+=": "key built by augmented assignment",
    "for": "key constant bound in a module-level for block",
    "with": "With",
    "match": "MatchAs",
    ":=": "key part bound by walrus two comprehensions deep",
    "global": "key bound through global in a function",
    "A = A + 'a'": "key grown by a self-referencing binding",
    "B = A * 0": "key through a cycle over a multiplication",
    "_VALUE_LIMIT": "key with more values than the limit",
    "P[0]": "key part taken by subscript",
    "'%s' % A": "key formatted with percent",
    "f'{A!s}'": "key from an f-string with a conversion",
    "chr(X)": "key from a call over a module number",
    "X = 102": "key from a call over a module number",
    "NS.A": "key part from a class attribute",
    "'FCM_'.lower()": "key part from a method of a constant",
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
    "except": "ExceptHandler",
    "import": "Import",
    "def": "deep copy bound only in a nested def",
    "copy.deepcopy(x)": "deepcopy with a memo",
    "import copy": "star import may rebind copy",
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
    "A = B; B = A": "alias cycle with a value other than the member",
    "list[Any]": "unknown module name only read by index",
    "x[i]": "unknown module name only read by index",
    "A := part": "walross",
    "[registration for registration in creds]": "comprehension target of the copied name",
    "_BROKEN": "test_the_guard_rejects_a_shared_or_unknown_form",
    "C": "test_python_is_the_oracle_for_module_keys",
    "_ = [0 for K3 in ()]": "comprehension target does not hide a module key",
    "str(K3)": "unknown module key inside a call",
    "C = A + 'credentials'": "bindung",
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


def _docstring_sentences() -> list[str]:
    """Every sentence of every docstring in this file, literals kept whole."""

    tree = _parse(pathlib.Path(__file__))
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


def test_the_docstring_names_only_pinned_forms() -> None:
    """Docstrings equal code, mechanically: every literal that names a form has
    a pin, and every sentence that claims detection names a pinned form."""

    pins = {*_BROKEN, *_ACCEPTED, *_ORACLE_FORMS, *_ORACLE_FAMILIES}
    pins |= {name for name in globals() if name.startswith("test_")}
    problems = [
        f"pin {pin!r} of ``{literal}`` does not exist"
        for literal, pin in _DOCUMENTED_FORMS.items()
        if pin not in pins
    ]
    for sentence in _docstring_sentences():
        literals = re.findall(r"``(.+?)``", sentence)
        problems += [
            f"unpinned form ``{literal}``"
            for literal in literals
            if _claims_a_form(literal) and literal not in _DOCUMENTED_FORMS
        ]
        if _CLAIM.search(sentence) and not any(
            literal in _DOCUMENTED_FORMS for literal in literals
        ):
            problems.append(f"claim without a pinned form: {sentence[:90]}")
    assert not problems, problems


def _chain(n: int, back_edge: bool) -> ast.Module:
    lines = [f'N{i} = N{i - 1} + "a"' for i in range(n, 0, -1)] + ['N0 = "a"']
    return ast.parse("\n".join([*lines, f"N0 = N{n}"] if back_edge else lines))


def test_the_fixpoint_evaluates_each_binding_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7-F5: dependencies first, each binding folded once on the acyclic chain
    written backwards, and a growing cycle is unknown without being folded."""

    calls: Counter[int] = Counter()
    fold = _values

    def counted(node: ast.AST, *args: object) -> Values:
        calls[id(node)] += 1
        return fold(node, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(sys.modules[__name__], "_values", counted)
    env = _module_values(_chain(1000, back_edge=False), {})
    assert (sum(calls.values()), max(calls.values())) == (1001, 1)
    assert env["N1000"] == frozenset({"a" * 1001})
    calls.clear()
    env = _module_values(_chain(1000, back_edge=True), {})
    assert max(calls.values(), default=0) <= 1
    assert env["N1000"] is _UNKNOWN
