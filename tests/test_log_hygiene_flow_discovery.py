# tests/test_log_hygiene_flow_discovery.py
"""Log hygiene for ``config_flow.py`` and ``discovery.py``.

Static checks walk every logger call in both modules:

* No format string carries credential wording in front of a ``%s`` on the
  first source line of the literal (mirror of the Semgrep rule
  ``python-logger-credential-disclosure``, as in ``test_log_hygiene_auth.py``).
* No logger call passes an upper-case constant with a credential word in its
  name, as a bare name or as an attribute (``_MAX_SECRETS_RETRY_ATTEMPTS``,
  ``discovery_module.SECRETS_DISCOVERY_NAMESPACE``).
* Every ``token_source`` value is built by ``_probe_source_label`` and every
  ``candidate_sources`` value by ``_cand_labels``, in a dict literal or a
  ``dict(...)`` call: the source name travels in the same tuple as the token,
  and discovery payloads can bring their own names.
* No ``extra`` item in ``config_flow.py`` is keyed ``email`` or holds a
  ``_mask_email_for_logs`` call: the address there can come from the secrets
  bundle, and the masked form is still reported by CodeQL.
* ``config_flow.py`` never passes ``source``, ``token``, ``candidates`` or
  ``cands`` to a logger outside these two helpers.
* ``discovery.py`` passes a namespace (``ns``, ``self._namespace``) to a
  logger only through ``_namespace_label``.

Behavioural checks pin that the failure path of the token probe and the
candidate list carry no part of a token or an address (the success and guard
paths are covered only statically), and that the account label for discovery logs
carries none either when no e-mail argument is given (with one, it is the
masked address).
Not covered: logger calls that receive the values through a helper other than
the ones named here, a namespace held under another name, ``extra`` built in a
variable before the call or from a list of pairs (``dict([...])``), an
address masked into a local before the call (``masked = _mask_email_for_logs(...)``
then ``extra={"account": masked}``), an
``extra`` key held in a constant, ``getattr`` with a constant's name as a
string, and logger methods bound to an alias (``dbg = _LOGGER.debug``).
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow, discovery

_MODULES = (Path(config_flow.__file__), Path(discovery.__file__))
_SEMGREP_FORMAT = re.compile(r"(?i).*(api.key|secret|credential|token|password).*%s.*")
_LOGGER_OBJECT = re.compile(r"(?i)(_logger|logger|self.logger|log)")
_LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warn",
        "warning",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)
_CREDENTIAL_CONSTANTS = re.compile(r"(?i)(token|secret|password|credential)")
# Token-shaped test value, assembled at runtime so no secret scanner sees a
# token literal in the source.
_TOKEN = "aas_et/" + "Z" * 40
_LOCAL_PART = "johnsmith"


def _logger_calls() -> list[tuple[Path, str, ast.Call]]:
    calls: list[tuple[Path, str, ast.Call]] = []
    for path in _MODULES:
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and _LOGGER_OBJECT.search(ast.unparse(node.func.value))
            ):
                calls.append((path, source, node))
    return calls


def _format_index(call: ast.Call) -> int:
    # ``logger.log(level, msg, *args)`` carries the level first.
    return 1 if isinstance(call.func, ast.Attribute) and call.func.attr == "log" else 0


def _call_values(call: ast.Call) -> list[ast.expr]:
    # The message argument is included: a message formatted before the call
    # (f-string, ``%``, ``.format``, ``+``) carries its values in that argument.
    start = _format_index(call)
    return [*call.args[start:], *(kw.value for kw in call.keywords)]


def _extra_items(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """Return the string-keyed items of a dict literal or a ``dict(...)`` call."""

    if isinstance(node, ast.Dict):
        return [
            (key.value, value)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        return [(kw.arg, kw.value) for kw in node.keywords if kw.arg is not None]
    return []


def _is_call_to(node: ast.expr, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def test_both_modules_have_logger_calls() -> None:
    names = {path.name for path, _source, _call in _logger_calls()}
    assert names == {"config_flow.py", "discovery.py"}
    assert len(_logger_calls()) >= 100


def test_no_credential_wording_before_placeholder() -> None:
    offenders = []
    for path, source, call in _logger_calls():
        index = _format_index(call)
        if len(call.args) <= index:
            continue
        segment = ast.get_source_segment(source, call.args[index])
        if segment is None:
            continue
        if _SEMGREP_FORMAT.match(segment.splitlines()[0]):
            offenders.append(f"{path.name}:{call.lineno}")
    assert offenders == []


def test_no_credential_named_constant_passed_to_logger() -> None:
    offenders = []
    for path, _source, call in _logger_calls():
        for arg in _call_values(call):
            for node in ast.walk(arg):
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                else:
                    continue
                if name.isupper() and _CREDENTIAL_CONSTANTS.search(name):
                    offenders.append(f"{path.name}:{call.lineno}:{name}")
    assert offenders == []


_FIXED_EXTRA = {
    "token_source": "_probe_source_label",
    "candidate_sources": "_cand_labels",
}


def test_candidate_sources_are_always_fixed_labels() -> None:
    seen = dict.fromkeys(_FIXED_EXTRA, 0)
    offenders = []
    for path, _source, call in _logger_calls():
        for arg in _call_values(call):
            for node in ast.walk(arg):
                for key, value in _extra_items(node):
                    if key not in _FIXED_EXTRA:
                        continue
                    seen[key] += 1
                    if not _is_call_to(value, _FIXED_EXTRA[key]):
                        offenders.append(f"{path.name}:{call.lineno}:{key}")
    assert seen == {"token_source": 3, "candidate_sources": 1}
    assert offenders == []


def test_config_flow_puts_no_address_into_extra() -> None:
    # Home Assistant's log format does not print ``extra``, so the field never
    # reaches the log a user downloads; CodeQL reported the masked address
    # read from the secrets bundle there all the same.
    offenders = []
    seen = 0
    for path, _source, call in _logger_calls():
        if path.name != "config_flow.py":
            continue
        for arg in _call_values(call):
            for node in ast.walk(arg):
                for key, value in _extra_items(node):
                    masked = any(
                        _is_call_to(inner, "_mask_email_for_logs")
                        for inner in ast.walk(value)
                        if isinstance(inner, ast.expr)
                    )
                    seen += 1
                    if key == "email" or masked:
                        offenders.append(f"{path.name}:{call.lineno}:{key}")
    assert seen > 0
    assert offenders == []


_CANDIDATE_NAMES = frozenset({"source", "token", "candidates", "cands"})


def _raw_candidate_names(node: ast.AST) -> list[str]:
    """Return candidate names used outside ``_probe_source_label``/``_cand_labels``."""

    if any(_is_call_to(node, helper) for helper in _FIXED_EXTRA.values()):
        return []
    if isinstance(node, ast.Name) and node.id in _CANDIDATE_NAMES:
        return [node.id]
    return [
        name
        for child in ast.iter_child_nodes(node)
        for name in _raw_candidate_names(child)
    ]


def test_config_flow_logs_candidates_only_through_labels() -> None:
    offenders = [
        f"{path.name}:{call.lineno}:{name}"
        for path, _source, call in _logger_calls()
        if path.name == "config_flow.py"
        for arg in _call_values(call)
        for name in _raw_candidate_names(arg)
    ]
    assert offenders == []


def _is_namespace(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "ns"
    return isinstance(node, ast.Attribute) and node.attr == "_namespace"


def test_discovery_never_logs_raw_namespace() -> None:
    offenders = [
        f"{path.name}:{call.lineno}"
        for path, _source, call in _logger_calls()
        if path.name == "discovery.py"
        for arg in _call_values(call)
        if not _is_call_to(arg, "_namespace_label")
        for node in ast.walk(arg)
        if _is_namespace(node)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("aas_token", "aas_token"),
        ("manual", "manual"),
        ("fcm_registration", "fcm_registration"),
        ("tokens_3", "tokens_n"),
        ("candidate_tokens_12", "candidate_tokens_n"),
        ("tokens_x", "other"),
        ("cache", "other"),
        (_TOKEN, "other"),
        (None, "other"),
    ],
)
def test_probe_source_label(source: object, label: str) -> None:
    assert config_flow._probe_source_label(source) == label


def test_candidate_list_hides_payload_names() -> None:
    labels = config_flow._cand_labels([("aas_token", "a"), (_TOKEN, "b")])
    assert labels == "aas_token, other"


@pytest.mark.asyncio
async def test_token_probe_logs_no_token_or_address(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _fake_new_api(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def _fake_probe(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(config_flow, "_async_new_api_for_probe", _fake_new_api)
    monkeypatch.setattr(config_flow, "_try_probe_devices", _fake_probe)
    email = f"{_LOCAL_PART}@example.com"
    candidates = [(_TOKEN, _TOKEN), ("tokens_0", _TOKEN + "1")]

    with caplog.at_level(logging.DEBUG, logger=config_flow.__name__):
        chosen = await config_flow.async_pick_working_token(
            object(),  # type: ignore[arg-type]
            email,
            candidates,
        )
        config_flow._log_token_validation_failure(candidates=candidates)

    assert chosen is None
    records = [r for r in caplog.records if r.name == config_flow.__name__]
    assert len(records) == 3
    sources = [r.__dict__.get("token_source") for r in records[:2]]
    assert sources == ["other", "tokens_n"]
    assert records[2].__dict__["candidate_sources"] == "other, tokens_n"
    for record in records:
        text = f"{record.getMessage()} {record.__dict__}"
        assert _TOKEN not in text
        assert _LOCAL_PART not in text
        assert "email" not in record.__dict__


def test_account_label_without_email_names_only_the_kind() -> None:
    address = f"{_LOCAL_PART}@example.com"
    digest = hashlib.sha256(_TOKEN.encode()).hexdigest()
    email_key = discovery._cloud_discovery_stable_key(None, None, {"email": address})
    token_key = discovery._cloud_discovery_stable_key(None, _TOKEN, None)
    assert email_key == f"email:{address}"
    assert token_key.startswith("token:")

    assert discovery._redact_account_for_log(None, email_key) == "<account from bundle>"
    assert discovery._redact_account_for_log(None, token_key) == "<token-keyed account>"
    assert (
        discovery._redact_account_for_log(None, "anonymous:abc")
        == "<anonymous account>"
    )
    assert discovery._redact_account_for_log(None, "other") == "<unidentified account>"
    for key in (email_key, token_key):
        label = discovery._redact_account_for_log(None, key)
        assert _LOCAL_PART[:4] not in label
        assert digest[:4] not in label
    assert discovery._redact_account_for_log(address, email_key) == "joh***@example.com"


def test_namespace_label() -> None:
    assert (
        discovery._namespace_label(discovery.CLOUD_DISCOVERY_NAMESPACE)
        == discovery.CLOUD_DISCOVERY_NAMESPACE
    )
    assert (
        discovery._namespace_label(discovery.SECRETS_DISCOVERY_NAMESPACE)
        == discovery.SECRETS_DISCOVERY_NAMESPACE
    )
    assert discovery._namespace_label("foreign.ns") == "other"
