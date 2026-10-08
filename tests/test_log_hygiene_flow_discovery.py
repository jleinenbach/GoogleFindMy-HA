# tests/test_log_hygiene_flow_discovery.py
"""Log hygiene for ``config_flow.py`` and ``discovery.py``.

Static checks walk every logger call in both modules:

* No format string carries credential wording in front of a ``%s`` on the
  first source line of the literal (mirror of the Semgrep rule
  ``python-logger-credential-disclosure``, as in ``test_log_hygiene_auth.py``).
* No logger call passes an upper-case constant with a credential word in its
  name (``_MAX_SECRETS_RETRY_ATTEMPTS``, ``SECRETS_DISCOVERY_NAMESPACE``).
* Every ``token_source`` value is built by ``_probe_source_label``: the source
  name travels in the same tuple as the token, and discovery payloads can
  bring their own names.
* ``discovery.py`` passes the namespace ``ns`` to a logger only through
  ``_namespace_label``.

Behavioural checks pin that the token probe, the candidate list and the
account label for discovery logs carry no part of a token or an address.
Not covered: logger calls that receive the values through a helper other than
the ones named here.
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
    {"debug", "info", "warn", "warning", "error", "exception", "critical"}
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


def _call_values(call: ast.Call) -> list[ast.expr]:
    return [*call.args[1:], *(kw.value for kw in call.keywords)]


def test_both_modules_have_logger_calls() -> None:
    names = {path.name for path, _source, _call in _logger_calls()}
    assert names == {"config_flow.py", "discovery.py"}
    assert len(_logger_calls()) >= 100


def test_no_credential_wording_before_placeholder() -> None:
    offenders = []
    for path, source, call in _logger_calls():
        if not call.args:
            continue
        segment = ast.get_source_segment(source, call.args[0])
        if segment is None:
            continue
        if _SEMGREP_FORMAT.match(segment.splitlines()[0]):
            offenders.append(f"{path.name}:{call.lineno}")
    assert offenders == []


def test_no_credential_named_constant_passed_to_logger() -> None:
    offenders = [
        f"{path.name}:{call.lineno}:{node.id}"
        for path, _source, call in _logger_calls()
        for arg in _call_values(call)
        for node in ast.walk(arg)
        if isinstance(node, ast.Name)
        and node.id.isupper()
        and _CREDENTIAL_CONSTANTS.search(node.id)
    ]
    assert offenders == []


def test_token_source_is_always_a_fixed_label() -> None:
    seen = 0
    offenders = []
    for path, _source, call in _logger_calls():
        for arg in _call_values(call):
            for node in ast.walk(arg):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values, strict=True):
                    if not (
                        isinstance(key, ast.Constant) and key.value == "token_source"
                    ):
                        continue
                    seen += 1
                    if not (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "_probe_source_label"
                    ):
                        offenders.append(f"{path.name}:{call.lineno}")
    assert seen == 3
    assert offenders == []


def _is_namespace_label(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_namespace_label"
    )


def test_discovery_never_logs_raw_namespace() -> None:
    offenders = [
        f"{path.name}:{call.lineno}"
        for path, _source, call in _logger_calls()
        if path.name == "discovery.py"
        for arg in _call_values(call)
        if not _is_namespace_label(arg)
        for node in ast.walk(arg)
        if isinstance(node, ast.Name) and node.id == "ns"
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
        config_flow._log_token_validation_failure(email=email, candidates=candidates)

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
