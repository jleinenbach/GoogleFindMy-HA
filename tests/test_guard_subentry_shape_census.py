# tests/test_guard_subentry_shape_census.py
"""Latency watch over the subentry shape census the deletion paths rest on.

``PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS`` leaves the stale sweep in
``ConfigEntrySubEntryManager.async_sync`` without a type guard. That decision is
not a claim that the sweep is safe in general; it rests on two *measured*
properties of today's tree:

* every production writer produces one of exactly three
  ``(subentry_type, group_key)`` pairs, all of which resolve onto a core key, so
  no writer hands the sweep a removal candidate; and
* all three production callers of ``async_sync`` pass exactly the two core keys
  as ``desired``, so ``desired`` is the core key set rather than an arbitrary
  one. Two build the definitions inline; the options-flow repair delegates to
  ``_build_core_subentry_definitions``, which returns both core definitions or
  an empty list, and its caller returns early on empty.

Both are latent premises: nothing in the code enforces either, and a fourth pair
or a caller with a narrower ``desired`` would silently turn a group into a
removal candidate whose registry bindings ``async_remove`` takes with it. The
plan's trigger ``T-B`` names that drift; this guard is its observer, and
``grep -rn "stale_keys" tests/`` returning nothing was the evidence that no
observer existed.

Three design choices keep it honest, each one a lesson paid for elsewhere:

* **AST over text.** The census must not grep for ``subentry_type=``. The flow
  handlers write that field as a dict item
  (``create_kwargs["subentry_type"] = self._subentry_type``), so a text pattern
  derived from the keyword form misses the *only* producer of the
  ``(hub, service)`` pair, the very pair the plan exists for. The near miss is
  recorded as ``CA-SWEEP-PATTERN-COVERS-SURROGATE-001``.
* **Frozen set, not a count.** ``tests/test_guard_config_entry_stub.py`` states
  the reason: a bare count is defeated by a one-for-one swap. Removing one pair
  and adding another leaves the number equal while a fresh shape slips in.
  Freezing the *members* catches that, and the stale self-test makes the freeze
  cut both ways.
* **Names, then values.** The frozen entries are the constant *names* the AST
  sees, and a second observation resolves those names against ``const.py``.
  Either alone is defeatable: renaming a constant while keeping its value is
  invisible to a value-only freeze, and rebinding a value while keeping the name
  is invisible to a name-only one.

Two deliberate blind spots, stated rather than implied, and both wider than an
earlier draft of this docstring admitted:

* the repair path in ``config_flow.py`` calls
  ``_async_create_subentry(..., subentry_type=...)`` without passing the group
  key as an argument. **Neither** half is read at that call site: swapping its
  ``subentry_type`` argument leaves all four observations unchanged. Its types
  are covered only indirectly, through the handler-class pairs, and its keys
  only through behaviour tests.
* ``ConfigEntrySubEntryManager.update_visible_device_ids`` writes the group key
  onto an existing subentry of arbitrary type, so it too forms a pair no static
  census can read.

What the census does cover is the *creation* of a group, which is where a new
shape enters the tree. Both blind spots rewrite an existing one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "custom_components" / "googlefindmy"
CONST_MODULE = PACKAGE / "const.py"

# Observation 1: the (subentry_type, group_key) pairs a production writer can
# produce, as constant *names*. Statically complete for the two forms that carry
# both halves in one place: a flow-handler class body, and a
# ``ConfigEntrySubentryDefinition(...)`` call.
FROZEN_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("SUBENTRY_TYPE_TRACKER", "TRACKER_SUBENTRY_KEY"),
        ("SUBENTRY_TYPE_SERVICE", "SERVICE_SUBENTRY_KEY"),
        ("SUBENTRY_TYPE_HUB", "SERVICE_SUBENTRY_KEY"),
    }
)

# Observation 2: those names resolved against ``const.py``. A rename with an
# unchanged value passes observation 1 and fails here, and vice versa.
FROZEN_CONSTANT_VALUES: dict[str, str] = {
    "TRACKER_SUBENTRY_KEY": "core_tracking",
    "SERVICE_SUBENTRY_KEY": "service",
    "SUBENTRY_TYPE_TRACKER": "tracker",
    "SUBENTRY_TYPE_SERVICE": "service",
    "SUBENTRY_TYPE_HUB": "hub",
}

# Observation 3a: every production call site of ``async_sync``, by file and
# enclosing function. This is what ``desired`` originates from, and a *new*
# caller is the drift the plan's trigger ``T-B`` names.
FROZEN_SYNC_CALLERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("custom_components/googlefindmy/__init__.py", "async_setup_entry"),
        ("custom_components/googlefindmy/coordinator/subentry.py", "_repair"),
        (
            "custom_components/googlefindmy/config_flow.py",
            "_async_trigger_core_subentry_repair",
        ),
    }
)

# Observation 3b: the ``key=`` values of every definition built anywhere in the
# package. Together with 3a this brackets ``desired`` from both sides: 3a
# catches a new consumer, 3b a new or vanished key.
#
# Why two observations instead of one mapping caller to keys: the repair caller
# receives its definitions as a parameter built in a *different* function
# (``_build_core_subentry_definitions``), so the caller-to-keys edge is not
# statically readable without dataflow. An earlier draft of this guard tried to
# read it from the enclosing function body and simply lost that caller. The
# split is narrower than the ideal and says so, rather than reporting a complete
# census it cannot deliver.
FROZEN_DEFINITION_KEYS: frozenset[str] = frozenset(
    {"TRACKER_SUBENTRY_KEY", "SERVICE_SUBENTRY_KEY"}
)

_DEFINITION_FACTORY = "ConfigEntrySubentryDefinition"
_PLAN = "PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS"


def _parse(path: Path) -> ast.Module:
    """Parse one package file, failing with a readable message.

    Without the wrapper an unparsable file (a vendored drop, a syntax level the
    runner does not know) raises ``SyntaxError`` out of every test in this file
    at once, and none of the four carefully written failure messages below ever
    reaches a reader.
    """

    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as err:  # pragma: no cover - no unparsable file today
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)} does not parse ({err}), so the "
            f"subentry shape census cannot be taken. {_PLAN} relies on it to "
            "observe the premises the stale sweep rests on; a census that "
            "silently skips a file would be worse than one that stops."
        )


def _bound_name(value: ast.expr | None) -> str | None:
    """Render an assigned value as a comparable token, or ``None`` if opaque.

    A constant is rendered as its ``repr`` rather than dropped. Dropping is the
    dangerous direction: a handler written with string literals instead of the
    shared constants would vanish from the census entirely, and the ratchet
    would stay green while a fourth shape entered the tree.
    """

    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return repr(value.value)
    return None


def _class_attribute_pairs(tree: ast.Module) -> set[tuple[str, str]]:
    """Pairs written as sibling class attributes (the flow handlers).

    Both ``x = VALUE`` and ``x: str = VALUE`` are read. The annotated form is
    not hypothetical: ``_BaseSubentryFlow`` already annotates both fields, so a
    subclass repeating the annotation is the likelier spelling, and an earlier
    draft of this census saw only the bare form.
    """

    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        attrs: dict[str, str] = {}
        for stmt in node.body:
            target: ast.expr | None = None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign):
                target = stmt.target
            if not isinstance(target, ast.Name):
                continue
            bound = _bound_name(stmt.value)
            if bound is not None:
                attrs[target.id] = bound
        if "_subentry_type" in attrs and "_group_key" in attrs:
            pairs.add((attrs["_subentry_type"], attrs["_group_key"]))
    return pairs


def _definition_call_pairs(tree: ast.Module) -> set[tuple[str, str]]:
    """Pairs written as keywords of a ``ConfigEntrySubentryDefinition`` call."""

    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != _DEFINITION_FACTORY:
            continue
        if any(kw.arg is None for kw in node.keywords):
            # ``Definition(**payload)`` hides both halves behind a mapping this
            # census cannot read. Surfacing it as a shape keeps the ratchet
            # loud: the frozen set will not contain it, so the guard fails and
            # names the construction instead of silently skipping it.
            pairs.add(("<**kwargs>", "<**kwargs>"))
            continue
        kwargs = {
            kw.arg: kw.value
            for kw in node.keywords
            if kw.arg in {"key", "subentry_type"}
        }
        key = _bound_name(kwargs.get("key"))
        subentry_type = _bound_name(kwargs.get("subentry_type"))
        if key is not None and subentry_type is not None:
            pairs.add((subentry_type, key))
    return pairs


def _observed_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = _parse(path)
        pairs |= _class_attribute_pairs(tree)
        pairs |= _definition_call_pairs(tree)
    return pairs


def _observed_constant_values() -> dict[str, str]:
    """Module-level ``NAME[: str] = "literal"`` bindings in ``const.py``."""

    values: dict[str, str] = {}
    tree = _parse(CONST_MODULE)
    for stmt in tree.body:
        target: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            values[target.id] = stmt.value.value
    return values


def _observed_sync_callers() -> set[tuple[str, str]]:
    """Production ``async_sync(...)`` call sites, by file and enclosing function.

    The *innermost* enclosing function is reported, which matters where a nested
    helper carries the call: ``coordinator/subentry.py`` has ``_repair`` inside
    ``_schedule_core_subentry_repair``, and naming the outer one would let the
    inner be replaced unnoticed.

    Two spellings count, and the second one is why this census exists in the
    form it does. A direct ``manager.async_sync(...)`` is an ``ast.Attribute``.
    The options-flow repair instead does
    ``getattr(subentry_manager, "async_sync", None)`` and awaits the bound
    result, which is an ``ast.Constant`` in an argument list and invisible to an
    attribute matcher. An earlier draft of this guard matched attributes only,
    claimed to see "every production call site" in its own docstring, and knew
    two of the three. That is the same shape as the near miss the module
    docstring records for ``subentry_type=``: a pattern derived from the
    conspicuous spelling.
    """

    callers: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = _parse(path)
        rel = path.relative_to(REPO_ROOT).as_posix()
        stack: list[str] = []

        def names_async_sync(node: ast.AST) -> bool:
            if isinstance(node, ast.Attribute) and node.attr == "async_sync":
                return True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "async_sync"
            ):
                return True
            return False

        def visit(node: ast.AST, rel: str = rel, stack: list[str] = stack) -> None:
            pushed = False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append(node.name)
                pushed = True
            if names_async_sync(node) and stack:
                callers.add((rel, stack[-1]))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                stack.pop()

        visit(tree)
    return callers


def _observed_definition_keys() -> set[str]:
    """Every ``key=`` name passed to a definition anywhere in the package."""

    keys: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name != _DEFINITION_FACTORY:
                continue
            for kw in node.keywords:
                if kw.arg == "key" and isinstance(kw.value, ast.Name):
                    keys.add(kw.value.id)
    return keys


def test_producible_subentry_pairs_are_frozen() -> None:
    """Class A2 (standing assertion). Freeze the shapes, not their number."""

    observed = _observed_pairs()
    added = observed - FROZEN_PAIRS
    assert not added, (
        f"A new (subentry_type, group_key) pair appeared: {sorted(added)}. "
        f"{_PLAN} leaves the stale sweep in "
        "``ConfigEntrySubEntryManager.async_sync`` without a type guard because "
        "every producible pair resolves onto a core key, so no writer hands the "
        "sweep a removal candidate. A pair outside that set may break the "
        "premise, and removal there runs through ``async_remove`` into core's "
        "``async_remove_subentry``, which clears the device and entity registry "
        "bindings. Next step: decide whether the new pair resolves onto a core "
        "key before ``async_sync`` runs. If it does, add it here with that "
        "reason. If it does not, the sweep needs the guard that plan defers, "
        "and the decision belongs in the plan, not in this allowlist."
    )


def test_frozen_pairs_have_no_stale_entries() -> None:
    """Stale self-test: the freeze must cut both ways.

    Without this a producer can be deleted and its pair linger, and the ratchet
    then licenses re-adding it later without a decision.
    """

    observed = _observed_pairs()
    stale = FROZEN_PAIRS - observed
    assert not stale, (
        f"Frozen pairs no longer produced by any writer: {sorted(stale)}. "
        "Drop them here so the census keeps describing the tree rather than its "
        f"history; {_PLAN} carries the reasoning."
    )


@pytest.mark.parametrize("name", sorted(FROZEN_CONSTANT_VALUES))
def test_frozen_constant_names_resolve_to_frozen_values(name: str) -> None:
    """The names above are only as good as what they are bound to."""

    observed = _observed_constant_values()
    assert name in observed, (
        f"``{name}`` is no longer a module-level string constant in "
        f"``const.py``. The pair census in this file freezes constant *names*; "
        "a name that no longer resolves makes the freeze vacuous."
    )
    assert observed[name] == FROZEN_CONSTANT_VALUES[name], (
        f"``{name}`` changed from {FROZEN_CONSTANT_VALUES[name]!r} to "
        f"{observed[name]!r}. Stored subentries carry the old value, so this is "
        f"a migration, not a rename; {_PLAN} carries the deletion paths that "
        "compare stored keys against these constants."
    )


def test_async_sync_caller_census_is_frozen() -> None:
    """Class A2. ``desired`` is the safe set; a narrower one deletes.

    The sweep removes every managed key *outside* ``desired``. That inverts the
    flow-side polarity, where a non-core key is the skipped one, and it is why a
    caller passing fewer keys does not merely sync less: it hands the difference
    to ``async_remove``.
    """

    observed = _observed_sync_callers()
    assert observed == set(FROZEN_SYNC_CALLERS), (
        f"The ``async_sync`` caller census changed.\n"
        f"  expected: {sorted(FROZEN_SYNC_CALLERS)}\n"
        f"  observed: {sorted(observed)}\n"
        "A new caller, or one whose ``desired`` is narrower than the core key "
        "set, turns every key outside that set into a removal candidate in "
        "``ConfigEntrySubEntryManager.async_sync``, registry bindings included. "
        f"{_PLAN} defers the type guard on that sweep precisely because both "
        "known callers pass the full core key set. Next step: read the new "
        "caller's ``desired``. If it is the full core key set, record it here "
        "with that reason. If it is narrower, the deferred guard is now owed, "
        "and that decision belongs in the plan rather than in this allowlist."
    )


def test_definition_keys_are_frozen() -> None:
    """Class A2. The other bracket around ``desired``.

    A key added here without a matching entry in the pair census means a group
    can be built that no writer census knows about; a key that vanishes means
    ``desired`` silently shrank for every caller at once.
    """

    observed = _observed_definition_keys()
    assert observed == set(FROZEN_DEFINITION_KEYS), (
        f"The definition key census changed.\n"
        f"  expected: {sorted(FROZEN_DEFINITION_KEYS)}\n"
        f"  observed: {sorted(observed)}\n"
        f"{_PLAN} rests on ``desired`` being exactly the core key set. Next "
        "step: if a key was added, check that the pair census above knows its "
        "``(subentry_type, group_key)`` shape; if one vanished, every managed "
        "group under it just became a removal candidate."
    )
