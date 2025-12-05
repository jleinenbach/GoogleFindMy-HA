"""Static guardrail to keep the codebase Python 3.15-ready and 3.13-safe."""

from __future__ import annotations

import ast
import warnings
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REMOVED_MODULES = {
    "cgi",
    "cgitb",
    "audioop",
    "pipes",
    "chunk",
    "aifc",
    "sunau",
    "imghdr",
    "mailcap",
    "nntplib",
    "uu",
    "crypt",
    "nis",
}

MODULE_FIX_SUGGESTIONS = {
    "cgi": "Replace `cgi.escape` with `html.escape` or move off CGI entirely.",
    "cgitb": "Remove CGI traceback helpers; prefer modern ASGI/WSGI error logging.",
    "audioop": "Port audio handling to `numpy`, `scipy`, or vendor a replacement.",
    "pipes": "Use `subprocess` helpers instead of `pipes` pipelines.",
    "chunk": "Prefer the `wave` module or structured parsers for chunked audio data.",
    "aifc": "Switch to `wave` or a maintained audio library.",
    "sunau": "Switch to `wave` or a maintained audio library.",
    "imghdr": "Use explicit header checks or libraries like `filetype` for detection.",
    "mailcap": "Use `mimetypes` in place of `mailcap` lookups.",
    "nntplib": "Depend on an external NNTP client library instead of `nntplib`.",
    "uu": "Use `base64` helpers instead of uuencoding.",
    "crypt": "Migrate password hashing to `hashlib` or `passlib`.",
    "nis": "Remove legacy NIS lookups or depend on a maintained replacement.",
}

SKIP_PARTS = {
    ".git",
    "node_modules",
    "ProtoDecoders",
    "firebase_messaging",
    "dist",
    "build",
    "venv",
    ".venv",
    "__pycache__",
}


@dataclass
class Diagnostic:
    path: Path
    line: int
    message: str
    suggestion: str | None = None
    severity: str = "error"


class Py315CompatibilityVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, diagnostics: list[Diagnostic]) -> None:
        self.path = path
        self.diagnostics = diagnostics
        self.locale_modules: set[str] = set()
        self.threading_modules: set[str] = set()
        self.webbrowser_modules: set[str] = set()
        self.pathlib_modules: set[str] = set()
        self.ctypes_modules: set[str] = set()
        self.typing_modules: set[str] = set()
        self.locale_callables: set[str] = set()
        self.rlock_callables: set[str] = set()
        self.path_classes: set[str] = set()
        self.ctypes_callables: set[str] = set()
        self.namedtuple_callables: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            module = alias.name.split(".")[0]
            name = alias.asname or module
            if module in REMOVED_MODULES:
                self._add_removed_module_error(node, module)
            if module == "locale":
                self.locale_modules.add(name)
            if module == "threading":
                self.threading_modules.add(name)
            if module == "webbrowser":
                self.webbrowser_modules.add(name)
            if module == "pathlib":
                self.pathlib_modules.add(name)
            if module == "ctypes":
                self.ctypes_modules.add(name)
            if module == "typing":
                self.typing_modules.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = (node.module or "").split(".")[0]
        if module in REMOVED_MODULES:
            self._add_removed_module_error(node, module)
        if module == "locale":
            for alias in node.names:
                if alias.name == "getdefaultlocale":
                    self.locale_callables.add(alias.asname or alias.name)
        if module == "threading":
            for alias in node.names:
                if alias.name == "RLock":
                    self.rlock_callables.add(alias.asname or alias.name)
        if module == "ctypes":
            for alias in node.names:
                if alias.name == "SetPointerType":
                    self.ctypes_callables.add(alias.asname or alias.name)
        if module == "typing":
            for alias in node.names:
                if alias.name == "NamedTuple":
                    self.namedtuple_callables.add(alias.asname or alias.name)
        if module == "webbrowser":
            for alias in node.names:
                if alias.name == "MacOSX":
                    self._add_webbrowser_error(node, alias.asname or alias.name)
        if module == "pathlib":
            for alias in node.names:
                if alias.name in {"PurePath", "Path"}:
                    self.path_classes.add(alias.asname or alias.name)
        for alias in node.names:
            if alias.name == "*":
                self._add_wildcard_warning(node, module)
                if module == "threading":
                    self.rlock_callables.add("RLock")
                if module == "locale":
                    self.locale_callables.add("getdefaultlocale")
                if module == "ctypes":
                    self.ctypes_callables.add("SetPointerType")
                if module == "typing":
                    self.namedtuple_callables.add("NamedTuple")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute):
            if (
                func.attr == "getdefaultlocale"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.locale_modules
            ):
                self._add_locale_error(func)
            if (
                func.attr == "RLock"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.threading_modules
            ):
                self._add_rlock_error(node)
            if (
                func.attr == "SetPointerType"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.ctypes_modules
            ):
                self._add_ctypes_error(func)
            if func.attr == "is_reserved" and self._looks_like_path_instance(func.value):
                self._add_pathlib_error(func)
            if (
                func.attr == "NamedTuple"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.typing_modules
            ):
                self._add_namedtuple_error(node)
        elif isinstance(func, ast.Name):
            if func.id in self.locale_callables:
                self._add_locale_error(func)
            if func.id in self.rlock_callables:
                self._add_rlock_error(node)
            if func.id in self.ctypes_callables:
                self._add_ctypes_error(func)
            if func.id in self.namedtuple_callables:
                self._add_namedtuple_error(node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if (
            node.attr == "MacOSX"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.webbrowser_modules
        ):
            self._add_webbrowser_error(node)
        self.generic_visit(node)

    def _looks_like_path_instance(self, value: ast.AST) -> bool:
        if isinstance(value, ast.Name):
            return value.id in self.path_classes
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return value.value.id in self.pathlib_modules
        return False

    def _add_removed_module_error(self, node: ast.AST, module: str) -> None:
        suggestion = MODULE_FIX_SUGGESTIONS.get(module)
        message = f"`{module}` is removed in Python 3.15 and must not be imported."
        self.diagnostics.append(
            Diagnostic(self.path, getattr(node, "lineno", 1), message, suggestion)
        )

    def _add_locale_error(self, node: ast.AST) -> None:
        self.diagnostics.append(
            Diagnostic(
                self.path,
                getattr(node, "lineno", 1),
                "`locale.getdefaultlocale()` was removed in Python 3.15.",
                "Use `locale.getencoding()` for the encoding and `locale.getlocale()` "
                "(after `locale.setlocale(locale.LC_CTYPE, \"\")`) for the language.",
            )
        )

    def _add_rlock_error(self, node: ast.Call) -> None:
        if node.args or node.keywords:
            self.diagnostics.append(
                Diagnostic(
                    self.path,
                    getattr(node, "lineno", 1),
                    "`threading.RLock` rejects all arguments in Python 3.15.",
                    "Drop positional and keyword arguments: `threading.RLock()`.",
                )
            )

    def _add_pathlib_error(self, node: ast.AST) -> None:
        self.diagnostics.append(
            Diagnostic(
                self.path,
                getattr(node, "lineno", 1),
                "`PurePath.is_reserved()` was removed in Python 3.15.",
                "Call `os.path.isreserved(path_obj)` instead.",
            )
        )

    def _add_webbrowser_error(self, node: ast.AST, alias: str | None = None) -> None:
        message = "`webbrowser.MacOSX` was removed in Python 3.15."
        suggestion = "Use `webbrowser.get('macosx')` when targeting macOS browsers."
        self.diagnostics.append(
            Diagnostic(
                self.path,
                getattr(node, "lineno", 1),
                message,
                f"Replace `{alias or 'webbrowser.MacOSX'}` with {suggestion}",
            )
        )

    def _add_ctypes_error(self, node: ast.AST) -> None:
        self.diagnostics.append(
            Diagnostic(
                self.path,
                getattr(node, "lineno", 1),
                "`ctypes.SetPointerType` was removed in Python 3.15.",
                "Vendor a replacement or refactor pointer handling without `SetPointerType`.",
            )
        )

    def _add_namedtuple_error(self, node: ast.Call) -> None:
        if not node.keywords:
            return
        self.diagnostics.append(
            Diagnostic(
                self.path,
                getattr(node, "lineno", 1),
                "`typing.NamedTuple` keyword arguments are banned in Python 3.15.",
                "Use class-based NamedTuple syntax or a list of field tuples.",
            )
        )

    def _add_wildcard_warning(self, node: ast.AST, module: str) -> None:
        self.diagnostics.append(
            Diagnostic(
                self.path,
                getattr(node, "lineno", 1),
                f"Wildcard import from `{module}` obscures Python 3.15 compatibility analysis.",
                "Replace the `*` import with explicit names to expose incompatible symbols.",
                severity="warning",
            )
        )


def iter_python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        yield path


def format_diagnostics(diagnostics: Iterable[Diagnostic]) -> str:
    lines = [
        "Python 3.15 compatibility guard found incompatibilities.",
        "Each item includes a migration hint to keep the codebase runnable on Python 3.13 and 3.15:",
    ]
    for diag in diagnostics:
        location = f"{diag.path}:{diag.line}"
        prefix = "[warning]" if diag.severity == "warning" else "[error]"
        lines.append(f"- {prefix} {location} — {diag.message}")
        if diag.suggestion:
            lines.append(f"  Suggested fix: {diag.suggestion}")
    return "\n".join(lines)


def test_python_315_compatibility_guard() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    diagnostics: list[Diagnostic] = []

    for path in iter_python_files(repo_root):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive guard
            diagnostics.append(
                Diagnostic(
                    path,
                    exc.lineno or 1,
                    "The current interpreter (Python 3.13 compatibility check) could not parse this file. "
                    "This often means 3.14+ syntax such as template strings (t\"...\") or other grammar changes.",
                    "Use 3.13-compatible syntax (for example, f-strings instead of t-strings) "
                    "so the integration stays runnable across supported versions.",
                )
            )
            continue

        Py315CompatibilityVisitor(path, diagnostics).visit(tree)

    errors = [diag for diag in diagnostics if diag.severity == "error"]
    warnings_only = [diag for diag in diagnostics if diag.severity == "warning"]

    for warning in warnings_only:
        warnings.warn(format_diagnostics([warning]))

    if errors:
        pytest.fail(format_diagnostics(errors))
