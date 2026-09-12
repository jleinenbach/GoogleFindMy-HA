# tests/test_guard_device_registry_kwargs.py
"""Static ratchet against the pre-2026.8 device registry API.

Two kinds of check live here. The larger part is an AST ratchet over the
production tree, described below. The smaller part, at the end of the file,
reads the three ``AGENTS.md`` contracts and holds their polarity, because the
same migration that removed the old calls also removed the sentences that told
an agent to write them. Both guard one rule, so they share a file.

Home Assistant 2026.8 made a device belong to exactly one config entry and
subentry.  Five shapes in this repository spoke the old model when this gate
landed, and each of them fails differently.  Since ``N-22`` the ratchet below is
empty, so the list reads as the taxonomy the scanner still looks for rather than
as an inventory of today's tree:

1. the four ownership kwargs (``add_config_entry_id`` and friends),
2. the same names as **strings**, used as dict keys or subscripts,
3. ``DeviceEntry.config_entries``, a shim that now always yields one element, so
   any ``len()`` or set-difference on it has lost its meaning,
4. the ``DeviceRegistry.devices`` mapping, in both the attribute and the
   ``getattr`` form, and
5. ``async_get_device``, whose identifier lookup is no longer unique.

The check is static and AST-based, following
``tests/test_guard_config_flow_reload_deprecation.py``: a runtime test cannot see
a call site that no test happens to exercise, and a grep cannot tell
``hass.config_entries`` (fine, that is the entry manager) from
``device.config_entries`` (the deprecated shim).

**Direction matters.**  Rules 3 and 5 use an *allow* list of receivers rather
than a *deny* list of device variable names.  A deny list only catches names
somebody thought of: measured on this tree it would have missed
``device_entry.config_entries`` while carrying two names that occur nowhere.  An
allow list cannot make that mistake -- whatever it does not know, it reports.

**Doubt resolves to "pass, but counted".**  A construct matching none of the
lists (say ``.config_entries`` on a call result) is let through and listed as
undecidable in the failure text.  A gate that blocks on doubt gets switched off
after the second false alarm, and a switched-off gate protects nothing.

The known-violation list below is the ratchet: it held the sites of the day it
landed so the gate was green from the start, and every migration work package
shrank it.  Since ``N-22`` it is empty, which is the state it was built to reach.
The counts were part of it on purpose -- without them a *new* violation could
have hidden inside a function that was already listed.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOT = _REPO_ROOT / "custom_components" / "googlefindmy"

#: The compatibility layer is allowed to speak both dialects; that is its job.
_TRANSLATOR = "coordinator/helpers/registry.py"

_OWNERSHIP_KWARGS = frozenset(
    {
        "add_config_entry_id",
        "add_config_subentry_id",
        "remove_config_entry_id",
        "remove_config_subentry_id",
    }
)

#: Receivers for which ``.config_entries`` means the config entry *manager* or a
#: module, not the deprecated device shim.  Verified at each site on the tree:
#: ``self._hass`` and ``hass_arg`` hold a HomeAssistant instance, ``cf`` in
#: discovery.py holds the ``homeassistant`` package.  ``self`` covers
#: ``ConfigFlow.self.config_entries``; it currently matches nothing, which is
#: harmless here -- a stale entry in an *allow* list permits something that does
#: not occur, whereas a stale entry in a deny list makes the list look complete.
_CONFIG_ENTRIES_OWNERS = frozenset(
    {"hass", "_hass", "self", "self.hass", "self._hass", "hass_arg", "cf"}
)

#: Receivers whose ``.devices`` is *not* the deprecated registry mapping.  An
#: allow list for the same reason as ``_CONFIG_ENTRIES_OWNERS``: a deny list of
#: registry variable names missed ``service_device_registry`` outright and could
#: only ever catch names somebody had already thought of.  Verified at every
#: site: both names below resolve to ``SemanticLabelRecord.devices``, a
#: ``set[str]`` (``coordinator/main.py:596``), never a device registry.
_DEVICES_NON_REGISTRY_OWNERS = frozenset({"record", "self"})


@dataclass(frozen=True)
class Finding:
    """One occurrence of a deprecated shape."""

    rule: str
    path: str
    function: str
    detail: str

    @property
    def site(self) -> tuple[str, str, str]:
        """Location key without a line number, so edits do not churn the list."""
        return (self.rule, self.path, self.function)


def _receiver(node: ast.expr) -> str | None:
    """Render a simple receiver expression, or ``None`` if it is not simple."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _receiver(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the qualified name of the function containing it."""
    owner: dict[ast.AST, str] = {}

    def _walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                owner[child] = name
                _walk(child, name)
            else:
                owner[child] = prefix
                _walk(child, prefix)

    _walk(tree, "<module>")
    return owner


def _docstring_nodes(tree: ast.AST) -> set[ast.AST]:
    """Return every string constant that serves as a docstring."""
    found: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(body[0].value)
    return found


def scan_file(path: Path, relative: str) -> tuple[list[Finding], list[Finding]]:
    """Return ``(findings, undecidable)`` for one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)
    docstrings = _docstring_nodes(tree)
    findings: list[Finding] = []
    undecidable: list[Finding] = []
    in_translator = relative == _TRANSLATOR

    def _where(node: ast.AST) -> str:
        return owner.get(node, "<module>")

    for node in ast.walk(tree):
        # Rule 1 -- ownership kwargs at a call site.
        if isinstance(node, ast.Call) and not in_translator:
            for keyword in node.keywords:
                if keyword.arg in _OWNERSHIP_KWARGS:
                    findings.append(
                        Finding("kwargs", relative, _where(node), keyword.arg)
                    )

        # Rule 2 -- the same names as string literals, in any position.
        #
        # Deliberately shape-independent.  An earlier draft looked at dict keys
        # and subscripts only and missed six real call sites of the form
        # ``kwargs.setdefault("add_config_entry_id", entry_id)`` -- neither a key
        # nor a subscript, but a plain argument.  That is the same lesson the
        # migration plan drew for ``getattr``: a rule written against the shapes
        # currently in view is blind to the next one.  Docstrings are exempt,
        # since documenting the old name is how the translator explains itself.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _OWNERSHIP_KWARGS
            and node not in docstrings
            and not in_translator
        ):
            findings.append(
                Finding("kwargs_string", relative, _where(node), node.value)
            )

        # Rule 3 -- DeviceEntry.config_entries, attribute and getattr form.
        if isinstance(node, ast.Attribute) and node.attr == "config_entries":
            receiver = _receiver(node.value)
            if receiver is None:
                undecidable.append(
                    Finding(
                        "config_entries",
                        relative,
                        _where(node),
                        "receiver is not a simple name",
                    )
                )
            elif receiver not in _CONFIG_ENTRIES_OWNERS and not in_translator:
                findings.append(
                    Finding("config_entries", relative, _where(node), receiver)
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
        ):
            attribute = node.args[1].value
            receiver = _receiver(node.args[0])
            if attribute == "config_entries":
                if receiver is None:
                    undecidable.append(
                        Finding(
                            "config_entries",
                            relative,
                            _where(node),
                            "getattr receiver is not a simple name",
                        )
                    )
                elif receiver not in _CONFIG_ENTRIES_OWNERS and not in_translator:
                    findings.append(
                        Finding(
                            "config_entries",
                            relative,
                            _where(node),
                            f"getattr {receiver}",
                        )
                    )
            if attribute == "devices":
                if receiver is None:
                    undecidable.append(
                        Finding(
                            "devices",
                            relative,
                            _where(node),
                            "getattr receiver is not a simple name",
                        )
                    )
                elif receiver not in _DEVICES_NON_REGISTRY_OWNERS and not in_translator:
                    findings.append(
                        Finding(
                            "devices", relative, _where(node), f"getattr {receiver}"
                        )
                    )
            # Rule 5 -- async_get_device through getattr.
            if attribute == "async_get_device" and not in_translator:
                findings.append(
                    Finding(
                        "async_get_device",
                        relative,
                        _where(node),
                        f"getattr {receiver}",
                    )
                )

        # Rule 4 -- the DeviceRegistry.devices mapping, attribute and getattr.
        if isinstance(node, ast.Attribute) and node.attr == "devices":
            receiver = _receiver(node.value)
            if receiver is None:
                undecidable.append(
                    Finding(
                        "devices",
                        relative,
                        _where(node),
                        "receiver is not a simple name",
                    )
                )
            elif receiver not in _DEVICES_NON_REGISTRY_OWNERS and not in_translator:
                findings.append(Finding("devices", relative, _where(node), receiver))

        # Rule 5 -- async_get_device as a direct attribute call.
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "async_get_device"
            and not in_translator
        ):
            findings.append(
                Finding(
                    "async_get_device",
                    relative,
                    _where(node),
                    _receiver(node.value) or "<expression>",
                )
            )

    return findings, undecidable


def scan_production_tree() -> tuple[list[Finding], list[Finding]]:
    """Scan every production module and return ``(findings, undecidable)``."""
    findings: list[Finding] = []
    undecidable: list[Finding] = []
    for path in sorted(_PRODUCTION_ROOT.rglob("*.py")):
        relative = path.relative_to(_PRODUCTION_ROOT).as_posix()
        file_findings, file_undecidable = scan_file(path, relative)
        findings.extend(file_findings)
        undecidable.extend(file_undecidable)
    return findings, undecidable


#: Known limitation: the key groups by function, so swapping one violation for
#: another inside an already-listed function keeps the count and passes both
#: tests.  Accepted deliberately -- the alternative is line numbers, which churn
#: on every unrelated edit and would make the table unmaintainable.  The
#: substitution case is covered by review, not by this file.
#:
#: The ratchet.  Key is ``(rule, file, enclosing function)`` -- no line numbers,
#: so unrelated edits do not churn it -- and the value is how many occurrences
#: that site held.  The count is what made it a ratchet rather than a
#: whitelist: without it a *new* violation could hide inside a function that is
#: already listed.  Every migration work package shrank this table; nothing may
#: ever add to it.  It is empty since ``N-22``.
#:
#: Measured at the starting state (commit 77d9eb97): 88 occurrences at 48 sites;
#: after AP-11 (the compatibility shim stopped naming the old keyword): 85 at 46;
#: after AP-14 (identity.py moved to the shared resolver): 84 at 45; after
#: AP-12 (coordinator/registry.py speaks intents): 47 at 34; after AP-13
#: (services.py speaks intents and asks per entry): 35 at 25; after AP-15
#: (config_flow.py speaks intents): 25 at 23; after AP-16 (__init__.py speaks
#: intents, asks per entry and iterates through the shared helper): 4 at 4; after
#: AP-17 (diagnostics.py asks the registry for one entry's devices, which drops
#: the whole-mapping read and the set-shaped ownership read in one line): 2 at 2;
#: after N-22 (the last two ``devices`` reads ask the shared iterator and the
#: per-entry helper instead): **0 at 0**, which is the point of the ratchet.
#:
#: Measure the table yourself rather than trusting the chronicle above::
#:
#:     python3 -c "import ast,pathlib; t=ast.parse(pathlib.Path(
#:     'tests/test_guard_device_registry_kwargs.py').read_text());
#:     d=[ast.literal_eval(n.value) for n in ast.walk(t) if isinstance(
#:     n, ast.AnnAssign) and getattr(n.target,'id','')=='KNOWN_VIOLATIONS'][0];
#:     print(len(d), sum(d.values()))"
#:
#: An empty table is not the end of the gate, it is its sharpest state: from
#: here every finding is a new site, and ``test_no_new_deprecated_registry_usage``
#: reports it without anyone having to lower a number first.
KNOWN_VIOLATIONS: dict[tuple[str, str, str], int] = {}


def _current() -> dict[tuple[str, str, str], int]:
    """Group today's findings by site."""
    findings, _ = scan_production_tree()
    counted: Counter[tuple[str, str, str]] = Counter(item.site for item in findings)
    return dict(counted)


def test_no_new_deprecated_registry_usage() -> None:
    """No site may appear that is not in the ratchet, and none may grow."""
    current = _current()

    added = sorted(site for site in current if site not in KNOWN_VIOLATIONS)
    grown = sorted(
        (site, KNOWN_VIOLATIONS[site], count)
        for site, count in current.items()
        if site in KNOWN_VIOLATIONS and count > KNOWN_VIOLATIONS[site]
    )

    problems: list[str] = []
    for rule, path, function in added:
        problems.append(f"  new site: [{rule}] {path} in {function}")
    for (rule, path, function), was, now in grown:
        problems.append(f"  grew from {was} to {now}: [{rule}] {path} in {function}")

    assert not problems, (
        "The device registry ratchet caught new pre-2026.8 usage.\n"
        + "\n".join(problems)
        + "\n\nA device belongs to exactly one config entry and subentry since "
        "Home Assistant 2026.8. Express the intent (MOVE / ENSURE / DETACH) and "
        "let coordinator/helpers/registry.py translate it; see AGENTS.md, the "
        "bullet starting 'Registry updates **must not** pass'."
    )


def test_ratchet_has_no_stale_entries() -> None:
    """Every ratchet entry must still match, with exactly its recorded count.

    A shrinking count means a work package landed and the table was not updated;
    that is a required edit, not a failure of the code.  Leaving stale entries in
    place would let the ratchet drift into a list of historical curiosities that
    no longer describes the tree.
    """
    current = _current()

    stale = sorted(site for site in KNOWN_VIOLATIONS if site not in current)
    shrunk = sorted(
        (site, KNOWN_VIOLATIONS[site], current[site])
        for site in KNOWN_VIOLATIONS
        if site in current and current[site] < KNOWN_VIOLATIONS[site]
    )

    problems = [
        f"  gone, remove the entry: [{rule}] {path} in {function}"
        for rule, path, function in stale
    ]
    problems += [
        f"  now {now} instead of {was}, lower the count: [{rule}] {path} in {function}"
        for (rule, path, function), was, now in shrunk
    ]

    assert not problems, "KNOWN_VIOLATIONS no longer describes the tree:\n" + "\n".join(
        problems
    )


def test_undecidable_constructs_are_reported_not_hidden() -> None:
    """Doubtful constructs pass, but their number is stated.

    The gate lets a construct through when it cannot classify the receiver, so a
    false alarm can never be the reason someone disables it.  The count is
    printed and pinned here, so a sudden rise is visible instead of silent.
    """
    _, undecidable = scan_production_tree()
    for item in undecidable:
        print(
            f"undecidable: [{item.rule}] {item.path} in {item.function}: {item.detail}"
        )
    assert len(undecidable) <= 5, (
        f"{len(undecidable)} undecidable constructs, up from 0 at the time the "
        "gate landed. Review them and either extend the allow lists with a "
        "verified receiver or migrate the sites."
    )


#: Contracts that must NAME the replacement keyword, in nearest-wins order. A
#: rule that lives only in the root file is invisible to someone working from
#: ``tests/``, and the component file is the nearest contract for the module
#: this gate protects.
_KEYWORD_FILES = (
    "AGENTS.md",
    "custom_components/googlefindmy/AGENTS.md",
    "tests/AGENTS.md",
)

#: Contracts that must not CARRY the superseded advice. This is the wider set on
#: purpose: naming the replacement is a duty of the three files above, but
#: restoring the old rule is forbidden everywhere, and the topical files under
#: ``custom_components/googlefindmy/agents/`` are what an agent opens first for
#: registry work.
_NO_LEGACY_ADVICE_GLOBS = ("AGENTS.md", "**/AGENTS.md")

#: The legacy ownership keywords an instruction could tell someone to pass.
_LEGACY_ADVICE_TARGETS = r"add_config_(?:sub)?entry_id"

#: Prescriptive forms measured in this repository's own prose. The first two are
#: verbatim from the lines the migration removed (``git log -p``); the others are
#: the paraphrases the contracts use elsewhere for the same kind of duty, which
#: is how a model with pre-2026.8 training data restores the rule without
#: reusing the removed wording.
_SUPERSEDED_WORDINGS = (
    rf"always include (?:an |the |both )?{_LEGACY_ADVICE_TARGETS}",
    r"must include the child entry_id",
    rf"must (?:carry|pass|include|supply|send) (?:an |the |both )?{_LEGACY_ADVICE_TARGETS}",
    rf"(?:always|be sure to|make sure to|ensure you) (?:pass|supply|send|set|add) (?:an |the |both )?{_LEGACY_ADVICE_TARGETS}",
)

#: Words that turn a sentence about the old keywords into a prohibition or a
#: history note. Without this the contracts would report themselves once a
#: pattern above is widened: they discuss the superseded rule at length in order
#: to forbid it. Measured 2026-09-09 the filter rescues nothing in the tree, the
#: four patterns simply do not reach any prohibition sentence today. It is kept
#: so the pattern list can grow without the contracts turning red, and the
#: negative samples in the positive control keep it under test.
_PROHIBITION_MARKERS = re.compile(
    r"\b(?:never|no longer|not |n't|superseded|deprecat|removed|stop |instead|"
    r"forbidden|must not|do not|don't|deletes|inert|attaches nothing|"
    r"this bullet demanded|used to)\b",
    re.IGNORECASE,
)

#: How many consecutive lines are joined before matching. Markdown prose here is
#: hard-wrapped, so a sentence routinely spans two lines; three covers that with
#: margin. A window rather than a paragraph keeps the reported line number the
#: line the match starts on, which a paragraph cannot: the largest block in
#: these files is over 31000 characters.
_WINDOW = 3


def _windows(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, normalised text)`` for each sliding window.

    Backticks and bold markers are dropped, whitespace is collapsed. The
    underscore stays: it is part of every keyword this file looks for, and
    stripping it turned ``add_config_entry_id`` into ``addconfigentryid``, which
    no pattern matches. The positive control below caught that.
    """
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    for index in range(len(lines)):
        joined = " ".join(lines[index : index + _WINDOW])
        collapsed = re.sub(r"[`*]", "", joined)
        out.append((index + 1, re.sub(r"\s+", " ", collapsed).strip()))
    return out


def _legacy_advice_in(text: str) -> list[tuple[int, str]]:
    """Return every window that prescribes a superseded ownership keyword."""
    return [
        (line, window)
        for line, window in _windows(text)
        if any(re.search(p, window, re.IGNORECASE) for p in _SUPERSEDED_WORDINGS)
        and not _PROHIBITION_MARKERS.search(window)
    ]


def test_the_contracts_name_the_single_owner_keyword() -> None:
    """The three nearest contracts name the replacement API."""
    missing = [
        name
        for name in _KEYWORD_FILES
        if "new_config_subentry_id"
        not in (_REPO_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not missing, (
        "the single-owner keyword is unnamed in: "
        + ", ".join(missing)
        + ". An agent reading only that file keeps writing the legacy quadruple."
    )


def test_no_contract_carries_the_superseded_advice() -> None:
    """No ``AGENTS.md`` in the tree tells anyone to pass the old keywords.

    Blind spot, stated rather than implied: this holds four measured wordings
    and their close variants, not the meaning. A sufficiently different
    paraphrase passes, and the prohibition filter can be defeated by a sentence
    that both forbids and prescribes. It is a tripwire against the restoration
    of a known sentence, not a semantic check.

    Obsolete when the compatibility layer for cores below 2026.8 goes, that is
    once the declared minimum reaches 2026.8.0 or the ``2027.8.0`` removal
    lands: from then on the legacy keywords no longer parse and no contract can
    usefully mention them.
    """
    contracts = sorted(
        {
            path
            for pattern in _NO_LEGACY_ADVICE_GLOBS
            for path in _REPO_ROOT.glob(pattern)
            if ".git" not in path.parts
        }
    )
    assert len(contracts) >= len(_KEYWORD_FILES), (
        f"only {len(contracts)} contract files found; the globs "
        f"{_NO_LEGACY_ADVICE_GLOBS} no longer reach the tree."
    )

    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{line}: {window[:120]}"
        for path in contracts
        for line, window in _legacy_advice_in(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, "superseded ownership advice is back:\n" + "\n".join(
        offenders
    )


def test_the_superseded_wordings_still_match_what_they_describe() -> None:
    """Positive control: an empty result must mean absence, not a dead pattern.

    The samples carry the forms the check has to survive: a line break inside
    the sentence, single and double backticks, an article before the keyword,
    and this repository's own paraphrase of the same duty. Each of those made an
    earlier version of these patterns miss. The negative samples guard the other
    side, because a filter that reports the prohibitions themselves gets
    switched off after the second false alarm.
    """
    positives = (
        "always include `add_config_entry_id` (or `config_entry_id` on legacy cores)",
        "Registry updates **must** include the child `entry_id` in every call",
        "so Home Assistant keeps it: always include\n``add_config_entry_id`` today",
        "always include an `add_config_entry_id` whenever a subentry is present",
        "Every `async_update_device` call on a tracker device must carry\n"
        "`add_config_entry_id`",
        "be sure to pass `add_config_subentry_id` when a subentry exists",
    )
    unmatched = [s for s in positives if not _legacy_advice_in(s)]
    assert not unmatched, (
        "these superseded wordings are no longer recognised: "
        + "; ".join(repr(s) for s in unmatched)
    )

    # Each of these matches one of the patterns above and is saved only by the
    # prohibition filter. Samples that miss the patterns anyway would test
    # nothing here: measured, they leave the filter green when it is deleted.
    negatives = (
        "Superseded, do not restore: always include `add_config_entry_id`.",
        "Older guidance said every call must carry `add_config_entry_id`; it no "
        "longer applies.",
        "A call must not include `add_config_entry_id` on Core 2026.8+.",
    )
    false_alarms = [s for s in negatives if _legacy_advice_in(s)]
    assert not false_alarms, (
        "the prohibition filter let these through as advice: "
        + "; ".join(repr(s) for s in false_alarms)
    )
