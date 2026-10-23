# tests/test_guard_update_failed_translation.py
"""Guard: every ``UpdateFailed`` of the integration is translatable.

Home Assistant copies ``translation_key`` of an ``UpdateFailed`` into the
setup-retry reason of the integration card when the first refresh fails, and
the frontend shows that reason in the user's language. A raise without a key
falls back to the English text again, and nothing else would notice. The
checks run on the syntax tree of every module under
``custom_components/googlefindmy``:

(a) coverage: each ``UpdateFailed(...)`` call passes ``translation_domain`` as
    the name ``DOMAIN`` and a ``translation_key``; no module imports
    ``UpdateFailed`` under an alias or subclasses it, either of which would
    hide a raise from this scan. At least ``_MIN_CALLS`` calls must be found,
    so a wrong path cannot turn the guard green by finding nothing;
(b) parity: the key is a string literal, ``strings.json`` and every file in
    ``translations/`` define ``exceptions.<key>.message``, the template in
    ``strings.json`` uses exactly the placeholders the call passes, and no
    locale uses one the call does not pass;
(c) privacy: placeholder names come from ``_ALLOWED_PLACEHOLDERS``. A device
    name is derived information under ``AGENTS.md`` section 5 (b) and would
    land in the integration card and the log; devices are named by ID.

A ``**kwargs`` mapping at a call is not seen. Each arm has a self-test on a
synthetic source that must fail.
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


@dataclass
class _Call:
    where: str
    keywords: dict[str, ast.expr]


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


def _scan(sources: dict[str, str]) -> _Scan:
    scan = _Scan()
    for path, source in sorted(sources.items()):
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                scan.hidden += [
                    f"{path}:{node.lineno} imports UpdateFailed as {alias.asname}"
                    for alias in node.names
                    if alias.name == "UpdateFailed" and alias.asname
                ]
            elif isinstance(node, ast.ClassDef):
                if any(_name(base) == "UpdateFailed" for base in node.bases):
                    scan.hidden.append(f"{path}:{node.lineno} subclasses UpdateFailed")
            elif isinstance(node, ast.Call) and _name(node.func) == "UpdateFailed":
                keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                scan.calls.append(_Call(f"{path}:{node.lineno}", keywords))
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
        "alias.py": "from homeassistant.helpers.update_coordinator import UpdateFailed as UF",
        "sub.py": "class Mine(UpdateFailed):\n    pass",
    }
    problems = _coverage_problems(_scan(sources))
    assert any("nokey.py" in p and "translation_key" in p for p in problems)
    assert any("literal.py" in p and "DOMAIN" in p for p in problems)
    assert any("alias.py" in p for p in problems)
    assert any("sub.py" in p for p in problems)


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
        "translation_placeholders={'name': dev_name})"
    )
    problems = _placeholder_name_problems(_scan({"name.py": source}))
    assert problems == ["name.py:1 placeholder 'name' is not allowed"]
