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

Three deliberate blind spots, stated rather than implied, and all wider than an
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
* the collectors recognise the definition by the *name* ``ConfigEntrySubentry-
  Definition``, a handler by its ``class`` statement, and a field by an
  assignment inside that statement. Ten constructions reaching the same runtime
  shape are therefore invisible, each measured against the collectors rather
  than assumed. Six replace the *call*: a local alias
  (``Def = ConfigEntrySubentryDefinition``), an import alias
  (``import X as Def``), ``dataclasses.replace``, ``functools.partial``, a
  ``@dataclass`` subclass overriding the default, and ``type(...)`` as a class
  factory. Two replace the *class body*: ``setattr`` or a plain assignment onto
  a handler after its ``class`` statement. **The likeliest of the ten is the
  ninth**: a handler subclass overriding one field and inheriting the other, for
  instance ``class Archive(TrackerSubentryFlowHandler): _group_key = ...``. The
  pair census needs both halves in one class body, so it reads the parent's
  shape and stays green -- and subclassing a handler is the most ordinary
  extension of this tree, not an exotic one. The tenth is a binding form no
  assignment statement carries: a walrus, a starred target, a ``for`` or
  ``with`` target. Closing the first nine needs alias and dataflow resolution,
  which is a different tool; ``async_sync`` itself type-checks nothing (it reads
  attributes off whatever it is handed), so no runtime check narrows this
  either. What holds the line instead is that all four definition call sites
  construct the class directly and all three handlers declare both fields in
  their own body today, and every one of the ten would be a visible, unusual
  edit in review.

What the census does cover is the *creation* of a group, which is where a new
shape enters the tree. The first two blind spots rewrite an existing group
rather than create one; the third is a limit of static name matching and is the
only one that could hide a creation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "custom_components" / "googlefindmy"
CONST_MODULE = PACKAGE / "const.py"

# Observation 1: the (subentry_type, group_key) pairs a production writer can
# produce, as constant *names*. For the two forms that carry both halves in one
# place -- a flow-handler class body, and a ``ConfigEntrySubentryDefinition(...)``
# call -- no *value* escapes: a half this census cannot read becomes a marker
# rather than a dropped entry, whether it is a literal, an expression, a
# positional argument, an omitted keyword or a rebinding. What it does not
# survive is a different *construction*, nor a binding form no assignment
# statement carries; the module docstring states both as the third blind spot.
# An entry starting with ``<`` is such a marker and never belongs in this set:
# freezing one would silence the very construction it reports.
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


# Markers for a binding that exists but this census cannot read. They are shapes
# like any other: absent from the frozen sets, so the ratchet fails and *names*
# the construction instead of skipping it. The policy they encode is one rule,
# applied by every collector below: a binding that exists and cannot be read is
# reported; a binding that does not exist stays silent. Only the second half is a
# genuine non-event, and the distinction is load-bearing -- ``_BaseSubentryFlow``
# annotates both fields without assigning them (``_group_key: str``), which binds
# nothing and must not be reported.
_OPAQUE = "<opaque>"
_POSITIONAL = "<positional>"
_TYPE_DEFAULTED = "<subentry_type defaulted>"
_KWARGS = "<**kwargs>"


def _bound_name(value: ast.expr | None) -> str | None:
    """Render an assigned value as a comparable token, or ``None`` if opaque.

    A constant is rendered as its ``repr`` rather than dropped. Dropping is the
    dangerous direction: a handler written with string literals instead of the
    shared constants would vanish from the census entirely, and the ratchet
    would stay green while a fourth shape entered the tree.

    ``None`` means *unreadable*, not *absent*. Callers must distinguish the two:
    every one of them turns ``None`` into a marker, and only a missing statement
    is allowed to produce nothing. An earlier draft let three of the four
    collectors drop unreadable bindings silently, which is the very direction
    this docstring warns about.
    """

    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return repr(value.value)
    return None


_PAIR_FIELDS = frozenset({"_subentry_type", "_group_key"})


def _class_body_statements(node: ast.ClassDef) -> Iterator[ast.stmt]:
    """Statements binding attributes of *this* class, nested blocks included.

    ``node.body`` alone misses a field written inside an ``if``, ``try``, ``with``
    or ``match`` block in the class body, and ``ast.walk`` would over-collect: it
    descends into methods, where a local of the same name is not a class
    attribute, and into inner classes, whose fields belong to them. Both are
    walked separately by the caller's own ``ast.walk`` over the module, so
    skipping them here loses nothing.

    Yielded in **source order**, which is load-bearing rather than cosmetic: a
    field bound twice is resolved by the last binding at runtime, and a draft of
    this helper used a LIFO stack, so the *first* binding won. A class rebinding
    ``_group_key`` to a fresh value then read as its old one -- a shape that was
    loud before this helper existed went quiet. The caller no longer depends on
    the order (it reports a divergent rebinding as opaque), but a reader does.
    """

    def walk(body: list[ast.stmt]) -> Iterator[ast.stmt]:
        for stmt in body:
            yield stmt
            if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for field in ("body", "orelse", "finalbody"):
                yield from walk(getattr(stmt, field, None) or [])
            for handler in getattr(stmt, "handlers", None) or []:
                yield from walk(handler.body)
            for case in getattr(stmt, "cases", None) or []:
                yield from walk(case.body)

    yield from walk(node.body)


def _assignment_bindings(stmt: ast.stmt) -> Iterator[tuple[ast.expr, ast.expr | None]]:
    """Yield ``(target, value)`` for every name a statement binds.

    Four forms, and the third is why this helper exists rather than an inline
    branch: ``x = V``, ``x: str = V``, ``a, b = V1, V2``, and ``x += V``. The
    tuple form passed the earlier ``len(stmt.targets) == 1`` check and then
    failed the ``isinstance(target, ast.Name)`` one, so a handler written that
    way vanished. A bare ``x: str`` yields nothing on purpose: it binds no
    value, which is a real non-event rather than an unreadable one.

    Four further binding forms are *not* read, and the module docstring names
    them as part of the third blind spot: a walrus inside an expression, a
    starred tuple target, and ``for``/``with`` targets. All four bind a class
    attribute in principle and none is a plausible spelling for these two fields
    -- measured as silent rather than assumed, and stated instead of covered,
    because enumerating statement shapes is the same pattern-guessing this file
    exists to warn about.
    """

    if isinstance(stmt, ast.AnnAssign):
        if stmt.value is not None:
            yield stmt.target, stmt.value
        return
    if isinstance(stmt, ast.AugAssign):
        # ``_group_key += SUFFIX`` rebinds the field to a value derived from the
        # old one. The result is unreadable, and reporting nothing would let the
        # rebinding hide behind the earlier plain assignment.
        yield stmt.target, None
        return
    if not isinstance(stmt, ast.Assign):
        return
    for target in stmt.targets:
        if not isinstance(target, ast.Tuple):
            yield target, stmt.value
            continue
        values: list[ast.expr | None]
        if isinstance(stmt.value, ast.Tuple) and len(stmt.value.elts) == len(
            target.elts
        ):
            values = list(stmt.value.elts)
        else:
            # ``a, b = build()`` binds both names to values this census cannot
            # read. Reporting them as opaque is the point; dropping them is what
            # ``_bound_name`` warns about.
            values = [None] * len(target.elts)
        yield from zip(target.elts, values)


def _class_attribute_pairs(tree: ast.Module) -> set[tuple[str, str]]:
    """Pairs written as sibling class attributes (the flow handlers).

    Both ``x = VALUE`` and ``x: str = VALUE`` are read. The annotated form is
    not hypothetical: ``_BaseSubentryFlow`` already annotates both fields, so a
    subclass repeating the annotation is the likelier spelling, and an earlier
    draft of this census saw only the bare form.

    An unreadable value (``const.ARCHIVE_KEY``, an f-string, a call) is recorded
    as ``<opaque>`` rather than dropped. Dropping it lost the whole handler,
    because the pair needs both halves, so a single unreadable field hid a
    complete new shape.

    A field bound twice with *different* tokens is opaque too. Reporting the
    survivor would mean choosing a reading this census cannot justify: a second
    plain assignment rebinds (last wins), while two assignments in opposite
    branches of an ``if`` are two possible runtime values and neither is *the*
    shape.
    """

    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        attrs: dict[str, str] = {}
        for stmt in _class_body_statements(node):
            for target, value in _assignment_bindings(stmt):
                if not (isinstance(target, ast.Name) and target.id in _PAIR_FIELDS):
                    continue
                token = _bound_name(value) or _OPAQUE
                previous = attrs.get(target.id)
                attrs[target.id] = token if previous in (None, token) else _OPAQUE
        if "_subentry_type" in attrs and "_group_key" in attrs:
            pairs.add((attrs["_subentry_type"], attrs["_group_key"]))
    return pairs


def _definition_call_pairs(tree: ast.Module) -> set[tuple[str, str]]:
    """Pairs carried by a ``ConfigEntrySubentryDefinition`` call.

    Keyword arguments are read; every other construction of the *same* call is
    reported as a marker rather than skipped -- ``**kwargs``, positional
    arguments, an omitted ``subentry_type`` (which has a dataclass default), and
    an argument whose value is an expression.
    """

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
            pairs.add((_KWARGS, _KWARGS))
            continue
        if node.args:
            # Positional construction. ``key`` is the first field today, but
            # reading ``args[0]`` as the key would freeze the field *order* into
            # this census, and a reordered dataclass would then mislabel shapes
            # rather than fail. Report the construction instead.
            pairs.add((_POSITIONAL, _POSITIONAL))
            continue
        kwargs = {
            kw.arg: kw.value
            for kw in node.keywords
            if kw.arg in {"key", "subentry_type"}
        }
        # ``_bound_name(None)`` is ``None``, so a missing and an unreadable key
        # collapse into the same marker on purpose: neither tells the census
        # which group is being built.
        key = _bound_name(kwargs.get("key")) or _OPAQUE
        if "subentry_type" not in kwargs:
            # The field carries a dataclass default, so omitting it is a valid
            # construction that still produces a pair. The earlier version
            # required both keywords and dropped this call entirely. Resolving
            # the default statically was considered and rejected: it lives in
            # another module than two of the four call sites, so it would need
            # cross-file resolution plus the fresh assumption that the default
            # stays an ``ast.Name`` -- and it would fail *silently* the day it
            # becomes a ``field(default_factory=...)``. The readable half is
            # kept so the failure message names the key rather than only the
            # omission.
            pairs.add((_TYPE_DEFAULTED, key))
            continue
        pairs.add((_bound_name(kwargs["subentry_type"]) or _OPAQUE, key))
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

    One form is knowingly *not* covered: ``getattr(manager, name, None)`` where
    the attribute is a variable. Covering it means reporting every dynamic
    ``getattr`` as a possible call site, and there are 25 of them in the package
    (measured), none related to this manager. A ratchet that cries 25 times is
    one an allowlist silences, so the limit is stated here instead of widened.
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
            if names_async_sync(node):
                # A call outside any function (module level, a ``lambda``, a
                # comprehension) used to be dropped for having an empty stack,
                # which is the same silent direction as the collectors above.
                # There is none today, so naming it costs nothing and the frozen
                # set will reject the first one.
                callers.add((rel, stack[-1] if stack else "<module>"))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                stack.pop()

        visit(tree)
    return callers


def _observed_definition_keys() -> set[str]:
    """Every ``key`` passed to a definition anywhere in the package.

    Read through ``_bound_name`` like every other collector. An earlier version
    accepted ``ast.Name`` only, so ``key="archive"`` left no trace here, and
    when that call also omitted ``subentry_type`` the pair census dropped it
    too: a new group key entered the runtime path with both ratchets green. The
    rule the helper's own docstring states was written two hundred lines above
    the one collector that did not follow it.
    """

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
            if any(kw.arg is None for kw in node.keywords):
                keys.add(_KWARGS)
                continue
            if node.args:
                keys.add(_POSITIONAL)
                continue
            for kw in node.keywords:
                if kw.arg == "key":
                    keys.add(_bound_name(kw.value) or _OPAQUE)
    return keys


def test_producible_subentry_pairs_are_frozen() -> None:
    """Class A2 (standing assertion). Freeze the shapes, not their number."""

    observed = _observed_pairs()
    added = observed - FROZEN_PAIRS
    assert not added, (
        f"A new (subentry_type, group_key) pair appeared: {sorted(added)}. "
        "An entry starting with ``<`` is not a pair at all but a binding this "
        "census could not read (a positional call, an omitted ``subentry_type``, "
        "an expression, a rebinding). Never freeze one: that silences the "
        "construction instead of the drift. Change the spelling so both halves "
        "are readable, or widen the collector. For a real pair: "
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
        f"{_PLAN} rests on ``desired`` being exactly the core key set. An "
        "observed entry starting with ``<`` is an unreadable binding, not a "
        "key, and freezing it would hide the call site that produced it. Next "
        "step: if a key was added, check that the pair census above knows its "
        "``(subentry_type, group_key)`` shape; if one vanished, every managed "
        "group under it just became a removal candidate."
    )


# The collectors' own contract: an unreadable binding is reported, an absent one
# is not. Without this table the rule lives only in prose, and prose did not
# protect the sibling function: ``_bound_name`` stated it two hundred lines
# above the one collector that ignored it, and the gap was found by an external
# reviewer rather than by this file. Each row is a spelling that reaches the
# same runtime shape as a supported one; ``expected`` is what the census must
# make of it.
_SPELLINGS: tuple[tuple[str, str, set[tuple[str, str]]], ...] = (
    (
        "keyword names, the four production call sites",
        "ConfigEntrySubentryDefinition(key=K, title=t, data=d, subentry_type=T)",
        {("T", "K")},
    ),
    (
        "literal key, type named",
        'ConfigEntrySubentryDefinition(key="a", title=t, data=d, subentry_type=T)',
        {("T", "'a'")},
    ),
    (
        "subentry_type omitted, so its dataclass default applies",
        "ConfigEntrySubentryDefinition(key=K, title=t, data=d)",
        {(_TYPE_DEFAULTED, "K")},
    ),
    (
        "literal key and omitted type, the reported shape: both halves were "
        "dropped before, the readable one is kept now",
        'ConfigEntrySubentryDefinition(key="a", title=t, data=d)',
        {(_TYPE_DEFAULTED, "'a'")},
    ),
    (
        "positional construction",
        'ConfigEntrySubentryDefinition("a", t, d, T)',
        {(_POSITIONAL, _POSITIONAL)},
    ),
    (
        "key behind an attribute expression",
        "ConfigEntrySubentryDefinition(key=const.K, title=t, data=d, subentry_type=T)",
        {("T", _OPAQUE)},
    ),
    (
        "type behind an attribute expression",
        "ConfigEntrySubentryDefinition(key=K, title=t, data=d, subentry_type=const.T)",
        {(_OPAQUE, "K")},
    ),
    (
        "handler fields behind attribute expressions",
        "class F:\n    _subentry_type = const.T\n    _group_key = const.K\n",
        {(_OPAQUE, _OPAQUE)},
    ),
    (
        "handler field built by an f-string",
        'class F:\n    _subentry_type = T\n    _group_key = f"{P}_a"\n',
        {("T", _OPAQUE)},
    ),
    (
        "handler fields bound by one tuple assignment",
        "class F:\n    _subentry_type, _group_key = T, K\n",
        {("T", "K")},
    ),
    (
        "handler fields inside a conditional block",
        "class F:\n    if True:\n        _subentry_type = T\n        _group_key = K\n",
        {("T", "K")},
    ),
    (
        "handler fields inside a match case",
        "class F:\n    match FLAG:\n        case 1:\n"
        "            _subentry_type = T\n            _group_key = K\n",
        {("T", "K")},
    ),
    (
        "a field rebound to a different value is not resolvable statically",
        "class F:\n    _subentry_type = T\n    _group_key = K\n    _group_key = A\n",
        {("T", _OPAQUE)},
    ),
    (
        "a field rebound in a conditional branch, same reason",
        "class F:\n    _subentry_type = T\n    _group_key = K\n"
        "    if FLAG:\n        _group_key = A\n",
        {("T", _OPAQUE)},
    ),
    (
        "an augmented assignment rebinds to an unreadable value",
        "class F:\n    _subentry_type = T\n    _group_key = K\n    _group_key += S\n",
        {("T", _OPAQUE)},
    ),
    (
        "rebinding to the same token is not a second shape",
        "class F:\n    _subentry_type = T\n    _group_key = K\n    _group_key = K\n",
        {("T", "K")},
    ),
    (
        "bare annotations bind nothing, so they are a real non-event",
        "class F:\n    _subentry_type: str\n    _group_key: str\n",
        set(),
    ),
    (
        "a subclass inheriting one half is silent: the ninth blind construction, "
        "pinned here so the gap is documented rather than believed closed",
        "class Archive(TrackerSubentryFlowHandler):\n    _group_key = A\n",
        set(),
    ),
    (
        "locals inside a method are not class attributes",
        "class F:\n    def m(self):\n        _subentry_type = T\n        _group_key = K\n",
        set(),
    ),
)


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    list(_SPELLINGS),
    ids=[label for label, _, _ in _SPELLINGS],
)
def test_collectors_report_unreadable_bindings_instead_of_dropping_them(
    label: str, source: str, expected: set[tuple[str, str]]
) -> None:
    """Class A2 (standing assertion). The policy, not a single collector.

    Eight mutations, each run against this table rather than assumed, and each
    killing exactly the rows named and no others:

    * neutralising the rebinding check in ``_class_attribute_pairs``
      (``previous in (None, token)``) kills the two rebinding rows;
    * replacing its ``or _OPAQUE`` with a ``continue`` kills the two
      expression rows and the augmented-assignment row;
    * dropping the ``ast.AugAssign`` branch of ``_assignment_bindings`` kills
      the augmented-assignment row;
    * dropping the ``cases`` traversal in ``_class_body_statements`` kills the
      match-case row;
    * dropping its tuple-target branch kills the tuple row;
    * dropping the ``node.args`` branch of ``_definition_call_pairs`` kills the
      positional row;
    * dropping its ``_TYPE_DEFAULTED`` branch kills the two omitted-type rows;
    * replacing its ``or _OPAQUE`` on ``subentry_type`` kills the
      ``type behind an attribute expression`` row.

    That last one killed *nothing* on its first run, which is how the row it now
    covers was found: the fallback existed with no case exercising it. The three
    silent rows stay green under all eight, since they assert silence rather
    than a marker -- and one of them, the inheriting subclass, pins a gap the
    module docstring declares rather than a behaviour it closes.
    """

    tree = ast.parse(source)
    observed = _class_attribute_pairs(tree) | _definition_call_pairs(tree)
    assert observed == expected, (
        f"The census read {sorted(observed)} from the {label!r} spelling, "
        f"expected {sorted(expected)}. Its collectors must report a binding "
        "they cannot read as a marker and stay silent only where no binding "
        "exists; a dropped binding is a shape entering the tree with the "
        f"ratchet green, which is what {_PLAN} exists to notice."
    )
