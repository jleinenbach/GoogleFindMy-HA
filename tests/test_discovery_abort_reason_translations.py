# tests/test_discovery_abort_reason_translations.py
"""Guard tests binding discovery abort reasons to their translation namespace.

Background (regression class prevention)
----------------------------------------
The discovery validation chokepoint
``config_flow._normalize_and_validate_discovery_payload`` raises
``DiscoveryFlowError(<reason>)`` for rejected payloads. The two discovery
catch sites (``async_step_discovery`` and ``async_step_discovery_update_info``)
forward that reason verbatim via ``self.async_abort(reason=err.reason)``.
Home Assistant resolves abort reasons from the ``config.abort`` translation
namespace, NOT ``config.error``. A reason that only exists under
``config.error`` therefore surfaces as an untranslated/unknown abort.

PR #1128 introduced ``keys_missing`` (single-shared-key rule) but only added it
to ``config.error`` / ``options.error``. The structural test below enumerates
every reason the chokepoint can raise and asserts each one is resolvable as an
abort reason, so the same "added to the wrong namespace" mistake fails fast for
any future custom discovery-rejection reason — not just ``keys_missing``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    prepare_flow_hass_config_entries,
)

CONFIG_FLOW_PATH = Path(config_flow.__file__)
STRINGS_PATH = Path("custom_components/googlefindmy/strings.json")

# Chokepoint whose raised reasons are forwarded verbatim to ``async_abort``.
_DISCOVERY_VALIDATOR = "_normalize_and_validate_discovery_payload"
_DISCOVERY_ERROR = "DiscoveryFlowError"

# Reasons Home Assistant Core resolves from its shared ``common`` translation
# strings even when an integration does not define them locally. Any reason on
# this allowlist is treated as translatable without a local ``config.abort``
# entry. Keep this list minimal and documented: it exists only so the
# structural guard does not flag pre-existing Core reasons, while still failing
# for integration-owned reasons (such as ``keys_missing``) that have no Core
# fallback. See https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#aborting-a-flow
_HA_CORE_COMMON_ABORT_REASONS = frozenset(
    {
        "cannot_connect",
        "unknown",
        "invalid_auth",
        "already_configured",
    }
)


def _raised_discovery_reasons() -> set[str]:
    """Return literal reasons raised inside the discovery validation chokepoint.

    Parses ``config_flow.py`` and collects every
    ``raise DiscoveryFlowError("<literal>")`` inside
    ``_normalize_and_validate_discovery_payload``. These reasons are forwarded
    verbatim by the discovery catch sites, so each must resolve as an abort
    reason. Dynamic (non-literal) reasons are reported so the guard never
    silently ignores a reason it cannot statically classify.
    """

    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    target: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _DISCOVERY_VALIDATOR:
            target = node
            break
    assert target is not None, (
        f"Could not locate {_DISCOVERY_VALIDATOR} in {CONFIG_FLOW_PATH.name}; "
        "the discovery-reason guard can no longer enumerate raised reasons."
    )

    reasons: set[str] = set()
    for sub in ast.walk(target):
        if not isinstance(sub, ast.Raise) or not isinstance(sub.exc, ast.Call):
            continue
        func = sub.exc.func
        if not (isinstance(func, ast.Name) and func.id == _DISCOVERY_ERROR):
            continue
        assert sub.exc.args, f"{_DISCOVERY_ERROR} raised without a reason argument"
        first = sub.exc.args[0]
        # Dynamic reasons (e.g. a mapped variable) are surfaced via the special
        # "<dynamic>" marker; the assertion below intentionally rejects it so a
        # future non-literal reason cannot bypass the namespace guard unnoticed.
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            reasons.add(first.value)
        else:
            reasons.add("<dynamic>")
    return reasons


def _config_abort_reasons() -> set[str]:
    """Return the abort reasons defined under ``config.abort`` in strings.json."""

    data = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))
    abort = data.get("config", {}).get("abort", {})
    return set(abort.keys())


def test_discovery_validator_reasons_are_resolvable_abort_reasons() -> None:
    """Every chokepoint discovery reason must resolve as an abort reason.

    Structural invariant: each reason raised by
    ``_normalize_and_validate_discovery_payload`` (forwarded verbatim to
    ``async_abort``) is either defined in ``strings.json`` ``config.abort`` or a
    known Home Assistant Core common abort reason. This catches the PR #1128
    class of bug where an integration-owned reason lands only in
    ``config.error`` and surfaces untranslated during discovery.
    """

    raised = _raised_discovery_reasons()
    assert "<dynamic>" not in raised, (
        "A discovery reason is raised from a non-literal expression; extend this "
        "guard to resolve it statically before asserting translation coverage."
    )

    abort_reasons = _config_abort_reasons()
    unresolved = {
        reason
        for reason in raised
        if reason not in abort_reasons and reason not in _HA_CORE_COMMON_ABORT_REASONS
    }
    assert not unresolved, (
        "Discovery-rejection reasons surface via async_abort but are missing "
        "from strings.json 'config.abort' (and are not Home Assistant Core "
        f"common reasons): {sorted(unresolved)}. Add each to config.abort so "
        "the user sees the actionable guidance instead of an untranslated abort."
    )


def test_keys_missing_is_defined_in_config_abort() -> None:
    """``keys_missing`` must be present in ``config.abort`` (explicit anchor).

    Narrow companion to the structural invariant: ``keys_missing`` is the
    integration-owned discovery-rejection reason introduced by PR #1128. It has
    no Home Assistant Core fallback, so it must be explicitly defined under
    ``config.abort`` to render the shared-key guidance during discovery aborts.
    """

    assert "keys_missing" in _config_abort_reasons(), (
        "strings.json 'config.abort' is missing 'keys_missing'; the discovery "
        "abort path would surface an untranslated reason."
    )


@pytest.mark.asyncio
async def test_discovery_without_shared_key_aborts_with_keys_missing() -> None:
    """A discovered bundle without a shared_key must abort with ``keys_missing``.

    Behavioral counterpart to the structural guard: it drives the real discovery
    validation path with a shared-key-less secrets bundle and asserts the
    surfaced abort reason is ``keys_missing``. This proves the reason genuinely
    reaches ``async_abort`` (not merely that the string exists), so the
    translation guard above protects an actually reachable surface.
    """

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []

        def async_entries(self, domain: str) -> list[Any]:
            return []

        def async_get_subentries(self, _entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    flow = config_flow.ConfigFlow()
    flow.hass = _FlowHass()  # type: ignore[assignment]
    flow.context = {}

    # secrets bundle with an oauth/aas token but deliberately no shared_key:
    # the single-key discovery rule must reject it as a non-renewable dead end.
    discovery_info = {
        "secrets_json": {
            "google_email": "DiscoveryUser@example.com",
            "aas_token": "aas_et/DISCOVERY",
        }
    }

    result = await flow.async_step_discovery(discovery_info)

    assert result["type"] == "abort"
    assert result["reason"] == "keys_missing"


# The overwrite question introduced with the discovery credential fix ends in one
# of two aborts, and neither is a Home Assistant Core common reason. They are
# emitted by ``async_abort`` directly rather than through the
# ``DiscoveryFlowError`` chokepoint above, so the structural guard there does not
# see them: without this test a rename in the code would silently leave the user
# with an untranslated abort.
_OVERWRITE_ABORT_REASONS = ("credentials_updated", "credentials_kept")
_OVERWRITE_STEP = "discovery_overwrite"

_TRANSLATION_FILES = (
    STRINGS_PATH,
    *sorted(Path("custom_components/googlefindmy/translations").glob("*.json")),
)


@pytest.mark.parametrize("translation_path", _TRANSLATION_FILES)
def test_overwrite_step_is_translated_everywhere(translation_path: Path) -> None:
    """Every locale must carry the overwrite question and both of its outcomes."""

    payload = json.loads(translation_path.read_text(encoding="utf-8"))
    config = payload["config"]

    step = config["step"].get(_OVERWRITE_STEP)
    assert step, f"{translation_path} has no config.step.{_OVERWRITE_STEP}"
    for field in ("title", "description"):
        assert step.get(field), (
            f"{translation_path}: {_OVERWRITE_STEP}.{field} is empty"
        )
    for section in ("data", "data_description"):
        assert step.get(section, {}).get(config_flow._FIELD_OVERWRITE_CREDENTIALS), (
            f"{translation_path}: {_OVERWRITE_STEP}.{section} lacks the toggle text"
        )

    for reason in _OVERWRITE_ABORT_REASONS:
        assert config["abort"].get(reason), (
            f"{translation_path} has no config.abort.{reason}"
        )


def test_overwrite_abort_reasons_are_the_ones_the_flow_emits() -> None:
    """Bind the translated reasons to the literals in the step implementation.

    Reading the source keeps the guard honest without driving the whole flow a
    second time: the behavioural coverage lives in
    ``tests/test_config_flow_discovery.py``.
    """

    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    step = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == f"async_step_{_OVERWRITE_STEP}"
        ),
        None,
    )
    assert step is not None, f"async_step_{_OVERWRITE_STEP} vanished"

    emitted = {
        keyword.value.value
        for node in ast.walk(step)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_abort"
        for keyword in node.keywords
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant)
    }

    assert set(_OVERWRITE_ABORT_REASONS) <= emitted, (
        "the translated reasons are no longer the ones the step aborts with; "
        f"the step emits {sorted(emitted)}"
    )


# The discovery confirmation card. It was shown under ``step_id="discovery"``
# until the rename, and that id can never carry a text: Home Assistant reserves
# it for the *entry point* of a discovery flow, and
# ``tests/test_manifest_translation_schema.py`` bans the key from every
# translation file. Under ``discovery_confirm`` (the id 32 core integrations use
# and none use ``discovery`` for) the card is translatable, so it has to actually
# be translated everywhere.
_CONFIRM_STEP = "discovery_confirm"


@pytest.mark.parametrize("translation_path", _TRANSLATION_FILES)
def test_confirm_step_is_translated_everywhere(translation_path: Path) -> None:
    """Every locale must carry a title and description for the discovery card."""

    payload = json.loads(translation_path.read_text(encoding="utf-8"))
    step = payload["config"]["step"].get(_CONFIRM_STEP)
    assert step, f"{translation_path} has no config.step.{_CONFIRM_STEP}"
    for field in ("title", "description"):
        assert step.get(field), f"{translation_path}: {_CONFIRM_STEP}.{field} is empty"


def test_confirm_step_id_matches_the_translated_key() -> None:
    """Bind the translated key to the ``step_id`` the flow actually shows.

    Existence of a text proves nothing on its own: if the form went back to
    showing ``step_id="discovery"``, every locale would still have its entry and
    the user would still see Home Assistant's generic default.
    """

    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    entry_step = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_step_discovery"
        ),
        None,
    )
    assert entry_step is not None, "async_step_discovery vanished"

    shown = {
        keyword.value.value
        for node in ast.walk(entry_step)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_show_form"
        for keyword in node.keywords
        if keyword.arg == "step_id" and isinstance(keyword.value, ast.Constant)
    }

    assert shown == {_CONFIRM_STEP}, (
        "the discovery entry point must show its form under the translatable "
        f"step id; it shows {sorted(shown)}"
    )

    handler = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == f"async_step_{_CONFIRM_STEP}"
        ),
        None,
    )
    assert handler is not None, (
        f"async_step_{_CONFIRM_STEP} is missing: Home Assistant routes the submit "
        "of that form there, so without it the card cannot be confirmed"
    )
