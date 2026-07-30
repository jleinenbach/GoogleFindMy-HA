# tests/test_guard_config_flow_reload_deprecation.py
"""Guard keeping the config flow free of the reload combination HA drops in 2026.12.

Home Assistant deprecates a reloading config-flow method on an entry that also
carries an update listener: warning from 2026.6, error from 2026.12. This
integration keeps its update listener (it adopts changed watch paths at
runtime), so the config flow must not reload:

* no ``async_update_reload_and_abort`` call,
* every ``_abort_if_unique_id_configured`` call passes ``reload_on_update=False``,
* no unconditional ``async_update_and_abort`` call, which is the tempting
  replacement but exists only from HA 2025.11.0 while ``hacs.json`` declares
  2025.9.1 as the minimum (there the method lives on ``ConfigSubentryFlow``
  alone),
* and no ``OptionsFlowWithReload`` base for the options flow. That fourth shape
  is inherited rather than called, which is why the three call-site checks
  above could not see it, and it is the one that actually bit: for
  ``OptionsFlowWithReload`` the core does not wait for 2026.12 but raises
  ``ValueError`` today, in ``OptionsFlowManager.async_finish_flow``, *before*
  the options are written.

A runtime test cannot catch the last point: the suite runs a newer Home
Assistant than the declared minimum, so an unconditional call would pass
happily and only break for users on the oldest supported version. The check is
therefore static, over the parsed production source rather than over a grep,
so that a renamed local variable or a wrapped line cannot slip past it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_FLOW = _REPO_ROOT / "custom_components" / "googlefindmy" / "config_flow.py"


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    """Return every call whose attribute or plain name is ``name``."""

    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            found.append(node)
        elif isinstance(func, ast.Name) and func.id == name:
            found.append(node)
    return found


def _parsed() -> ast.AST:
    return ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))


def test_the_config_flow_never_reloads_the_entry_itself() -> None:
    """``async_update_reload_and_abort`` must not be called any more."""

    offenders = [
        call.lineno for call in _calls(_parsed(), "async_update_reload_and_abort")
    ]

    assert offenders == [], (
        "config_flow.py reloads from the flow at line(s) "
        f"{offenders}; use _async_update_entry_and_abort instead, the update "
        "listener in __init__.py performs the reload"
    )


def test_every_unique_id_abort_disables_the_core_reload() -> None:
    """``_abort_if_unique_id_configured`` must pass ``reload_on_update=False``."""

    offenders: list[int] = []
    for call in _calls(_parsed(), "_abort_if_unique_id_configured"):
        keywords = {
            keyword.arg: keyword.value
            for keyword in call.keywords
            if keyword.arg is not None
        }
        value = keywords.get("reload_on_update")
        if not (isinstance(value, ast.Constant) and value.value is False):
            offenders.append(call.lineno)

    assert offenders == [], (
        "_abort_if_unique_id_configured without reload_on_update=False at "
        f"line(s) {offenders}; with an update listener present that is the "
        "combination Home Assistant turns into an error in 2026.12"
    )


def test_no_unconditional_call_to_a_method_the_minimum_version_lacks() -> None:
    """``ConfigFlow.async_update_and_abort`` exists only from HA 2025.11.0.

    Guarded statically on purpose: the test environment runs a newer core, so
    an unconditional call would be green here and broken for users on 2025.9.1.
    """

    offenders = [call.lineno for call in _calls(_parsed(), "async_update_and_abort")]

    assert offenders == [], (
        "async_update_and_abort called at line(s) "
        f"{offenders}; ConfigFlow gained it in HA 2025.11.0 while hacs.json "
        "declares 2025.9.1, so use async_update_entry plus async_abort"
    )


def test_the_declared_minimum_is_still_the_one_this_guard_assumes() -> None:
    """If the minimum is raised past 2025.11.0, the guard above may be relaxed."""

    hacs = (_REPO_ROOT / "hacs.json").read_text(encoding="utf-8")

    assert '"homeassistant": "2025.9.1"' in hacs, (
        "the declared minimum changed; re-check whether "
        "ConfigFlow.async_update_and_abort is now available everywhere"
    )


def test_the_options_flow_base_is_not_the_reloading_one() -> None:
    """The fourth shape of the same combination, and the one that bit.

    ``OptionsFlowWithReload`` is not a call, so none of the checks above could
    see it: it is inherited. With an update listener registered,
    ``OptionsFlowManager.async_finish_flow`` raises ``ValueError`` *before* it
    writes the options, so every options submission on a loaded entry fails and
    persists nothing.

    Checked on the class rather than on the assignment, so that setting
    ``automatic_reload`` by hand is caught as well.
    """

    from custom_components.googlefindmy import config_flow

    assert not getattr(config_flow.OptionsFlowHandler, "automatic_reload", False), (
        "OptionsFlowHandler carries an automatic reload while the integration "
        "registers a config entry update listener; the core refuses that pair "
        "before it writes the options"
    )


def test_the_update_listener_this_guard_assumes_is_still_registered() -> None:
    """Positive control for the whole file.

    Every check here rests on the integration having an update listener; the
    core allows a reloading flow for an entry that has none. Without this the
    file could stay green while guarding nothing.
    """

    init_source = (
        _REPO_ROOT / "custom_components" / "googlefindmy" / "__init__.py"
    ).read_text(encoding="utf-8")

    assert "add_update_listener(" in init_source, (
        "no update listener is registered any more; every assumption in this "
        "file has to be re-checked before the guards are relaxed"
    )
