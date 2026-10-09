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
* ``entry_title_offenders`` finds logger calls that pass the title of a config
  entry (``entry.title``, ``config_entry.title``, ``self._entry.title``, a
  local derived from it, or the entry formatted as a whole);
  ``tests/test_log_hygiene_entry_title.py`` applies it to every module of the
  package, because the config flow sets the title to the account e-mail.

Behavioural checks pin that persisting a secrets bundle logs no value of the
bundle, no field name outside ``_LOGGABLE_BUNDLE_FIELDS`` (field names can embed
the account e-mail) and nothing derived from the bundle's e-mail, and that the
account label is masked on the bundle fallback and never carries the entry
title.

Not covered: ``_LOGGER.log(level, ...)`` calls and aliases such as ``log_fn``
(the Semgrep rule does not match them either); the secret-name check covers
``_resolve_entry_email`` only, so ``secrets_data`` in the credential seed and in
``_async_save_secrets_data`` stays a CodeQL source by design, and the behavioural
checks pin what those functions log. The key-flow check starts from the name
``key`` (each checked function must bind it, so a rename fails the check instead
of emptying it) and follows it into every name bound from an expression that
reads it: plain, annotated, augmented and walrus assignments, ``for`` and
comprehension targets, ``with ... as``, ``match`` capture patterns and, after a
``raise`` that reads it, every ``except ... as`` name of the function. Reads stop
only at comparisons and at ``_optional_credential_label``; calls such as
``str(key)`` and an assignment to ``d[k]`` or ``obj.attr`` taint the names in
the target. It does not follow values through other functions, through
``nonlocal``/``global`` or through a container read back under another name,
and it checks only the two functions. The title check sees attribute reads and
assignments in the same function; a title passed through another function or a
container is not followed.
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


def _target_names(target: ast.AST) -> set[str]:
    """Return every name in an assignment, loop or capture target. A ``match``
    pattern binds only its capture names, not the class or value names in it."""
    names: set[str] = set()
    if not isinstance(target, ast.pattern):
        names = {name.id for name in ast.walk(target) if isinstance(name, ast.Name)}
    names |= {
        pattern.name
        for pattern in ast.walk(target)
        if isinstance(pattern, ast.MatchAs | ast.MatchStar) and pattern.name
    }
    names |= {
        pattern.rest
        for pattern in ast.walk(target)
        if isinstance(pattern, ast.MatchMapping) and pattern.rest
    }
    return names


def _names_bound_from_key(function: ast.AST) -> set[str]:
    """Return ``key`` and every name bound from an expression that reads it,
    directly or through another such name (fixed point; forms listed in the
    module docstring)."""
    bound = {"key"}
    handlers = [
        handler.name
        for handler in ast.walk(function)
        if isinstance(handler, ast.ExceptHandler) and handler.name
    ]
    while True:
        before = len(bound)
        for node in ast.walk(function):
            pairs: list[tuple[ast.AST | None, list[ast.AST]]] = []
            if isinstance(node, ast.Assign):
                pairs.append((node.value, list(node.targets)))
            elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
                pairs.append((node.value, [node.target]))
            elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
                pairs.append((node.iter, [node.target]))
            elif isinstance(node, ast.With | ast.AsyncWith):
                pairs.extend(
                    (item.context_expr, [item.optional_vars])
                    for item in node.items
                    if item.optional_vars is not None
                )
            elif isinstance(node, ast.Match):
                pairs.append((node.subject, [case.pattern for case in node.cases]))
            elif isinstance(node, ast.Raise) and node.exc is not None:
                if _names_read(node.exc) & bound:
                    bound |= set(handlers)
            for value, targets in pairs:
                if value is not None and _names_read(value) & bound:
                    for target in targets:
                        bound |= _target_names(target)
        if len(bound) == before:
            return bound


def _binds_key(function: ast.AST) -> bool:
    """Return whether ``function`` binds the name ``key`` itself."""
    stored = {
        name.id
        for name in ast.walk(function)
        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
    }
    arguments = {arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg)}
    return "key" in stored | arguments


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
    unseeded = [function.name for function in functions if not _binds_key(function)]
    assert unseeded == [], (
        f"rename the key variable back to 'key' or extend the check: {unseeded}"
    )
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


# A config entry by name: ``entry``, ``config_entry``, ``self._entry``,
# ``existing_entry``, ``parent_entry``. ``subentry`` is not a config entry.
_ENTRY_OBJECT = re.compile(r"(^|\.)(\w*_)?entry$")


def _is_entry(node: ast.AST) -> bool:
    return _ENTRY_OBJECT.search(ast.unparse(node)) is not None


def _is_title_read(node: ast.AST) -> bool:
    """True for ``<entry>.title`` and ``getattr(<entry>, "title", ...)``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "title" and _is_entry(node.value)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and _is_entry(node.args[0])
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "title"
    )


_COMPREHENSION = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _comprehension_reads_title(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    bound: set[str],
) -> bool:
    """A comprehension target shadows a name of ``bound`` inside the
    comprehension; its first iterable is evaluated outside and still sees
    ``bound``."""
    inner = set(bound)
    for index, generator in enumerate(node.generators):
        if _reads_entry_title(generator.iter, bound if index == 0 else inner):
            return True
        inner -= _plain_target_names(generator.target)
        if any(_reads_entry_title(test, inner) for test in generator.ifs):
            return True
    elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
    return any(_reads_entry_title(element, inner) for element in elements)


def _reads_entry_title(node: ast.AST, bound: set[str]) -> bool:
    """True if ``node`` reads a config entry title (``_is_title_read``) or a
    name in ``bound``, honouring comprehension shadowing."""
    if _is_title_read(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in bound
    if isinstance(node, _COMPREHENSION):
        return _comprehension_reads_title(node, bound)
    return any(_reads_entry_title(child, bound) for child in ast.iter_child_nodes(node))


def _plain_target_names(target: ast.AST) -> set[str]:
    """Return the names a title assignment binds: plain names, also inside a
    tuple, list or starred target. ``entry.title = new_title`` stores into the
    entry and does not make ``entry`` itself a title, so attribute and
    subscript targets bind nothing (unlike ``_target_names``, which the key
    check needs)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _plain_target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return {name for elt in target.elts for name in _plain_target_names(elt)}
    return set()


_SCOPE = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Return the nodes of ``scope`` without those of nested functions and
    classes, which are scopes of their own."""
    nodes: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        nodes.append(node)
        if not isinstance(node, _SCOPE):
            pending.extend(ast.iter_child_nodes(node))
    return nodes


def _title_bindings(node: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """Return ``(target, value)`` pairs through which ``node`` binds names:
    assignments, ``for`` targets and ``with ... as``. A comprehension target
    is not bound: it exists only inside the comprehension, which a logger
    argument then reads in full."""
    if isinstance(node, ast.Assign):
        return [(target, node.value) for target in node.targets]
    if isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
        return [(node.target, node.value)] if node.value is not None else []
    if isinstance(node, ast.For | ast.AsyncFor):
        return [(node.target, node.iter)]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [(node.optional_vars, node.context_expr)]
    return []


def _is_logger_method(node: ast.AST) -> bool:
    """True for ``<logger>.<method>`` and ``getattr(<logger>, <name>)``."""
    if isinstance(node, ast.Attribute):
        return (
            node.attr in _LOG_METHODS
            and _LOGGER_OBJECT.search(ast.unparse(node.value)) is not None
        )
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and _LOGGER_OBJECT.search(ast.unparse(node.args[0])) is not None
    )


def _entry_object_args(arg: ast.AST) -> list[str]:
    """Return the config entries ``arg`` formats as a whole: the argument
    itself, a ``str``/``repr`` call or an f-string field. ``ConfigEntry.__repr__``
    carries the title, so logging the entry logs the account e-mail."""
    candidates = [arg]
    candidates += [
        node.args[0]
        for node in ast.walk(arg)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"str", "repr"}
        and node.args
    ]
    candidates += [
        node.value for node in ast.walk(arg) if isinstance(node, ast.FormattedValue)
    ]
    return [
        ast.unparse(node)
        for node in candidates
        if isinstance(node, ast.Name | ast.Attribute) and _is_entry(node)
    ]


def _local_names(scope: ast.AST) -> set[str]:
    """Return the names ``scope`` binds itself (parameters and assignment,
    ``for`` and ``with`` targets) without those declared ``nonlocal`` or
    ``global``. Such a name shadows the same name of an enclosing scope."""
    names: set[str] = set()
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        arguments = scope.args
        names |= {
            arg.arg
            for arg in [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                *([arguments.vararg] if arguments.vararg else []),
                *([arguments.kwarg] if arguments.kwarg else []),
            ]
        }
    declared: set[str] = set()
    for node in _scope_nodes(scope):
        for target, _value in _title_bindings(node):
            names |= _plain_target_names(target)
        if isinstance(node, ast.Nonlocal | ast.Global):
            declared |= set(node.names)
    return names - declared


def entry_title_offenders(tree: ast.AST) -> list[tuple[str, int, str]]:
    """Return ``(scope, line, form)`` for every logger call in ``tree`` that
    passes the title of a config entry: ``form`` is ``"title"`` for a title
    read, directly or through a local such as
    ``display_name = entry.title or entry.entry_id``, and the expression for an
    entry formatted as a whole (``_entry_object_args``). The config flow sets
    the title to the account e-mail.

    Each function, lambda, class body and the module body is one scope; a nested scope
    sees the title locals and logger aliases of the scopes around it, except
    names it binds itself (``_local_names``). A logger
    call is ``<logger>.<method>(...)``, ``getattr(<logger>, name)(...)`` or a
    call through a name bound to one (``log_fn = _LOGGER.debug if quiet else
    _LOGGER.warning``). Names are bound through assignments, ``for`` and
    ``with ... as`` targets, plain names only (``_plain_target_names``); a
    name once bound stays bound for its scope, so a later rebinding to a
    harmless value is still reported. ``tests/test_log_hygiene_entry_title.py``
    applies this to every module of the package."""
    offenders: list[tuple[str, int, str]] = []
    pending: list[tuple[ast.AST, frozenset[str], frozenset[str]]] = []
    if isinstance(tree, _SCOPE):
        pending.append((tree, frozenset(), frozenset()))
    else:
        pending.extend(
            (node, frozenset(), frozenset())
            for node in ast.walk(tree)
            if isinstance(node, _SCOPE)
        )
    while pending:
        scope, outer_bound, outer_aliases = pending.pop()
        nodes = _scope_nodes(scope)
        aliases = set(outer_aliases)
        bound = set(outer_bound)
        while True:
            before = len(bound) + len(aliases)
            for node in nodes:
                for target, value in _title_bindings(node):
                    names = _plain_target_names(target)
                    if any(_is_logger_method(part) for part in ast.walk(value)):
                        aliases |= names
                    if _reads_entry_title(value, bound):
                        bound |= names
            if len(bound) + len(aliases) == before:
                break
        name = getattr(
            scope, "name", "<lambda>" if isinstance(scope, ast.Lambda) else "<module>"
        )
        for call in nodes:
            if not isinstance(call, ast.Call) or not (
                _is_logger_method(call.func)
                or (isinstance(call.func, ast.Name) and call.func.id in aliases)
            ):
                continue
            arguments = [*call.args, *(kw.value for kw in call.keywords)]
            if any(_reads_entry_title(arg, bound) for arg in arguments):
                offenders.append((name, call.lineno, "title"))
            offenders.extend(
                (name, call.lineno, form)
                for arg in arguments
                for form in _entry_object_args(arg)
            )
        pending.extend(
            (
                node,
                frozenset(bound - _local_names(node)),
                frozenset(aliases - _local_names(node)),
            )
            for node in nodes
            if isinstance(node, _SCOPE)
        )
    return offenders


def test_label_entry_for_log_masks_the_bundle_email() -> None:
    entry = make_config_entry(
        entry_id="entry-1",
        data={DATA_SECRET_BUNDLE: {"username": _EMAIL}},
        options={},
        title=_EMAIL,
    )

    assert integration_init._label_entry_for_log(entry) == f"p***@{_DOMAIN}"  # type: ignore[arg-type]


def test_label_entry_for_log_uses_the_entry_id_for_an_address_without_at() -> None:
    entry = make_config_entry(
        entry_id="entry-3",
        data={"google_email": "pilotuser"},
        options={},
        title=_EMAIL,
    )

    assert integration_init._label_entry_for_log(entry) == "entry-3"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (_EMAIL, "entry-2"),
        ("Family account", "entry-2"),
        ("Jane Doe +49 170 0000000", "entry-2"),
        (f"alice@{_DOMAIN},bob@{_DOMAIN}", "entry-2"),
        (f"Family ({_EMAIL})", "entry-2"),
        ('"pilot user"@[192.0.2.1]', "entry-2"),
        (f"a@{_DOMAIN}/b@{_DOMAIN}|c@{_DOMAIN}", "entry-2"),
        ("pilot.user@exämple.de", "entry-2"),
    ],
)
def test_label_entry_for_log_never_logs_the_entry_title(
    title: str, expected: str
) -> None:
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
