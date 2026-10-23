# tests/test_guard_update_failed_translation.py
"""Guard: every ``UpdateFailed`` of the integration is translatable.

Home Assistant copies ``translation_key`` of an ``UpdateFailed`` into the
setup-retry reason of the integration card when the first refresh fails, and
the frontend shows that reason in the user's language. A raise without a key
falls back to the English text again. The checks run on the syntax tree of
every module under ``custom_components/googlefindmy``:

(a) coverage: each ``UpdateFailed(...)`` call passes a message text, and
    ``translation_domain`` as the name ``DOMAIN`` and a ``translation_key``
    (without a text, ``str()`` is built from the key, which is the bare key
    while no translations are cached). No module may hide a raise from this
    scan: an import under an alias, a subclass, ``getattr(…, "UpdateFailed")``
    or any use of the class as a value (``UF = UpdateFailed``,
    ``functools.partial(UpdateFailed)``, a bare ``raise UpdateFailed``) fails.
    Allowed positions are the called function, ``except`` clauses,
    ``isinstance``/``issubclass`` and annotations. At least ``_MIN_CALLS``
    calls must be found, so a wrong path cannot turn the guard green by
    finding nothing;
(b) parity: the key is a string literal, ``strings.json`` and every file in
    ``translations/`` define ``exceptions.<key>.message``, the template in
    ``strings.json`` uses exactly the placeholders the call passes, and no
    locale uses one the call does not pass;
(c) privacy: placeholder names come from ``_ALLOWED_PLACEHOLDERS``, and no
    placeholder value reads an identifier that contains ``name``, ``title``
    or ``email`` (dunders such as ``__name__`` excepted). A device name is
    derived information under ``AGENTS.md`` section 5 (b) and would land in
    the integration card and the log; devices are named by ID.

A ``**`` mapping at a call fails closed only when it carries the key itself
(it is then reported as a missing key); next to a literal key it is not seen.
Not seen either: ``getattr`` with a computed attribute name, a lookup through
``__dict__`` or ``vars()``, ``type(err)(...)`` and ``err.__class__(...)``, an
assignment expression inside an annotation, code built at run time
(``eval``, ``exec``), a starred or empty message text, a placeholder value read
by subscript or ``.get("name")``, and one under an identifier without those
three words. Each arm has a self-test on a synthetic source that must fail.
"""

from __future__ import annotations

import ast
import json
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
_MIN_CALLS = 15
_ALLOWED_PLACEHOLDERS = frozenset(
    {"device_id", "status", "error_type", "not_accepted", "rejected"}
)
_FORBIDDEN_VALUE_WORDS = ("name", "title", "email")
_CLASS_NAME = "UpdateFailed"


@dataclass
class _Call:
    where: str
    keywords: dict[str, ast.expr]
    has_text: bool


@dataclass
class _Scan:
    calls: list[_Call] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _allowed_positions(tree: ast.AST) -> set[int]:
    """Return the ids of nodes where naming the class hides no raise."""

    allowed: set[int] = set()

    def _with_tuple(node: ast.expr | None) -> None:
        if node is None:
            return
        allowed.add(id(node))
        if isinstance(node, ast.Tuple):
            allowed.update(id(elt) for elt in node.elts)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            allowed.add(id(node.func))
            if _name(node.func) in {"isinstance", "issubclass"} and len(node.args) == 2:
                _with_tuple(node.args[1])
        elif isinstance(node, ast.ExceptHandler):
            _with_tuple(node.type)
        elif isinstance(node, ast.ClassDef):
            allowed.update(id(base) for base in node.bases)
        annotations: list[ast.expr | None] = []
        if isinstance(node, ast.arg):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            if annotation is not None:
                allowed.update(id(sub) for sub in ast.walk(annotation))
    return allowed


def _scan(sources: dict[str, str]) -> _Scan:
    scan = _Scan()
    for path, source in sorted(sources.items()):
        tree = ast.parse(source)
        allowed = _allowed_positions(tree)
        for node in ast.walk(tree):
            where = f"{path}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom):
                scan.hidden += [
                    f"{where} imports UpdateFailed as {alias.asname}"
                    for alias in node.names
                    if alias.name == _CLASS_NAME and alias.asname
                ]
            elif isinstance(node, ast.ClassDef):
                if any(_name(base) == _CLASS_NAME for base in node.bases):
                    scan.hidden.append(f"{where} subclasses UpdateFailed")
            elif isinstance(node, ast.Call):
                if _name(node.func) == _CLASS_NAME:
                    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                    scan.calls.append(_Call(where, keywords, bool(node.args)))
                elif _name(node.func) == "getattr" and any(
                    isinstance(arg, ast.Constant) and arg.value == _CLASS_NAME
                    for arg in node.args
                ):
                    scan.hidden.append(f"{where} looks UpdateFailed up by name")
            elif (
                isinstance(node, (ast.Name, ast.Attribute))
                and _name(node) == _CLASS_NAME
                and isinstance(node.ctx, ast.Load)
                and id(node) not in allowed
            ):
                scan.hidden.append(f"{where} uses UpdateFailed as a value")
    return scan


def _package_sources() -> dict[str, str]:
    return {
        str(path.relative_to(_PACKAGE.parent)): path.read_text(encoding="utf-8")
        for path in sorted(_PACKAGE.rglob("*.py"))
    }


def _package_messages() -> list[tuple[str, dict[str, Any]]]:
    files = [
        _PACKAGE / "strings.json",
        *sorted((_PACKAGE / "translations").glob("*.json")),
    ]
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")).get("exceptions", {}))
        for path in files
    ]


def _coverage_problems(scan: _Scan) -> list[str]:
    problems = list(scan.hidden)
    for call in scan.calls:
        domain = call.keywords.get("translation_domain")
        if not call.has_text:
            problems.append(f"{call.where} has no message text")
        if "translation_key" not in call.keywords:
            problems.append(f"{call.where} has no translation_key")
        if not (isinstance(domain, ast.Name) and domain.id == "DOMAIN"):
            problems.append(f"{call.where} does not pass translation_domain=DOMAIN")
    return problems


def _given_placeholders(call: _Call) -> set[str] | None:
    """Return the passed placeholder names, or ``None`` if they are not literal."""

    node = call.keywords.get("translation_placeholders")
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return set()
    if isinstance(node, ast.Dict) and all(
        isinstance(key, ast.Constant) and isinstance(key.value, str)
        for key in node.keys
    ):
        return {key.value for key in node.keys if isinstance(key, ast.Constant)}
    return None


def _fields(message: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(message) if name}


def _parity_problems(
    scan: _Scan, messages: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    problems: list[str] = []
    for call in scan.calls:
        key = call.keywords.get("translation_key")
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            problems.append(f"{call.where} translation_key is not a string literal")
            continue
        given = _given_placeholders(call)
        if given is None:
            problems.append(f"{call.where} translation_placeholders is not literal")
            continue
        for index, (name, exceptions) in enumerate(messages):
            message = exceptions.get(key.value, {}).get("message")
            if message is None:
                problems.append(f"{call.where} {key.value} missing in {name}")
                continue
            used = _fields(message)
            if used - given or (index == 0 and used != given):
                problems.append(
                    f"{call.where} {key.value} placeholders {sorted(used)} in {name}, "
                    f"call passes {sorted(given)}"
                )
    return problems


def _placeholder_name_problems(scan: _Scan) -> list[str]:
    problems: list[str] = []
    for call in scan.calls:
        given = _given_placeholders(call) or set()
        problems += [
            f"{call.where} placeholder {name!r} is not allowed"
            for name in sorted(given - _ALLOWED_PLACEHOLDERS)
        ]
        node = call.keywords.get("translation_placeholders")
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                read = {
                    ident.lower()
                    for sub in ast.walk(value)
                    if isinstance(sub, (ast.Name, ast.Attribute))
                    and (ident := _name(sub)) is not None
                    # Dunders such as ``type(err).__name__`` name a class.
                    and not (ident.startswith("__") and ident.endswith("__"))
                }
                if any(
                    word in ident for ident in read for word in _FORBIDDEN_VALUE_WORDS
                ):
                    label = key.value if isinstance(key, ast.Constant) else "?"
                    problems.append(
                        f"{call.where} placeholder {label!r} reads {sorted(read)}"
                    )
    return problems


# --- the guard ---------------------------------------------------------------


def test_every_update_failed_is_translatable() -> None:
    scan = _scan(_package_sources())
    assert len(scan.calls) >= _MIN_CALLS, (
        f"found only {len(scan.calls)} UpdateFailed calls under {_PACKAGE}"
    )
    assert _coverage_problems(scan) == []


def test_keys_and_placeholders_match_every_locale() -> None:
    messages = _package_messages()
    assert len(messages) == 11, [name for name, _ in messages]
    assert _parity_problems(_scan(_package_sources()), messages) == []


def test_placeholder_names_are_allowed() -> None:
    assert _placeholder_name_problems(_scan(_package_sources())) == []


# --- self-tests: every arm must fail on a synthetic source -------------------

_GOOD = """
from homeassistant.helpers.update_coordinator import UpdateFailed
from .const import DOMAIN
raise UpdateFailed("x", translation_domain=DOMAIN, translation_key="k",
                   translation_placeholders={"status": "500"})
"""
_MESSAGES = [
    ("strings.json", {"k": {"message": "Error {status}."}}),
    ("de.json", {"k": {"message": "Fehler {status}."}}),
]


def test_self_test_good_source_passes_every_arm() -> None:
    scan = _scan({"good.py": _GOOD})
    assert len(scan.calls) == 1
    assert _coverage_problems(scan) == []
    assert _parity_problems(scan, _MESSAGES) == []
    assert _placeholder_name_problems(scan) == []


def test_self_test_coverage_arm_fails() -> None:
    sources = {
        "nokey.py": 'raise UpdateFailed("x", translation_domain=DOMAIN)',
        "literal.py": 'raise UpdateFailed("x", translation_domain="googlefindmy", translation_key="k")',
        "notext.py": 'raise UpdateFailed(translation_domain=DOMAIN, translation_key="k")',
        "alias.py": "from homeassistant.helpers.update_coordinator import UpdateFailed as UF",
        "sub.py": "class Mine(UpdateFailed):\n    pass",
        "assign.py": 'UF = UpdateFailed\nraise UF("x")',
        "partial.py": 'mk = functools.partial(UpdateFailed)\nraise mk("x")',
        "bare.py": "raise UpdateFailed",
        "barefrom.py": "raise UpdateFailed from err",
        "getattr.py": 'raise getattr(uc, "UpdateFailed")("x")',
        "attr.py": "UF = update_coordinator.UpdateFailed",
    }
    problems = _coverage_problems(_scan(sources))
    assert any("nokey.py" in p and "translation_key" in p for p in problems)
    assert any("literal.py" in p and "DOMAIN" in p for p in problems)
    assert any("notext.py" in p and "message text" in p for p in problems)
    for name in (
        "alias",
        "sub",
        "assign",
        "partial",
        "bare",
        "barefrom",
        "getattr",
        "attr",
    ):
        assert any(p.startswith(f"{name}.py:") for p in problems), name


def test_self_test_allowed_positions_pass() -> None:
    """Naming the class where it raises nothing must not be reported."""

    source = """
from homeassistant.helpers.update_coordinator import UpdateFailed
def check(err: UpdateFailed | None) -> UpdateFailed | None:
    try:
        pass
    except (UpdateFailed, ValueError):
        pass
    except UpdateFailed as exc:
        del exc
    result: list[UpdateFailed] = []
    del result
    return err if isinstance(err, UpdateFailed) else None
"""
    scan = _scan({"ok.py": source})
    assert scan.calls == []
    assert _coverage_problems(scan) == []


def test_self_test_parity_arm_fails() -> None:
    scan = _scan({"good.py": _GOOD})
    missing_locale = [*_MESSAGES, ("fr.json", {})]
    extra_in_locale = [*_MESSAGES, ("fr.json", {"k": {"message": "{status} {x}"}})]
    fewer_in_template = [("strings.json", {"k": {"message": "Error."}})]
    assert any(
        "missing in fr.json" in p for p in _parity_problems(scan, missing_locale)
    )
    assert any("fr.json" in p for p in _parity_problems(scan, extra_in_locale))
    assert any("strings.json" in p for p in _parity_problems(scan, fewer_in_template))
    computed = _scan(
        {
            "key.py": "raise UpdateFailed('x', translation_domain=DOMAIN, translation_key=k)"
        }
    )
    assert any(
        "not a string literal" in p for p in _parity_problems(computed, _MESSAGES)
    )


def test_self_test_placeholder_names_arm_fails() -> None:
    source = (
        "raise UpdateFailed('x', translation_domain=DOMAIN, translation_key='k', "
        "translation_placeholders={'name': str(dev_id)})"
    )
    problems = _placeholder_name_problems(_scan({"name.py": source}))
    assert problems == ["name.py:1 placeholder 'name' is not allowed"]


def test_self_test_placeholder_values_arm_fails() -> None:
    sources = {
        "dev.py": "raise UpdateFailed('x', translation_domain=DOMAIN, translation_key='k', "
        "translation_placeholders={'device_id': dev_name})",
        "title.py": "raise UpdateFailed('x', translation_domain=DOMAIN, translation_key='k', "
        "translation_placeholders={'device_id': str(entry.title)})",
        "mail.py": "raise UpdateFailed('x', translation_domain=DOMAIN, translation_key='k', "
        "translation_placeholders={'status': user_email})",
    }
    problems = _placeholder_name_problems(_scan(sources))
    assert [p.split(":")[0] for p in problems] == ["dev.py", "mail.py", "title.py"]
    class_name = _scan(
        {
            "cls.py": "raise UpdateFailed('x', translation_domain=DOMAIN, translation_key='k', "
            "translation_placeholders={'error_type': type(err).__name__})"
        }
    )
    assert _placeholder_name_problems(class_name) == []
