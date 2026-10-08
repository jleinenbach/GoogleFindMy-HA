# tests/test_log_hygiene_init.py
"""Log hygiene for ``custom_components/googlefindmy/__init__.py``.

Static checks:

* No logger format string carries credential wording (``api key``, ``secret``,
  ``credential``, ``token``, ``password``) in front of a ``%s`` on the first
  source line of the literal (Semgrep ``python-logger-credential-disclosure``,
  AGENTS.md "Log hygiene for scanners"; same mirror as
  ``tests/test_log_hygiene_auth.py``).
* ``_resolve_entry_email`` binds no local whose name CodeQL classifies as a
  secret. CodeQL has no sanitizer for ``_mask_email_for_logs``, so such a local
  turned every log line with the account's masked e-mail into a clear-text
  logging result.
* Every key of ``OPTIONAL_CREDENTIAL_KEYS`` has its own fixed log name.
* The credential seed passes ``account_label=_label_entry_for_log(entry)`` when
  it persists a bundle.

Behavioural checks pin that persisting a secrets bundle logs no value of the
bundle, no field name outside ``_LOGGABLE_BUNDLE_FIELDS`` (field names can embed
the account e-mail) and nothing derived from the bundle's e-mail, and that the
account label is masked on both of its e-mail paths (bundle fallback and entry
title).

Not covered: ``_LOGGER.log(level, ...)`` calls and aliases such as ``log_fn``
(the Semgrep rule does not match them either); the secret-name check covers
``_resolve_entry_email`` only, so ``secrets_data`` in the credential seed and in
``_async_save_secrets_data`` stays a CodeQL source by design, and the behavioural
checks pin what those functions log. The key-flow check follows ``key`` through
plain, annotated and walrus assignments inside the two functions; it does not
follow it through containers (``d[k] = key``), attributes, calls other than
``_optional_credential_label`` or loop targets, and it checks no other function.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

import pytest

import custom_components.googlefindmy as integration_init
from custom_components.googlefindmy.const import (
    DATA_SECRET_BUNDLE,
    OPTIONAL_CREDENTIAL_KEYS,
)
from tests.helpers.config_entries_stub import make_config_entry

_INIT_PATH = Path(integration_init.__file__)
_SEMGREP_FORMAT = re.compile(r"(?i).*(api.key|secret|credential|token|password).*%s.*")
_LOGGER_OBJECT = re.compile(r"(?i)(_logger|logger|self.logger|log)")
_LOG_METHODS = frozenset(
    {"debug", "info", "warn", "warning", "error", "exception", "critical"}
)
# CodeQL ``HeuristicNames::maybeSecret`` (shared SensitiveDataHeuristics.qll);
# Python's ``re`` needs one look-behind per alternative length.
_CODEQL_SECRET_NAME = re.compile(
    r"(?is).*((?<!is)(?<!is_)secret|(?<!un)(?<!un_)(?<!is)(?<!is_)trusted(?!_iter)"
    r"|confidential).*"
)
_DOMAIN = "example.com"
_EMAIL = f"pilot.user@{_DOMAIN}"


def _module_tree() -> tuple[str, ast.Module]:
    source = _INIT_PATH.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _logger_calls() -> list[tuple[str, ast.Call]]:
    source, tree = _module_tree()
    return [
        (source, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and _LOGGER_OBJECT.search(ast.unparse(node.func.value))
    ]


def test_init_module_has_logger_calls() -> None:
    assert len(_logger_calls()) >= 200


def test_no_credential_wording_before_placeholder() -> None:
    offenders = []
    for source, call in _logger_calls():
        if not call.args:
            continue
        segment = ast.get_source_segment(source, call.args[0])
        if segment is None:
            continue
        if _SEMGREP_FORMAT.match(segment.splitlines()[0]):
            offenders.append(call.lineno)
    assert offenders == []


@pytest.mark.parametrize(
    ("name", "flagged"),
    [
        ("secrets_bundle", True),
        ("DATA_SECRET_BUNDLE", True),
        ("bundle", False),
        ("is_secret", False),
        ("account_label", False),
    ],
)
def test_codeql_secret_name_mirror(name: str, flagged: bool) -> None:
    assert bool(_CODEQL_SECRET_NAME.match(name)) is flagged


def test_resolve_entry_email_binds_no_secret_named_local() -> None:
    _source, tree = _module_tree()
    (function,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_entry_email"
    ]
    bound = {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    assert bound, "the function must still bind locals"
    assert sorted(name for name in bound if _CODEQL_SECRET_NAME.match(name)) == []


def test_every_optional_credential_key_has_its_own_log_name() -> None:
    fallback = integration_init._optional_credential_label("not-a-credential-key")
    labels = [
        integration_init._optional_credential_label(key)
        for key in OPTIONAL_CREDENTIAL_KEYS
    ]
    assert fallback not in labels
    assert len(set(labels)) == len(OPTIONAL_CREDENTIAL_KEYS)
    assert all(label not in OPTIONAL_CREDENTIAL_KEYS for label in labels)


def test_credential_seed_labels_the_account_from_the_entry() -> None:
    _source, tree = _module_tree()
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_async_save_secrets_data"
    ]
    assert len(calls) == 1, f"expected one caller, found {len(calls)}"
    (label,) = [kw.value for kw in calls[0].keywords if kw.arg == "account_label"]
    assert ast.unparse(label) == "_label_entry_for_log(entry)"


def _names_read(node: ast.AST) -> set[str]:
    """Return the names read in ``node``, skipping comparisons and the label
    helper: ``x == key`` and ``_optional_credential_label(key)`` pass no data
    from ``key`` into their result."""
    names: set[str] = set()

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.Compare):
            return
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "_optional_credential_label"
        ):
            return
        if isinstance(current, ast.Name):
            names.add(current.id)
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return names


def _names_bound_from_key(function: ast.AST) -> set[str]:
    """Return ``key`` and every local assigned from it, directly or through
    another such local (fixed point over plain, annotated and walrus
    assignments)."""
    bound = {"key"}
    while True:
        before = len(bound)
        for node in ast.walk(function):
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
                value = node.value
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if value is not None and _names_read(value) & bound:
                    for target in targets:
                        bound |= {
                            name.id
                            for name in ast.walk(target)
                            if isinstance(name, ast.Name)
                        }
        if len(bound) == before:
            return bound


def test_bundle_and_credential_keys_never_reach_a_log_argument() -> None:
    """Pin the data flow, not only the text: a field or key name is logged from
    a literal, never from the ``key`` variable or a local derived from it (equal
    at runtime, but CodeQL follows the variable back to the bundle or the
    secret-named constant). All arguments count, the format string included."""
    _source, tree = _module_tree()
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and (
            node.name == "_async_save_secrets_data"
            or any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_optional_credential_label"
                for call in ast.walk(node)
            )
        )
    ]
    names = sorted(function.name for function in functions)
    assert "_async_save_secrets_data" in names, names
    assert len(names) >= 2, f"no caller of _optional_credential_label: {names}"
    offenders = [
        f"{function.name}:{call.lineno}"
        for function in functions
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _LOG_METHODS
        and _LOGGER_OBJECT.search(ast.unparse(call.func.value))
        and any(
            _names_read(arg) & _names_bound_from_key(function)
            for arg in [*call.args, *(kw.value for kw in call.keywords)]
        )
    ]
    assert offenders == [], f"logger call passes a key-derived value: {offenders}"


def test_label_entry_for_log_masks_the_bundle_email() -> None:
    entry = make_config_entry(
        entry_id="entry-1",
        data={DATA_SECRET_BUNDLE: {"username": _EMAIL}},
        options={},
        title=_EMAIL,
    )

    assert integration_init._label_entry_for_log(entry) == f"p***@{_DOMAIN}"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (_EMAIL, f"p***@{_DOMAIN}"),
        ("Family account", "Family account"),
        (f"a@{_DOMAIN}, bob@{_DOMAIN}", f"*@{_DOMAIN}, b***@{_DOMAIN}"),
        ("Home @ Berlin", "Home @ Berlin"),
        (f"alice@{_DOMAIN},bob@{_DOMAIN}", f"a***@{_DOMAIN},b***@{_DOMAIN}"),
        (f"Family ({_EMAIL})", f"Family (p***@{_DOMAIN})"),
        ("pilot.user@exämple.de", "p***@exämple.de"),
        ("pilot.user@", "p***@"),
    ],
)
def test_label_entry_for_log_masks_an_e_mail_title(title: str, expected: str) -> None:
    entry = make_config_entry(entry_id="entry-2", data={}, options={}, title=title)

    assert integration_init._label_entry_for_log(entry) == expected  # type: ignore[arg-type]


class _FailingCache:
    """Cache stub whose writes all fail, so every failure branch logs."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        raise TypeError("write rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_label", "expected_label"),
    [("u***@elsewhere.test", "u***@elsewhere.test"), (None, "entry entry-7")],
)
async def test_saving_a_bundle_logs_no_bundle_field_or_email(
    caplog: pytest.LogCaptureFixture,
    account_label: str | None,
    expected_label: str,
) -> None:
    bundle = {
        "username": _EMAIL,
        "owner_key": "00" * 16,
        "oauth_token": "oauth-value",
        f"adm_token_{_EMAIL}": "adm-value",
    }
    with caplog.at_level(logging.DEBUG, logger=integration_init._LOGGER.name):
        await integration_init._async_save_secrets_data(
            _FailingCache("entry-7"),  # type: ignore[arg-type]
            bundle,
            account_label=account_label,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("no 'shared_key'" in message for message in messages)
    assert any("bundle field owner_key (str)" in message for message in messages)
    assert any("bundle field oauth_token (str)" in message for message in messages)
    assert any("bundle field other field (str)" in message for message in messages)
    assert all(_DOMAIN not in message for message in messages), messages
    assert all("adm_token" not in message for message in messages), messages
    assert all(expected_label in message for message in messages), messages
