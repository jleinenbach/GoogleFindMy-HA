#!/usr/bin/env python3
# script/translation_placeholder_check.py
"""Lint Repairs issue translation placeholders for code/translation symmetry.

The script statically walks every ``custom_components/googlefindmy/**/*.py``
file, finds calls of the form ``*.async_create_issue(...)`` and extracts:

* ``translation_key`` (literal string or a constant defined in ``const.py``)
* ``translation_placeholders`` keys (literal dict, or an intra-procedural
  dict variable built via ``var = {...}`` + ``var["key"] = value`` updates).

It then compares each translation key against the corresponding
``issues.<key>.title`` / ``issues.<key>.description`` placeholders found
in ``strings.json`` (the SSOT for translations).

In addition, every ``custom_components/googlefindmy/translations/*.json``
locale file is validated against the same SSOT: each locale's
``issues.<key>.title`` / ``issues.<key>.description`` may render a
*subset* of the placeholders declared in ``strings.json`` for that key
(translators are free to drop placeholders if the translated wording does
not need them), but it must not introduce any *additional* placeholder
that the call site cannot supply (which would surface in HA Repairs as a
raw ``{placeholder}`` to non-English users; Codex feedback on PR #1069).

Finding classes:

* ``MISSING`` (fatal): translation references a placeholder that the code
  does not supply (the literal ``{name}`` ends up visible in the UI).
* ``MISSING_TRANSLATION`` (fatal): code references a translation key that
  has no entry in ``strings.json``.
* ``DRIFT_RISK`` (fatal): ``translation_placeholders=<variable>`` where the
  variable's literal-string keys cannot be traced statically. This means
  the linter cannot detect placeholder drift for that call site, so it
  must fail the gate (else the symmetry check silently passes for any
  call using variable-built dicts; Codex feedback on PR #1069).
* ``LOCALE_EXTRA`` (fatal): a ``translations/*.json`` locale file
  references a placeholder for an issue key that does not exist in the
  same key's ``strings.json`` template, so the call site cannot supply
  it and HA Repairs would render the raw ``{placeholder}`` to users of
  that locale (Codex feedback on PR #1069).
* ``DEAD`` (warning): code supplies a placeholder that no translation
  string renders. This is informational only — HA Repairs forwards the
  full ``translation_placeholders`` dict to the issue payload, so extra
  keys beyond what the description renders are legitimate machine-readable
  diagnostic metadata (for example the ``cause`` placeholder on
  ``duplicate_account_entries``).
* ``SKIPPED`` (warning): a translation key or non-variable placeholders
  argument could not be statically resolved (e.g. parse errors on a
  newer Python syntax, unsupported AST nodes).

``MISSING`` / ``MISSING_TRANSLATION`` / ``DRIFT_RISK`` / ``LOCALE_EXTRA``
fail the run. ``--strict`` also fails on warnings.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = ROOT / "custom_components" / "googlefindmy"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class Finding:
    """A single linter finding."""

    kind: str
    file: Path
    line: int
    translation_key: str
    detail: str

    def format_for_github(self) -> str:
        """Render this finding as a GitHub Actions annotation line.

        Only fatal kinds (``MISSING`` / ``MISSING_TRANSLATION`` /
        ``DRIFT_RISK`` / ``LOCALE_EXTRA``) reach this method via ``main()``
        when ``--github`` is active. Non-fatal ``SKIPPED`` / ``DEAD``
        findings are rendered as plain stdout lines instead, because
        Actions annotations suggest required action and would obscure the
        non-blocking informational nature of diagnostic-only placeholders
        (continuation of PR #1069 feedback: avoid noise that looks like a
        failure on green runs).
        """
        level = "warning" if self.kind in ("SKIPPED", "DEAD") else "error"
        rel = self.file.relative_to(ROOT)
        return (
            f"::{level} file={rel},line={self.line},"
            f"title=translation_placeholder_check {self.kind}::"
            f"{self.translation_key}: {self.detail}"
        )


@dataclass
class FileScan:
    """Result of scanning a single Python file."""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[Finding] = field(default_factory=list)


def _load_const_strings(const_path: Path) -> dict[str, str]:
    """Build a name -> str-literal map from ``const.py`` module-level assigns."""
    if not const_path.exists():
        return {}
    try:
        tree = ast.parse(const_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in tree.body:
        # Match plain `NAME = "literal"` and `NAME: str = "literal"`
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targets = [node.targets[0]]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if not targets or value is None:
            continue
        if (
            isinstance(targets[0], ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            out[targets[0].id] = value.value
    return out


def _extract_issue_placeholders(data: object) -> dict[str, set[str]]:
    """Return mapping ``issue_key -> placeholders`` from one parsed JSON tree.

    Reads ``issues.<key>.title`` and ``issues.<key>.description``. Used by
    both ``_load_translations`` (for ``strings.json``) and
    ``_load_locale_placeholders`` (for each ``translations/*.json``) so the
    extraction stays in sync between SSOT and locales.
    """
    issues = (
        data.get("issues") if isinstance(data, dict) else None
    ) or {}
    out: dict[str, set[str]] = {}
    if not isinstance(issues, dict):
        return out
    for key, payload in issues.items():
        text_parts: list[str] = []
        for field_name in ("title", "description"):
            value = payload.get(field_name) if isinstance(payload, dict) else None
            if isinstance(value, str):
                text_parts.append(value)
        placeholders: set[str] = set()
        for text in text_parts:
            placeholders.update(PLACEHOLDER_RE.findall(text))
        out[key] = placeholders
    return out


def _load_translations(strings_path: Path) -> dict[str, set[str]]:
    """Return mapping ``issue_key -> placeholders`` from ``strings.json``."""
    data = json.loads(strings_path.read_text(encoding="utf-8"))
    return _extract_issue_placeholders(data)


def _load_locale_placeholders(
    translations_dir: Path,
) -> dict[Path, dict[str, set[str]]]:
    """Return ``{locale_file: {issue_key: placeholders}}`` for all locales.

    Each ``custom_components/googlefindmy/translations/*.json`` is parsed
    and reduced to ``issues.<key>.title`` / ``issues.<key>.description``
    placeholders. Used to detect ``LOCALE_EXTRA`` drift where a translator
    introduces a placeholder that does not exist in the SSOT for the same
    issue key (Codex feedback on PR #1069).

    Returns an empty mapping if ``translations_dir`` is absent so callers
    can degrade gracefully in stripped checkouts.
    """
    if not translations_dir.is_dir():
        return {}
    out: dict[Path, dict[str, set[str]]] = {}
    for locale_file in sorted(translations_dir.glob("*.json")):
        try:
            data = json.loads(locale_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A malformed locale file is reported by sync_translations.py /
            # translation_key_check.py; we silently skip it here to avoid
            # double-reporting and keep this checker focused on placeholders.
            continue
        out[locale_file] = _extract_issue_placeholders(data)
    return out


def _resolve_key(
    node: ast.expr,
    const_map: dict[str, str],
    local_consts: dict[str, str],
) -> str | None:
    """Resolve an ``ast.expr`` to a string literal if possible."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in local_consts:
            return local_consts[node.id]
        if node.id in const_map:
            return const_map[node.id]
    if isinstance(node, ast.Attribute) and node.attr in const_map:
        # e.g. ``const.TRANSLATION_KEY_FOO`` — only handle by attr name.
        return const_map[node.attr]
    return None


def _collect_module_local_consts(tree: ast.Module) -> dict[str, str]:
    """Collect top-level ``NAME = "literal"`` assignments within the file."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                out[target.id] = value.value
    return out


def _placeholders_from_dict(node: ast.Dict) -> set[str] | None:
    """Return the constant-string keys of a literal dict, or None if dynamic."""
    keys: set[str] = set()
    for key in node.keys:
        if key is None:  # ``**other`` unpack
            return None
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        else:
            return None
    return keys


# Sentinel returned by the per-statement helpers when an assignment is
# dynamic and the caller must give up on static resolution (return ``None``).
# Distinguishing "drift" from "statement is irrelevant" (``None`` return)
# lets the dispatcher stay flat and keeps the branch count well below the
# Ruff PLR0912 threshold.
_DRIFT_SENTINEL: object = object()


def _apply_assign_to_keys(
    stmt: ast.Assign, name: str, keys: set[str] | None
) -> set[str] | object | None:
    """Handle ``name = {...}`` or ``name["k"] = v`` for ``_trace_local_dict_keys``.

    Returns a fresh ``set`` with the new keys, ``_DRIFT_SENTINEL`` if the
    assignment is dynamic, or ``None`` if the statement is irrelevant
    (e.g. assigns to a different variable).
    """
    target = stmt.targets[0]
    if isinstance(target, ast.Name) and target.id == name:
        if not isinstance(stmt.value, ast.Dict):
            return _DRIFT_SENTINEL
        dict_keys = _placeholders_from_dict(stmt.value)
        if dict_keys is None:
            return _DRIFT_SENTINEL
        return set(dict_keys)
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == name
    ):
        slice_node = target.slice
        if not (
            isinstance(slice_node, ast.Constant)
            and isinstance(slice_node.value, str)
        ):
            return _DRIFT_SENTINEL
        new_keys = set(keys) if keys is not None else set()
        new_keys.add(slice_node.value)
        return new_keys
    return None


def _apply_ann_assign_to_keys(
    stmt: ast.AnnAssign, name: str
) -> set[str] | object | None:
    """Handle ``name: T = {...}`` for ``_trace_local_dict_keys``.

    Returns a fresh ``set`` with the new keys, ``_DRIFT_SENTINEL`` if the
    annotated assignment is dynamic, or ``None`` if the statement targets
    a different name or is a bare declaration (``name: T``).
    """
    if stmt.target.id != name or stmt.value is None:
        return None
    if not isinstance(stmt.value, ast.Dict):
        return _DRIFT_SENTINEL
    dict_keys = _placeholders_from_dict(stmt.value)
    if dict_keys is None:
        return _DRIFT_SENTINEL
    return set(dict_keys)


class _ScopedDictAssignmentCollector(ast.NodeVisitor):
    """Collect ``Assign``/``AnnAssign`` statements visible at a call site.

    "Visible" means: in the enclosing function's scope (not in a nested
    function/class/lambda) AND on a line strictly before ``call_line``.
    The empty ``visit_FunctionDef`` / ``visit_AsyncFunctionDef`` /
    ``visit_ClassDef`` / ``visit_Lambda`` methods stop descent into nested
    scopes (without those overrides, the default ``generic_visit`` would
    recurse and count assignments in unrelated nested functions — Codex
    feedback on PR #1069).
    """

    def __init__(self, call_line: int) -> None:
        self.call_line = call_line
        self.assignments: list[ast.Assign | ast.AnnAssign] = []

    # Stop descent into nested scopes — they cannot rebind names in the
    # outer scope that the Repairs call uses.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: D401
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: D401
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: D401
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: D401
        return

    def visit_Assign(self, node: ast.Assign) -> None:
        if node.lineno < self.call_line:
            self.assignments.append(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.lineno < self.call_line:
            self.assignments.append(node)
        self.generic_visit(node)


def _trace_local_dict_keys(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
    call_node: ast.Call,
) -> set[str] | None:
    """Trace the literal-string keys of ``name`` visible at ``call_node``.

    Supports plain (``name = {...}``) and annotated
    (``name: dict[str, Any] = {...}``) initialisation as well as
    ``name["k"] = v`` updates. Only considers assignments that

    * execute strictly before ``call_node`` (order-aware) and
    * live in the enclosing function's scope, not in nested
      function/class/lambda bodies (scope-aware).

    Returns ``None`` when any qualifying assignment is dynamic
    (``**rest``, non-string subscript, ``name = func()``,
    annotated assignment without value). Statements on the call's own
    line are excluded (rare multi-statement-per-line edge case).
    """
    collector = _ScopedDictAssignmentCollector(call_node.lineno)
    for stmt in func_node.body:
        collector.visit(stmt)
    keys: set[str] | None = None
    for stmt in collector.assignments:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            result = _apply_assign_to_keys(stmt, name, keys)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            result = _apply_ann_assign_to_keys(stmt, name)
        else:
            continue
        if result is _DRIFT_SENTINEL:
            return None
        if result is not None:
            keys = result  # type: ignore[assignment]
    return keys


def _enclosing_function(
    tree: ast.Module, call_node: ast.Call
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the nearest enclosing function for ``call_node``."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    current: ast.AST | None = call_node
    while current is not None:
        parent = parents.get(id(current))
        if parent is None:
            return None
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent
        current = parent


def _resolve_dict_literal(node: ast.Dict) -> tuple[set[str] | None, str | None]:
    """Branch of ``_resolve_placeholders`` handling literal-dict arguments."""
    keys = _placeholders_from_dict(node)
    if keys is None:
        return None, "dynamic dict literal (**unpack or non-string key)"
    return keys, None


def _resolve_name_reference(
    node: ast.Name,
    tree: ast.Module,
    call_node: ast.Call,
) -> tuple[set[str] | None, str | None]:
    """Branch of ``_resolve_placeholders`` handling variable references."""
    func = _enclosing_function(tree, call_node)
    if func is None:
        return None, f"variable '{node.id}' used at module scope"
    keys = _trace_local_dict_keys(func, node.id, call_node)
    if keys is None:
        return None, f"variable '{node.id}' could not be statically resolved"
    return keys, None


def _resolve_placeholders(
    node: ast.expr | None,
    tree: ast.Module,
    call_node: ast.Call,
) -> tuple[set[str] | None, str | None]:
    """Resolve ``translation_placeholders`` keyword to a key set.

    Returns ``(keys, skip_reason)``. ``keys`` is None if dynamic and the
    caller should treat it as ``SKIPPED``.
    """
    if node is None:
        return set(), None
    if isinstance(node, ast.Dict):
        return _resolve_dict_literal(node)
    if isinstance(node, ast.Name):
        return _resolve_name_reference(node, tree, call_node)
    return None, f"unsupported placeholders expression: {type(node).__name__}"


def _scan_file(
    path: Path, const_map: dict[str, str], translations: dict[str, set[str]]
) -> FileScan:
    """Scan a single Python file."""
    scan = FileScan()
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        scan.skipped.append(
            Finding(
                kind="SKIPPED",
                file=path,
                line=err.lineno or 1,
                translation_key="<parse-error>",
                detail=(
                    f"ast.parse failed under Python {sys.version_info.major}."
                    f"{sys.version_info.minor}: {err.msg}"
                ),
            )
        )
        return scan
    local_consts = _collect_module_local_consts(tree)
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "async_create_issue"):
            continue
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        tk_node = kwargs.get("translation_key")
        if tk_node is None:
            scan.skipped.append(
                Finding(
                    kind="SKIPPED",
                    file=path,
                    line=call.lineno,
                    translation_key="<unknown>",
                    detail="translation_key missing from async_create_issue call",
                )
            )
            continue
        key = _resolve_key(tk_node, const_map, local_consts)
        if key is None:
            scan.skipped.append(
                Finding(
                    kind="SKIPPED",
                    file=path,
                    line=call.lineno,
                    translation_key="<unresolved>",
                    detail="translation_key could not be statically resolved",
                )
            )
            continue
        code_keys, skip_reason = _resolve_placeholders(
            kwargs.get("translation_placeholders"), tree, call
        )
        if code_keys is None:
            # A failed variable resolution means the linter cannot detect
            # placeholder drift for this call — escalate to DRIFT_RISK
            # (fatal). Other dynamic constructs (parse errors, unsupported
            # AST nodes) stay informational SKIPPED.
            if skip_reason and skip_reason.startswith("variable "):
                scan.findings.append(
                    Finding(
                        kind="DRIFT_RISK",
                        file=path,
                        line=call.lineno,
                        translation_key=key,
                        detail=skip_reason,
                    )
                )
            else:
                scan.skipped.append(
                    Finding(
                        kind="SKIPPED",
                        file=path,
                        line=call.lineno,
                        translation_key=key,
                        detail=skip_reason or "translation_placeholders dynamic",
                    )
                )
            continue
        translation_keys = translations.get(key)
        if translation_keys is None:
            scan.findings.append(
                Finding(
                    kind="MISSING_TRANSLATION",
                    file=path,
                    line=call.lineno,
                    translation_key=key,
                    detail=f"no entry issues.{key} in strings.json",
                )
            )
            continue
        dead = sorted(code_keys - translation_keys)
        missing = sorted(translation_keys - code_keys)
        if dead:
            # DEAD placeholders are informational only. HA Repairs forwards the
            # full ``translation_placeholders`` dict to the issue payload, so
            # extra keys beyond what the description renders are legitimately
            # used as machine-readable diagnostic metadata (e.g. ``cause``).
            scan.skipped.append(
                Finding(
                    kind="DEAD",
                    file=path,
                    line=call.lineno,
                    translation_key=key,
                    detail=f"placeholders unused by translation: {', '.join(dead)}",
                )
            )
        if missing:
            scan.findings.append(
                Finding(
                    kind="MISSING",
                    file=path,
                    line=call.lineno,
                    translation_key=key,
                    detail=f"placeholders referenced by translation but not supplied: {', '.join(missing)}",
                )
            )
    return scan


def _iter_python_files(component_dir: Path) -> Iterable[Path]:
    for path in sorted(component_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on SKIPPED entries (default: warn only).",
    )
    parser.add_argument(
        "--github",
        action="store_true",
        help="Emit GitHub Actions workflow annotations on stdout.",
    )
    args = parser.parse_args()

    const_map = _load_const_strings(COMPONENT_DIR / "const.py")
    translations = _load_translations(STRINGS_PATH)

    total = FileScan()
    for path in _iter_python_files(COMPONENT_DIR):
        scan = _scan_file(path, const_map, translations)
        total.findings.extend(scan.findings)
        total.skipped.extend(scan.skipped)

    # LOCALE_EXTRA sweep: every translations/*.json must not introduce
    # placeholders absent from strings.json for the same issue key — otherwise
    # HA Repairs would surface raw ``{placeholder}`` text to non-English users
    # because the call site cannot supply that name (Codex feedback on PR #1069).
    for locale_file, locale_issues in _load_locale_placeholders(
        TRANSLATIONS_DIR
    ).items():
        for key, locale_ph in locale_issues.items():
            base_ph = translations.get(key, set())
            extra = locale_ph - base_ph
            if not extra:
                continue
            total.findings.append(
                Finding(
                    kind="LOCALE_EXTRA",
                    file=locale_file,
                    line=1,
                    translation_key=key,
                    detail=(
                        f"extraneous placeholder(s) {sorted(extra)!r}"
                        f" not declared in strings.json issues.{key}"
                    ),
                )
            )

    if not total.findings and not total.skipped:
        print("translation_placeholder_check: 0 findings, 0 skipped — OK")
        return 0

    for f in total.findings:
        line = (
            f.format_for_github()
            if args.github
            else (
                f"[{f.kind}] {f.file.relative_to(ROOT)}:{f.line}: "
                f"{f.translation_key}: {f.detail}"
            )
        )
        print(line)
    for f in total.skipped:
        # SKIPPED / DEAD are non-fatal informational findings. They are
        # rendered as plain stdout lines (not GitHub Actions annotations)
        # even when ``--github`` is active. Annotations imply user action
        # is required; SKIPPED / DEAD are diagnostic-only (the latter
        # documents intentional machine-readable placeholders like the
        # ``cause`` key on ``duplicate_account_entries``). They stay
        # visible in step logs for forensics but no longer surface as
        # warning annotations on the PR.
        print(
            f"[{f.kind}] {f.file.relative_to(ROOT)}:{f.line}: "
            f"{f.translation_key}: {f.detail}"
        )

    print(
        f"\ntranslation_placeholder_check: "
        f"{len(total.findings)} finding(s), {len(total.skipped)} skipped"
    )
    if total.findings:
        return 1
    if args.strict and total.skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
