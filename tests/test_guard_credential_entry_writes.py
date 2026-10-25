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
* Every occurrence of the member key in ``Auth/fcm_receiver_ha.py`` is
  classified. A key is any expression that may evaluate to the member: a
  string constant, an f-string whose placeholders fold, ``+`` of two folding
  parts, a name bound at module level, a constant of ``const.py`` reached by
  import (``from ..const import X``, ``const.X``, ``a.b.const.X``), or a
  keyword argument. A module name holds the set of every value any of its
  bindings gives it, in any block that runs at import and in any order
  (``A = 'fcm_'; K = A + 'credentials'; A = 'x'`` keeps ``K`` the member). A
  binding that does not fold (``+=``, ``for``, ``:=``, ``with``, unpacking,
  ``match``, ``global`` in a function, a call) or more than ``_VALUE_LIMIT``
  values make the name unknown, and an unknown key of a subscript store or
  ``del``, a dict display, ``setdefault`` or ``pop`` fails as unclassified.
  ``test_python_is_the_oracle_for_module_keys`` runs generated module code and
  holds the guard to what Python computes. Allowed kinds are a hash read
  (``.get`` or subscript load whose value goes straight into
  ``credential_member_digest``), an ``in`` test, a
  token cache read or write (``.get``/``.set`` on a receiver named in
  ``_CACHE_RECEIVERS``; ``TokenCache`` detaches values itself, pinned in
  ``tests/test_token_cache_value_isolation.py``), or a mapping write (subscript
  store, dict display key, keyword argument). Any other occurrence fails as
  unclassified, so a read whose value is kept or written through
  (``bundle[K]["gcm"] = v``, ``self.creds[e] = bundle.get(K)``) fails too.
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
  must pass, each against a clean reference source.

Detection limits, stated rather than implied. Not seen: a key computed at
runtime inside a function (a call, ``%``, ``format``, ``join``, a local name)
or bound in another module other than ``const.py``; at module level such a
name is unknown and fails as a key. A t-string is not a ``str`` and so never
the member as a key; one spelling the member still fails as unclassified. The
token cache is recognised by its receiver name, from a closed list, and a
writer on ``_WRITER_CALLS`` is trusted by its name; its body is not followed.
Code in another module that rebinds ``copy.deepcopy`` at runtime is outside
what one file can show. Writes of the member outside the receiver (the config
flow, ``TokenCache``) are out of scope.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import pathlib
from collections import Counter
from collections.abc import Mapping

import pytest
from hypothesis import event, given, settings
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
    walked in full on purpose, so their targets count (fail closed).
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
            and ast.unparse(node.func) in {"setattr", "delattr"}
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
#: fold, or to more strings than ``_VALUE_LIMIT``.
_UNKNOWN = None

#: The values a module name may hold, ``_UNKNOWN`` if not bounded.
Values = frozenset[str] | None

#: More values than this make a name unknown, so the fixpoint ends.
_VALUE_LIMIT = 32


def _join(left: Values, right: Values) -> Values:
    """Every concatenation of a value of ``left`` with one of ``right``."""

    if left is _UNKNOWN or right is _UNKNOWN:
        return _UNKNOWN
    joined = frozenset(a + b for a in left for b in right)
    return _UNKNOWN if len(joined) > _VALUE_LIMIT else joined


def _values(node: ast.AST, env: Mapping[str, Values]) -> Values:
    """The strings ``node`` may evaluate to, or ``_UNKNOWN``.

    Folds string constants, ``+``, f-strings without conversion or format
    spec, and names and ``a.b`` paths through ``env``. Anything else, and a
    name ``env`` does not know (a local, a builtin), is no string the guard can
    see: an empty set, the stated limit for keys computed at runtime.
    """

    if isinstance(node, ast.Constant):
        return frozenset({node.value}) if isinstance(node.value, str) else frozenset()
    if isinstance(node, ast.Name | ast.Attribute):
        path = _dotted(node)
        return env.get(path, frozenset()) if path is not None else frozenset()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _join(_values(node.left, env), _values(node.right, env))
    if isinstance(node, ast.JoinedStr):
        joined: Values = frozenset({""})
        for part in node.values:
            inner: ast.AST = part
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return frozenset()
                inner = part.value
            joined = _join(joined, _values(inner, env))
        return joined
    return frozenset()


def _is_member(node: ast.AST, env: Mapping[str, Values]) -> bool:
    values = _values(node, env)
    return values is not _UNKNOWN and _MEMBER in values


#: What one module-level binding gives its name: an expression to fold, the
#: values of an imported ``const`` constant, or ``_UNKNOWN``.
_Site = ast.expr | frozenset[str] | None


class _ModuleBindings:
    """Every binding of a module name, whatever statement or block it is in.

    Scopes follow the language reference: a def, lambda or class body binds its
    own names unless they are declared ``global`` there; a comprehension binds
    its targets, while ``:=`` in it binds in the enclosing scope. Decorators,
    defaults, bases and the first iterable run in the enclosing scope.
    """

    def __init__(self, constants: Mapping[str, Values]) -> None:
        self.constants = constants
        self.sites: dict[str, list[_Site]] = {}
        self.paths: dict[str, Values] = {}

    def bind(
        self, scope: str, declared: frozenset[str], name: str, site: _Site
    ) -> None:
        if scope == "module" or name in declared:
            self.sites.setdefault(name, []).append(site)

    def visit(self, node: ast.AST, scope: str, declared: frozenset[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            self._visit_definition(node, scope, declared)
        elif isinstance(node, ast.Lambda):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default, scope, declared)
            self.visit(node.body, "function", frozenset())
        elif isinstance(
            node, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
        ):
            self._visit_comprehension(node, scope, declared)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            self._visit_assignment(node, scope, declared)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            self._visit_import(node, scope, declared)
        else:
            if isinstance(node, ast.Name) and not isinstance(
                node.ctx, ast.Load | ast.Del
            ):
                self.bind(scope, declared, node.id, _UNKNOWN)
            for name in _bound_identifiers(node):
                self.bind(scope, declared, name, _UNKNOWN)
            for child in ast.iter_child_nodes(node):
                self.visit(child, scope, declared)

    def _visit_definition(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        scope: str,
        declared: frozenset[str],
    ) -> None:
        self.bind(scope, declared, node.name, _UNKNOWN)
        outer: list[ast.AST] = list(node.decorator_list)
        if isinstance(node, ast.ClassDef):
            outer += [*node.bases, *(keyword.value for keyword in node.keywords)]
        else:
            outer += [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
        for child in outer:
            self.visit(child, scope, declared)
        inner = frozenset(
            name
            for statement in node.body
            for sub in ast.walk(statement)
            if isinstance(sub, ast.Global)
            for name in sub.names
        )
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        for statement in node.body:
            self.visit(statement, kind, inner)

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
        scope: str,
        declared: frozenset[str],
    ) -> None:
        self.visit(node.generators[0].iter, scope, declared)
        inside: list[ast.AST] = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        for index, generator in enumerate(node.generators):
            inside += [generator.target, *generator.ifs]
            if index:
                inside.append(generator.iter)
        stack = inside
        while stack:
            child = stack.pop()
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                self.bind(scope, declared, child.target.id, _UNKNOWN)
                stack.append(child.value)
            elif isinstance(child, _NESTED_SCOPES) or isinstance(
                child, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
            ):
                self.visit(child, "comprehension", frozenset())
            else:
                stack.extend(ast.iter_child_nodes(child))

    def _visit_assignment(
        self, node: ast.Assign | ast.AnnAssign, scope: str, declared: frozenset[str]
    ) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for target in targets:
            if isinstance(target, ast.Name):
                # An annotation without a value binds nothing (N7).
                if value is not None:
                    site = value if isinstance(value, _FOLDABLE) else _UNKNOWN
                    self.bind(scope, declared, target.id, site)
            else:
                self.visit(target, scope, declared)
        for child in (value, getattr(node, "annotation", None)):
            if child is not None:
                self.visit(child, scope, declared)

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


def _module_values(
    tree: ast.Module, constants: Mapping[str, Values]
) -> dict[str, Values]:
    """The values every module name may hold, folded to a fixpoint.

    Flow-insensitive: a name holds every value any of its bindings gives it,
    in any order (``A = 'fcm_'; K = A + 'credentials'; A = 'x'`` keeps
    ``K`` the member). A binding that does not fold (``+=``, ``for``, ``:=``,
    ``with``, ``global`` in a function, a call, ...) makes the name unknown.
    Each step only grows a set or makes it unknown, and a set over
    ``_VALUE_LIMIT`` is unknown, so the loop ends.
    """

    bindings = _ModuleBindings(constants)
    for statement in tree.body:
        bindings.visit(statement, "module", frozenset())
    env: dict[str, Values] = dict(bindings.paths)
    for name in bindings.sites:
        env.setdefault(name, frozenset())
    grown = True
    while grown:
        grown = False
        for name, sites in bindings.sites.items():
            current = env[name]
            if current is _UNKNOWN:
                continue
            values: Values = current
            for site in sites:
                found = _values(site, env) if isinstance(site, ast.expr) else site
                if found is _UNKNOWN or values is _UNKNOWN:
                    values = _UNKNOWN
                    break
                values = values | found
                if len(values) > _VALUE_LIMIT:
                    values = _UNKNOWN
                    break
            if values != current:
                env[name] = values
                grown = True
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
                (ast.unparse(parent.func) in _DIGEST_CALLS and node in parent.args)
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
        if isinstance(call, ast.Call) and ast.unparse(call.func) in _WRITER_CALLS:
            return []
    elif isinstance(parent, ast.Call):
        if expr in parent.args and ast.unparse(parent.func) in _WRITER_CALLS:
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
    return [f"line {line}: the container {ast.unparse(expr)[:60]} is shared"]


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
            f"line {parent.lineno}: the container {ast.unparse(parent.value)} is not a local"
        ]
    if isinstance(parent, ast.Dict):
        return _expression_escapes(parent, function, parents, seen)
    return [f"line {getattr(node, 'lineno', 0)}: unknown container"]


def _is_outermost_key(
    node: ast.AST, parents: dict[ast.AST, ast.AST], env: Mapping[str, Values]
) -> bool:
    """``node`` is not part of a larger expression that folds to the member as well."""

    parent = parents.get(node)
    while isinstance(parent, ast.BinOp | ast.JoinedStr | ast.FormattedValue):
        if not isinstance(parent, ast.FormattedValue) and _is_member(parent, env):
            return False
        parent = parents.get(parent)
    return True


def _occurrences(
    tree: ast.Module, parents: dict[ast.AST, ast.AST], env: Mapping[str, Values]
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
            and _is_member(node, env)
            and _is_outermost_key(node, parents, env)
        ):
            found.append(node)
    return found


def _unknown_keys(
    tree: ast.Module, parents: dict[ast.AST, ast.AST], env: Mapping[str, Values]
) -> list[ast.AST]:
    """Keys of a write whose value the guard cannot bound.

    A subscript store or ``del``, a dict display key, or the first argument of
    ``setdefault`` or ``pop``: if it may be the member and nothing bounds it, it
    fails like an unclassified occurrence. A read keyed so (``list[Any]``,
    ``x[i]``) is not a write and stays out.
    """

    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, _FOLDABLE) or _values(node, env) is not _UNKNOWN:
            continue
        parent = parents.get(node)
        if (
            (
                isinstance(parent, ast.Subscript)
                and node is parent.slice
                and not isinstance(parent.ctx, ast.Load)
            )
            or (isinstance(parent, ast.Dict) and node in parent.keys)
            or (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Attribute)
                and parent.func.attr in {"setdefault", "pop"}
                and parent.args[:1] == [node]
            )
        ):
            found.append(node)
    return found


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
    env = _module_values(tree, known)
    occurrences = _occurrences(tree, parents, env)
    kinds = [_classify(node, parents) for node in occurrences]
    writes = [
        (node, value)
        for node, (kind, value) in zip(occurrences, kinds, strict=True)
        if kind == "mapping write"
    ]
    unclassified = [
        f"line {node.lineno}: {ast.unparse(parents.get(node, node))}"  # type: ignore[attr-defined]
        for node, (kind, _value) in zip(occurrences, kinds, strict=True)
        if kind == "unclassified"
    ] + [
        f"line {node.lineno}: unknown key {ast.unparse(node)}"  # type: ignore[attr-defined]
        for node in _unknown_keys(tree, parents, env)
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
            shared.append(f"line {line}: {ast.unparse(parents.get(node, node))}")
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
    "key bound through global in a function": (
        "K2 = K\n",
        "K2 = K\nK3 = 'x'\ndef _set():\n    global K3\n    K3 = 'fcm_credentials'\n",
        "            updated_data[K2] = copy.deepcopy(registration)\n",
        "            updated_data[K3] = creds\n",
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
    """The reference source passes, so every broken form below fails for its change."""

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
#: expression. The key is the binding construct the template must contain.
_ORACLE_FORMS: dict[str, str] = {
    "Assign": "{n} = {e}",
    "Assign/Tuple": "{n}, _unused = {e}, 0",
    "Assign/Starred": "*{n}, = ({e},)",
    "AnnAssign": "{n}: str = {e}",
    "AugAssign": "{n} += {e}",
    "For": "for {n} in ('x', {e}):\n    pass",
    "While/If": "if True:\n    {n} = {e}",
    "With": "with contextlib.nullcontext({e}) as {n}:\n    pass",
    "NamedExpr": "({n} := {e})",
    "NamedExpr/comprehension": "[({n} := {e}) for _ in (0,)]",
    "ExceptHandler": "try:\n    raise ValueError({e})\nexcept ValueError as {n}:\n    pass",
    "MatchAs": "match {e}:\n    case {n}:\n        pass",
    "MatchStar": "match [{e}]:\n    case [*{n}]:\n        pass",
    "MatchMapping": "match {{'k': {e}}}:\n    case {{**{n}}}:\n        pass",
    "Import": "import string as {n}",
    "FunctionDef": "def {n}():\n    return {e}",
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
        "MatchAs",
        "MatchStar",
        "MatchMapping",
        "Import",
        "ImportFrom",
        "FunctionDef",
        "ClassDef",
        "TypeAlias",
        "Global",
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


def _oracle_atom() -> st.SearchStrategy[str]:
    return st.one_of(
        st.sampled_from(_ORACLE_PARTS).map(repr), st.sampled_from(_ORACLE_NAMES)
    )


#: Some statements over all names, then one that binds ``C`` from two atoms,
#: so that about one draw in ten runs to the member at all.
_ORACLE_STATEMENTS = st.tuples(
    st.lists(
        st.tuples(
            st.sampled_from(sorted(_ORACLE_FORMS)),
            st.sampled_from(_ORACLE_NAMES),
            _oracle_expression(),
        ),
        max_size=5,
    ),
    st.tuples(
        st.sampled_from(sorted(_ORACLE_FORMS)),
        st.just("C"),
        st.tuples(_oracle_atom(), _oracle_atom()).map(
            lambda pair: f"{pair[0]} + {pair[1]}"
        ),
    ),
).map(lambda drawn: [*drawn[0], drawn[1]])


def _oracle_statement(form: str, name: str, expression: str) -> str:
    return _ORACLE_FORMS[form].format(n=name, e=expression)


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
    template contains the construct it is named for."""

    generated = {label.split("/")[0] for label in _ORACLE_FORMS} - {"While"}
    assert generated | set(_ORACLE_EXCLUDED) >= _BINDING_CONSTRUCTS
    for label, template in _ORACLE_FORMS.items():
        tree = ast.parse(template.format(n="C", e="'x'"))
        names = {type(node).__name__ for node in ast.walk(tree)}
        wanted = {
            "While/If": "If",
            "Assign/Tuple": "Tuple",
            "Assign/Starred": "Starred",
        }
        assert wanted.get(label, label.split("/")[0]) in names, label


@pytest.mark.parametrize("form", sorted(_ORACLE_FORMS))
def test_each_binding_form_of_a_key_part_is_seen(form: str) -> None:
    """Every form, once, binding the part ``A`` that ``C = A + 'credentials'``
    turns into the member: the guard must not pass the write keyed by ``C``."""

    statements = [_oracle_statement(form, "A", repr("fcm_")), "C = A + 'credentials'"]
    if not _oracle_runs_to_the_member(statements):
        pytest.skip(f"{form} binds no string that outlives the statement")
    _writes, unclassified, shared = _snippet_problems(_oracle_source(statements))
    assert unclassified or shared, form


@settings(max_examples=1500, deadline=None, derandomize=True)
@given(_ORACLE_STATEMENTS)
def test_python_is_the_oracle_for_module_keys(
    drawn: list[tuple[str, str, str]],
) -> None:
    """Whenever running the module binds the key to the member, the guard fails.

    Python itself decides the value, so a form nobody listed is found as well.
    """

    statements = [_oracle_statement(*item) for item in drawn]
    for form, _name, _expression in drawn:
        event(form)
    reached = _oracle_runs_to_the_member(statements)
    event(f"reaches the member: {reached}")
    if not reached:
        return
    _writes, unclassified, shared = _snippet_problems(_oracle_source(statements))
    assert unclassified or shared, statements
