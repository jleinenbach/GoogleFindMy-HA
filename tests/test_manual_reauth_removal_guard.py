# tests/test_manual_reauth_removal_guard.py
"""Guard against the return of the removed manual token reauth branch.

``async_step_reauth_confirm`` used to carry a second credential path: a raw
OAuth token pasted into the reauth form, persisted directly. Its form field was
commented out, and ``_interpret_reauth_choice`` had no ``return`` that could
produce ``"manual"`` either, so the branch was unreachable on the UI path *and*
on the code path. Testing an unreachable branch would have rebuilt the contract
instead of checking it, so the branch was removed.

Three mechanisms, because each is blind to the others' regression:

* **Vocabulary** (:func:`test_no_manual_reauth_branch_remains_in_config_flow`) --
  a revert, a cherry-pick or a copied snippet brings the old ``if method ==
  "manual":`` back verbatim. That is caught lexically.
* **Semantics** (:func:`test_interpret_reauth_choice_cannot_yield_manual`) -- a
  reimplementation under new names would pass the sweep, but it still has to make
  the reauth interpreter hand out a manual method. That is caught structurally,
  by reading the function's ``return`` statements.
* **Wiring** (:func:`test_reauth_confirm_reads_only_the_reauth_interpreter`) --
  the semantic check pins one function, so swapping the call site to the *other*
  interpreter in this module, which returns ``"manual"`` today, would evade it
  while both other checks stay green. That is caught by pinning which interpreter
  the step is allowed to call.

**Honest about the reach.** The sweep is scoped to ``config_flow.py``, not to the
tree, and that is deliberate rather than lazy: the word ``manual`` stays alive in
three neighbours that were *not* removed. Two are steps, the initial-setup
``async_step_individual_tokens`` and the options-flow credential refresher
``async_step_credentials``, together with the ``"manual"`` source label they hand
to ``async_pick_working_token``. The third is ``_interpret_credentials_choice``,
the interpreter shared by ``async_step_secrets_json`` and
``async_step_individual_tokens``; it *does* return ``"manual"`` as its method
element and is simply not wired to ``async_step_reauth_confirm``.
``async_step_credentials`` reaches the same vocabulary without it, through the
``("manual", ...)`` candidate label it builds inline. A repo-wide sweep for that
vocabulary would flag all three on every run.

Because that third neighbour exists, the wiring is pinned as well
(:func:`test_reauth_confirm_reads_only_the_reauth_interpreter`): without it a
revival would not need a new manual branch at all, only a swap of the
interpreter at the call site. That swap is a handful of lines rather than one,
since the other interpreter takes three keyword arguments and returns a
4-tuple, but it introduces no forbidden word and changes no ``return`` in the
pinned function, so neither of the other two checks would notice.
All three are regression guards, not a proof that the surface cannot return; a
deliberate reimplementation is a review decision, not a grep.

Unlike ``tests/test_track_b_removal_guard.py`` this file needs no fragmented
patterns and no self-exclusion: it scans a single production file, never itself.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_FLOW = _REPO_ROOT / "custom_components" / "googlefindmy" / "config_flow.py"

# The vocabulary of the removed branch. Every pattern was measured to have zero
# hits after the removal and to leave the surviving neighbours untouched; the
# bare word ``manual`` and the bare field name ``new_oauth_token`` are therefore
# NOT searched (both live on in the options flow).
_PATTERNS: tuple[str, ...] = (
    # The branch head itself, in both places it stood: the success path and the
    # multi-entry-guard deferral. ``method == "manual"`` without the leading
    # ``if`` would also match the surviving assertion in
    # ``async_step_individual_tokens``.
    r'if method == "manual":',
    # The local the deferral branch bound the pasted token to.
    r"\bmanual_token\b",
    # The form field itself, in either schema branch and independent of the
    # marshalling type: the removed lines happened to read ``: str``, but the
    # surviving neighbour in the same schema uses ``selector(...)``, so an
    # organic revival would mirror that rather than the removed spelling.
    r"vol\.(?:Optional|Required)\(\s*_REAUTH_FIELD_TOKEN",
    r"manual reauth token path",
)
_SWEEP = re.compile("|".join(_PATTERNS))

_INTERPRETER = "_interpret_reauth_choice"
_REAUTH_STEP = "async_step_reauth_confirm"
# What the interpreter is allowed to hand back as its ``method`` element.
_ALLOWED_METHODS = frozenset({None, "secrets"})


def test_no_manual_reauth_branch_remains_in_config_flow() -> None:
    """No part of the config flow may carry the removed branch's vocabulary.

    The scan runs over the whole file rather than line by line, because one
    pattern spans a call head and its argument (``vol.Optional(`` and the
    field name, separated by ``\\s*``). ``ruff-format`` wraps exactly there
    once the line grows, and a line-wise scan would let the wrapped form
    through. Every hit is reported, not just the first, so a fix pass can
    clear the finding in one go instead of rediscovering it match by match.
    """

    text = _CONFIG_FLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    hits: list[str] = []
    for match in _SWEEP.finditer(text):
        number = text.count("\n", 0, match.start()) + 1
        hits.append(f"config_flow.py:{number}: {lines[number - 1].strip()}")

    assert not hits, (
        "the manual token reauth branch was removed because neither the form nor "
        "the interpreter could reach it; these lines bring it, or a stale mention "
        "of it, back:\n" + "\n".join(hits)
    )


def _returned_methods() -> set[object]:
    """Return the literal ``method`` values ``_interpret_reauth_choice`` can yield.

    A ``return`` whose first tuple element is not a literal is reported as an
    opaque marker rather than ignored, so the check fails closed when the shape
    changes instead of quietly passing.
    """

    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == _INTERPRETER
    ]
    assert len(functions) == 1, (
        f"expected exactly one definition of {_INTERPRETER}, found "
        f"{len(functions)}; the guard below reads only one of them"
    )

    methods: set[object] = set()
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Return):
            continue
        value = node.value
        if isinstance(value, ast.Tuple) and len(value.elts) == 3:
            first = value.elts[0]
            methods.add(
                first.value
                if isinstance(first, ast.Constant)
                else f"<non-literal {type(first).__name__}>"
            )
        elif isinstance(value, ast.Tuple):
            # Reading slot 0 of a reshaped tuple would silently inspect the wrong
            # element, so a changed arity is reported instead of guessed.
            methods.add(f"<arity {len(value.elts)} instead of 3>")
        else:
            methods.add(f"<non-tuple {type(value).__name__}>")
    return methods


def test_interpret_reauth_choice_cannot_yield_manual() -> None:
    """The interpreter must not regain a way to announce a manual method.

    This is the part a vocabulary sweep cannot do. What is pinned here is the
    *method literal*, not the effect: together with the wiring check below it
    means no method element other than ``"secrets"`` can reach the branch
    dispatcher in ``async_step_reauth_confirm``, whatever a revived branch would
    call itself. A payload smuggled past a ``None`` method is a different failure
    and is not this test's subject.
    """

    methods = _returned_methods()

    assert methods, f"{_INTERPRETER} has no tuple returns left to inspect"
    assert methods <= _ALLOWED_METHODS, (
        f"{_INTERPRETER} may only hand back {sorted(_ALLOWED_METHODS, key=str)} as "
        f"its method element; it now also returns "
        f"{sorted(methods - _ALLOWED_METHODS, key=str)}. Reviving the manual "
        "reauth path needs an amendment in "
        "custom_components/googlefindmy/agents/config_flow/AGENTS.md, not a new "
        "return here."
    )


def test_reauth_confirm_reads_only_the_reauth_interpreter() -> None:
    """The step must not start reading its method from another interpreter.

    This module has a second one, ``_interpret_credentials_choice``, which serves
    the initial flow and the options flow and returns ``"manual"`` today. Pointing
    the reauth step at it would revive the surface without adding a single
    forbidden word and without changing any ``return`` in the pinned function, so
    neither of the other two checks would fire. Pinning the call site turns the
    premise of that reasoning into a checked fact.
    """

    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    steps = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == _REAUTH_STEP
    ]
    assert len(steps) == 1, (
        f"expected exactly one definition of {_REAUTH_STEP}, found {len(steps)}; "
        "the check below reads only one of them"
    )

    called = {
        node.func.id
        for node in ast.walk(steps[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("_interpret_")
    }

    assert called == {_INTERPRETER}, (
        f"{_REAUTH_STEP} may only interpret its input through {_INTERPRETER}; it "
        f"now calls {sorted(called) or 'none of the interpreters'}. "
        f"{_INTERPRETER} is the one function pinned by "
        "test_interpret_reauth_choice_cannot_yield_manual, so reading the method "
        "from anywhere else silently removes that protection."
    )
