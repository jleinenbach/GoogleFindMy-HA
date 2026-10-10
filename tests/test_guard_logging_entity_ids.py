# tests/test_guard_logging_entity_ids.py
"""Guard: no log call above DEBUG names an `entity_id` unless the record is
bound to one invocation.

`AGENTS.md` section 5 (b): a record that can repeat unattended (polling,
transport recovery, background sweeps, state-change listeners) keeps the
device name out of the record at INFO and above and carries it only at
DEBUG (a count, an index or a class (a)/(b) identifier may stand in its
place); an `entity_id` is a slug of that name and follows the same rule. A record bound
to one invocation (a service call, a button press) may name the entity at any
level, because the caller asked for that entity and has to see which one
failed.

The guard recognises the value at the identifier: a name or attribute that
is exactly `entity_id`, the constant `"entity_id"` (the `getattr(entry,
"entity_id", ...)` form) or a format string that spells `entity_id` directly
in front of a placeholder (`"entity_id=%s"`), reachable from an argument of a
log call whose level is INFO or above. A word in a sentence (`"missing
entity_id for tracker"`) or a plural (`entity_ids`) is not a match, because a
correct sentence needs the word too (`tests/AGENTS.md`, forbidden-token
guards). Every match is an offender unless it is listed in
`_INVOCATION_BOUND` with its path, the leaf and a prefix of the format
string; each entry must match exactly one call so that a copied line does
not inherit the exception. Of the repository's log wrappers (listed in
`test_guard_logging_payloads.py`) only `_log_warn_with_limit` emits above
DEBUG and counts here; `_log_verbose` emits DEBUG only.

Written as an AST walk over the package rather than a pin on the thirteen
sites found on 2026-09-18, because the shape reappears with every new log
line. Scope is INFO and above only: at DEBUG the `entity_id` is allowed.

Blind spots, declared: a value copied into a variable that is not spelled
`entity_id` (`eid = entry.entity_id`), an entity object rendered through
`str()`, a logger method bound to a local name (`log_fn = _LOGGER.warning`
in `NovaApi/nova_request.py`, `Auth/aas_token_retrieval.py`,
`coordinator/registry.py`; the receiver rule sees no `LOG` in `log_fn`),
and a library exception whose own text names the entity (the
registry's `ValueError` on a unique_id clash does); the last one is why the
migration errors in `coordinator/registry.py` state the rejection in fixed
words at ERROR and log the text at DEBUG. The caplog tests next to each fixed
site pin the record contents, this guard pins the identifier shape.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from custom_components import googlefindmy
from tests.test_guard_logging_payloads import _LOG_WRAPPERS

_PACKAGE_ROOT = Path(googlefindmy.__file__).resolve().parent

# `_log_verbose` (fcmpushclient.py, fcmregister.py) emits DEBUG only, so of
# the shared wrapper list only the WARNING wrapper belongs to this scope.
_WRAPPERS_ABOVE_DEBUG = frozenset({"_log_warn_with_limit"})
assert _WRAPPERS_ABOVE_DEBUG <= _LOG_WRAPPERS

# A format string names the value only where `entity_id` stands directly in
# front of a placeholder; `%s` after up to three punctuation characters.
_SPELLED_PLACEHOLDER = re.compile(r"entity_id\W{0,3}%[sdr]")

_LEVELS_ABOVE_DEBUG = frozenset(
    {"info", "warning", "warn", "error", "exception", "critical", "log"}
)

# Invocation-bound records reviewed on 2026-09-18 (PLAN_GFMY_LOGGING_RESTBESTAND
# AP3, V6): `async_rebuild_device_registry` runs once per service call and the
# caller has to see which legacy entity was removed or could not be removed.
_INVOCATION_BOUND: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("services.py", "entity_id", "[%s] Tracker cleanup: removing"),
        ("services.py", "entity_id", "[%s] Tracker cleanup: failed"),
    }
)


def _entity_id_leaves(node: ast.AST) -> set[str]:
    """Leaves under ``node`` that name or spell ``entity_id``.

    Spelled forms normalise to the identifier so that the exception list
    names one leaf per site.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "entity_id":
            found.add("entity_id")
        elif isinstance(child, ast.Attribute) and child.attr == "entity_id":
            found.add("entity_id")
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value == "entity_id" or _SPELLED_PLACEHOLDER.search(child.value):
                found.add("entity_id")
    return found


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        # A wrapper called bare, without a receiver.
        return func.id in _WRAPPERS_ABOVE_DEBUG
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _WRAPPERS_ABOVE_DEBUG:
        return True
    if func.attr not in _LEVELS_ABOVE_DEBUG:
        return False
    return "LOG" in ast.unparse(func.value).upper()


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
    reviewed: frozenset[tuple[str, str, str]] = _INVOCATION_BOUND,
) -> tuple[list[tuple[str, int, str, str]], int]:
    """Return ((path, line, leaf, format string) offenders, scanned calls).

    One offender per log call and leaf; a call that names the entity twice
    (positional value and spelled format string) is one offender per leaf
    name, so at most one after normalisation.
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
            msg_index = (
                1
                if isinstance(node.func, ast.Attribute) and node.func.attr == "log"
                else 0
            )
            first = node.args[msg_index] if len(node.args) > msg_index else None
            fmt = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else ""
            )
            leaves: set[str] = set()
            for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                leaves |= _entity_id_leaves(argument)
            for leaf in sorted(leaves):
                if _is_reviewed(reviewed, relative, leaf, fmt):
                    continue
                offenders.append((relative, node.lineno, leaf, fmt))
    return offenders, scanned


def test_no_repeatable_record_above_debug_names_an_entity_id() -> None:
    offenders, scanned = scan()
    # Vacuum guard: this scanner saw 698 log calls above DEBUG on
    # 2026-09-18; a scan that sees far fewer did not walk the package (wrong
    # root, parse failure, renamed logger).
    assert scanned > 600, (
        f"only {scanned} log calls above DEBUG scanned; the guard would be vacuous"
    )
    assert offenders == [], (
        "Log calls above DEBUG name an entity_id in a record that can repeat "
        "unattended (AGENTS.md section 5 b): keep the entity_id out of this "
        "level (a count, an index or a class (a)/(b) identifier may stand in its "
        "place) and log it in a DEBUG sibling, or, if the record is bound "
        "to one invocation, add (path, leaf, format-string prefix) to "
        "`_INVOCATION_BOUND` with the reason:\n"
        + "\n".join(f"{path}:{line}: {leaf}" for path, line, leaf, _fmt in offenders)
    )


def test_each_invocation_bound_site_matches_exactly_one_log_call() -> None:
    """The exception list excuses one vetted call per entry, no more, no less.

    Fewer than one: the entry outlived the site it excuses. More than one: a
    log call was copied or moved and kept the vetted prefix, so a repeatable
    record could name the entity while this guard stays green.
    """
    offenders, _ = scan(reviewed=frozenset())
    matches = {
        site: [
            (path, line)
            for path, line, leaf, fmt in offenders
            if _is_reviewed(frozenset({site}), path, leaf, fmt)
        ]
        for site in _INVOCATION_BOUND
    }
    wrong = {site: hits for site, hits in matches.items() if len(hits) != 1}
    assert wrong == {}, (
        "each invocation-bound site must match exactly one log call "
        f"(0 = stale entry, >1 = copied site): {wrong}"
    )
