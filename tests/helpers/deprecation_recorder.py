# tests/helpers/deprecation_recorder.py
"""Record Home Assistant ``report_usage`` calls raised from this integration.

Home Assistant deprecates device registry APIs by calling
:func:`homeassistant.helpers.frame.report_usage`.  Tests that want to assert
"this code path no longer triggers a deprecation" need to observe those calls.

Two properties of that mechanism drive the design of this module:

* ``homeassistant.helpers.device_registry`` binds ``report_usage`` **by value**
  at import time (``from .frame import ... report_usage``).  Replacing
  ``sys.modules["homeassistant.helpers.frame"]`` therefore does not reach the
  already bound reference.  The recorder is installed at every binding site
  instead of at the module, see :data:`BOUND_MODULES`.  This follows the rule in
  ``tests/AGENTS.md`` -> "Patching a symbol that other modules copy at import
  time".
* The recorder **delegates** to the original callable instead of replacing it.
  Replacing it would silence real behaviour: with the default
  ``core_behavior=ReportBehavior.ERROR`` a deprecated call made from a frame
  outside ``custom_components/`` raises ``RuntimeError``, and two tests in this
  repository rely on exactly that (see ``tests/test_device_entity_registration``
  and ``tests/test_entity_device_info_contract``).  A recorder that swallowed
  the exception would turn a failing test green without fixing anything.

Absence of recorded entries is only meaningful once the recorder is proven to be
wired up; ``tests/test_deprecation_recorder_wiring.py`` provides that proof, and
``tests/test_guard_device_registry_deprecations.py`` carries a canary for the
same reason.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from types import CodeType, FunctionType, ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pytest

#: Modules that bind ``report_usage`` into their own namespace at import time.
#:
#: Enumerated rather than guessed: on Core 2026.9.1 a regex over
#: ``from .frame import`` / ``from .helpers.frame import`` across the
#: ``homeassistant`` package finds nine modules whose import list contains
#: ``report_usage``.  All nine are listed.  A module that is absent from
#: ``sys.modules`` or does not expose the symbol is skipped, so the tuple is
#: safe to keep wide, and an over-wide tuple is the right error to make here:
#: a missing entry means silent under-reporting.
BOUND_MODULES: tuple[str, ...] = (
    "homeassistant.helpers.frame",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.device",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.helper_integration",
    "homeassistant.helpers.trigger",
    "homeassistant.config_entries",
    "homeassistant.data_entry_flow",
)

#: Only frames below this path are attributed to this integration.
INTEGRATION_PATH_MARKER = "custom_components/googlefindmy/"


@dataclass(frozen=True)
class DeprecationReport:
    """A single ``report_usage`` call observed during a test."""

    what: str
    breaks_in_ha_version: str | None
    integration_domain: str | None
    #: ``file:line:function`` for every frame below ``custom_components/googlefindmy``.
    integration_frames: tuple[str, ...]

    @property
    def from_integration(self) -> bool:
        """Whether the call originated inside this integration's code."""
        return bool(self.integration_frames)


@dataclass
class DeprecationRecorder:
    """Collect ``report_usage`` calls, then delegate to the original callable."""

    reports: list[DeprecationReport] = field(default_factory=list)
    #: Module names the recorder was actually bound into, filled by
    #: :func:`install_recorder`.  Tests assert on this to detect a lost wiring.
    bound_modules: tuple[str, ...] = ()
    _originals: dict[str, Any] = field(default_factory=dict, repr=False)
    _monkeypatch: Any = field(default=None, repr=False)

    def clear(self) -> None:
        """Drop everything recorded so far."""
        self.reports.clear()

    @property
    def integration_reports(self) -> list[DeprecationReport]:
        """Reports that have at least one frame inside this integration."""
        return [report for report in self.reports if report.from_integration]

    def matching(self, needle: str) -> list[DeprecationReport]:
        """Reports whose ``what`` text contains ``needle``."""
        return [report for report in self.reports if needle in report.what]

    def make_proxy(self, module_name: str, original: Any) -> Any:
        """Build the callable that replaces ``report_usage`` in ``module_name``."""
        self._originals[module_name] = original

        def _proxy(
            what: str,
            **kwargs: Any,
        ) -> None:
            self.reports.append(
                DeprecationReport(
                    what=what,
                    breaks_in_ha_version=kwargs.get("breaks_in_ha_version"),
                    integration_domain=kwargs.get("integration_domain"),
                    integration_frames=_integration_frames(),
                )
            )
            original(what, **kwargs)

        return _proxy


def _integration_frames() -> tuple[str, ...]:
    """Return ``file:line:function`` for frames inside this integration."""
    return tuple(
        f"{frame.filename}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_stack()
        if INTEGRATION_PATH_MARKER in frame.filename.replace("\\", "/")
    )


def install_recorder(
    monkeypatch: pytest.MonkeyPatch,
    recorder: DeprecationRecorder,
    modules: tuple[str, ...] = BOUND_MODULES,
) -> tuple[str, ...]:
    """Bind ``recorder`` wherever ``report_usage`` was imported by value.

    Returns the module names the recorder was bound into.  Callers assert on
    that return value: if ``homeassistant.helpers.device_registry`` disappears
    from it because Core changed its import style, the recorder would silently
    observe nothing, and silence is indistinguishable from "no deprecation".
    """
    recorder._monkeypatch = monkeypatch
    _set_active(recorder, modules)
    bound: list[str] = []
    for name in modules:
        module: ModuleType | None = sys.modules.get(name)
        if module is None:
            continue

        original = getattr(module, "report_usage", None)
        if original is not None:
            monkeypatch.setattr(
                module,
                "report_usage",
                recorder.make_proxy(name, original),
                raising=True,
            )
            bound.append(name)
            continue

        # Only reached when the module does not expose the symbol itself.  The
        # stubbed frame module in tests/conftest.py can route through a helper
        # instance (``frame_module.frame_helper.report_usage``); patching that
        # helper *in addition* to the module would record a single call twice,
        # because the module-level function delegates to it.  Measured: two
        # entries for one call.  So the helper is a fallback, not an addition.
        helper = getattr(module, "frame_helper", None)
        helper_original = getattr(helper, "report_usage", None)
        if helper is not None and helper_original is not None:
            helper_name = f"{name}.frame_helper"
            monkeypatch.setattr(
                helper,
                "report_usage",
                recorder.make_proxy(helper_name, _drop_positional(helper_original)),
                raising=True,
            )
            bound.append(helper_name)

    recorder.bound_modules = tuple(bound)
    return recorder.bound_modules


def _drop_positional(original: Any) -> Any:
    """Adapt a ``(*args, **kwargs)`` stub to the ``report_usage`` signature."""

    def _call(what: str, **kwargs: Any) -> None:
        original(what, **kwargs)

    return _call


#: Single-slot holder for the recorder installed by the active fixture.  A list
#: rather than a module-level name so the helpers below need no ``global``.
_ACTIVE: list[tuple[DeprecationRecorder, tuple[str, ...]]] = []


def _set_active(recorder: DeprecationRecorder, modules: tuple[str, ...]) -> None:
    """Remember the recorder so :func:`rebind_active` can re-install it."""
    _ACTIVE.clear()
    _ACTIVE.append((recorder, modules))


def reset_active() -> None:
    """Forget the active recorder; called when its fixture tears down."""
    _ACTIVE.clear()


def rebind_active() -> tuple[str, ...]:
    """Re-install the active recorder after Home Assistant modules were reloaded.

    ``tests/conftest.py::use_real_homeassistant_modules`` drops every
    ``homeassistant*`` entry from ``sys.modules`` and imports the real packages
    again.  Every binding made before that point refers to the discarded module
    objects, so without this call the recorder would observe nothing under
    exactly the fixture that exercises real Core code (see ``EE-30`` in the
    migration plan).  Returns the freshly bound module names, empty when no
    recorder is active.
    """
    if not _ACTIVE:
        return ()
    recorder, modules = _ACTIVE[0]
    if recorder._monkeypatch is None:  # pragma: no cover - defensive
        return ()
    return install_recorder(recorder._monkeypatch, recorder, modules)


#: File name used for synthetic frames attributed to this integration.  Home
#: Assistant's ``get_integration_frame`` only inspects ``co_filename`` for the
#: substring ``custom_components/<domain>/``; it never touches the file system,
#: so this path does not have to exist.
PROBE_FILENAME = (
    f"{INTEGRATION_PATH_MARKER}_deprecation_probe.py"
    if INTEGRATION_PATH_MARKER.startswith("/")
    else f"/repo/{INTEGRATION_PATH_MARKER}_deprecation_probe.py"
)


def call_from_integration_frame(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call ``func`` from a stack frame Core attributes to this integration.

    Deprecated Core APIs branch on *who* called them: from a custom integration
    they log, from anywhere else they raise.  Tests that want the logging branch
    therefore need a frame below ``custom_components/googlefindmy/``.  Compiling
    a trampoline with that file name is the least invasive way to get one; the
    alternative would be adding test-only helpers to production code.
    """
    source = "def _invoke(func, args, kwargs):\n    return func(*args, **kwargs)\n"
    module_code = compile(source, PROBE_FILENAME, "exec")
    # Instantiate the function straight from its code object: the frame gets
    # ``PROBE_FILENAME`` as ``co_filename`` without executing module-level code.
    trampoline_code = next(
        const for const in module_code.co_consts if isinstance(const, CodeType)
    )
    trampoline = FunctionType(trampoline_code, {})
    return trampoline(func, args, kwargs)


def bind_into(
    monkeypatch: pytest.MonkeyPatch,
    recorder: DeprecationRecorder,
    obj: Any,
    label: str | None = None,
) -> str | None:
    """Bind ``recorder`` into the namespace ``obj``'s code actually resolves.

    Patching ``sys.modules`` is not always enough.  Measured on Core 2026.9.1
    under ``use_real_homeassistant_modules``: the registry returned by
    ``device_registry.async_get(hass)`` is an instance of a class whose
    ``__globals__`` belong to an *earlier* module object, kept alive by the
    ``hass`` fixture, while ``sys.modules`` already holds a freshly imported
    one.  Patching only the latter leaves the live call path untouched, and the
    recorder records nothing while looking correctly installed.

    Pass the object whose code runs -- typically ``type(registry)`` -- and this
    resolves down to the namespace that its functions read ``report_usage``
    from.  Returns the label bound, or ``None`` when the namespace does not use
    the symbol at all.
    """
    namespace = _resolve_namespace(obj)
    if namespace is None or "report_usage" not in namespace:
        return None
    resolved_label = label or getattr(obj, "__qualname__", None) or repr(obj)
    monkeypatch.setitem(
        namespace,
        "report_usage",
        recorder.make_proxy(resolved_label, namespace["report_usage"]),
    )
    recorder.bound_modules = (*recorder.bound_modules, resolved_label)
    return resolved_label


def _resolve_namespace(obj: Any) -> dict[str, Any] | None:
    """Return the globals dict whose ``report_usage`` ``obj``'s code reads."""
    if isinstance(obj, ModuleType):
        return obj.__dict__
    globals_dict = getattr(obj, "__globals__", None)
    if isinstance(globals_dict, dict):
        return globals_dict
    if isinstance(obj, type):
        for value in vars(obj).values():
            candidate = getattr(value, "__globals__", None)
            if isinstance(candidate, dict):
                return candidate
    return None


def reporter_available(registry: Any) -> bool:
    """Whether the running Core version reports these deprecations at all.

    Core 2026.8 changed the device registry *behaviour* but shipped no
    ``report_usage`` for the affected APIs; the reports first appear in 2026.9.
    Measured: on 2026.8.2 the device registry holds eight ``report_usage`` calls,
    none of them in ``async_get_device`` or in the ownership branches of
    ``async_update_device``.  Every assertion of the form "the recorder saw X" is
    therefore version dependent, and the version this repository pins in
    ``poetry.lock`` is one of the silent ones.

    Deliberately a **source** check and not a probe call: probing would require
    installing the recorder first, and one caller
    (``test_module_level_binding_alone_is_not_enough``) exists precisely to
    observe the state *before* any extra binding.  A probe would destroy the
    thing it measures there.
    """
    import inspect

    try:
        source = inspect.getsource(type(registry).async_get_device)
    except (OSError, TypeError):  # pragma: no cover - defensive
        return False
    return "report_usage" in source
