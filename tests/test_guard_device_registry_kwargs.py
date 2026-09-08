# tests/test_guard_device_registry_kwargs.py
"""Static ratchet against the pre-2026.8 device registry API.

Home Assistant 2026.8 made a device belong to exactly one config entry and
subentry.  Five shapes in this repository still speak the old model, and each of
them fails differently:

1. the four ownership kwargs (``add_config_entry_id`` and friends),
2. the same names as **strings**, used as dict keys or subscripts,
3. ``DeviceEntry.config_entries``, a shim that now always yields one element, so
   any ``len()`` or set-difference on it has lost its meaning,
4. the ``DeviceRegistry.devices`` mapping, in both the attribute and the
   ``getattr`` form, and
5. ``async_get_device``, whose identifier lookup is no longer unique.

The check is static and AST-based, following
``tests/test_guard_config_flow_reload_deprecation.py``: a runtime test cannot see
a call site that no test happens to exercise, and a grep cannot tell
``hass.config_entries`` (fine, that is the entry manager) from
``device.config_entries`` (the deprecated shim).

**Direction matters.**  Rules 3 and 5 use an *allow* list of receivers rather
than a *deny* list of device variable names.  A deny list only catches names
somebody thought of: measured on this tree it would have missed
``device_entry.config_entries`` while carrying two names that occur nowhere.  An
allow list cannot make that mistake -- whatever it does not know, it reports.

**Doubt resolves to "pass, but counted".**  A construct matching none of the
lists (say ``.config_entries`` on a call result) is let through and listed as
undecidable in the failure text.  A gate that blocks on doubt gets switched off
after the second false alarm, and a switched-off gate protects nothing.

The known-violation list below is the ratchet: it holds today's sites so the
gate is green on the day it lands, and every migration work package shrinks it.
The counts are part of it on purpose -- without them a *new* violation could
hide inside a function that is already listed.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOT = _REPO_ROOT / "custom_components" / "googlefindmy"

#: The compatibility layer is allowed to speak both dialects; that is its job.
_TRANSLATOR = "coordinator/helpers/registry.py"

_OWNERSHIP_KWARGS = frozenset(
    {
        "add_config_entry_id",
        "add_config_subentry_id",
        "remove_config_entry_id",
        "remove_config_subentry_id",
    }
)

#: Receivers for which ``.config_entries`` means the config entry *manager* or a
#: module, not the deprecated device shim.  Verified at each site on the tree:
#: ``self._hass`` and ``hass_arg`` hold a HomeAssistant instance, ``cf`` in
#: discovery.py holds the ``homeassistant`` package.  ``self`` covers
#: ``ConfigFlow.self.config_entries``; it currently matches nothing, which is
#: harmless here -- a stale entry in an *allow* list permits something that does
#: not occur, whereas a stale entry in a deny list makes the list look complete.
_CONFIG_ENTRIES_OWNERS = frozenset(
    {"hass", "_hass", "self", "self.hass", "self._hass", "hass_arg", "cf"}
)

#: Receivers whose ``.devices`` is *not* the deprecated registry mapping.  An
#: allow list for the same reason as ``_CONFIG_ENTRIES_OWNERS``: a deny list of
#: registry variable names missed ``service_device_registry`` outright and could
#: only ever catch names somebody had already thought of.  Verified at every
#: site: both names below resolve to ``SemanticLabelRecord.devices``, a
#: ``set[str]`` (``coordinator/main.py:596``), never a device registry.
_DEVICES_NON_REGISTRY_OWNERS = frozenset({"record", "self"})


@dataclass(frozen=True)
class Finding:
    """One occurrence of a deprecated shape."""

    rule: str
    path: str
    function: str
    detail: str

    @property
    def site(self) -> tuple[str, str, str]:
        """Location key without a line number, so edits do not churn the list."""
        return (self.rule, self.path, self.function)


def _receiver(node: ast.expr) -> str | None:
    """Render a simple receiver expression, or ``None`` if it is not simple."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _receiver(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the qualified name of the function containing it."""
    owner: dict[ast.AST, str] = {}

    def _walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                owner[child] = name
                _walk(child, name)
            else:
                owner[child] = prefix
                _walk(child, prefix)

    _walk(tree, "<module>")
    return owner


def _docstring_nodes(tree: ast.AST) -> set[ast.AST]:
    """Return every string constant that serves as a docstring."""
    found: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(body[0].value)
    return found


def scan_file(path: Path, relative: str) -> tuple[list[Finding], list[Finding]]:
    """Return ``(findings, undecidable)`` for one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _enclosing_functions(tree)
    docstrings = _docstring_nodes(tree)
    findings: list[Finding] = []
    undecidable: list[Finding] = []
    in_translator = relative == _TRANSLATOR

    def _where(node: ast.AST) -> str:
        return owner.get(node, "<module>")

    for node in ast.walk(tree):
        # Rule 1 -- ownership kwargs at a call site.
        if isinstance(node, ast.Call) and not in_translator:
            for keyword in node.keywords:
                if keyword.arg in _OWNERSHIP_KWARGS:
                    findings.append(
                        Finding("kwargs", relative, _where(node), keyword.arg)
                    )

        # Rule 2 -- the same names as string literals, in any position.
        #
        # Deliberately shape-independent.  An earlier draft looked at dict keys
        # and subscripts only and missed six real call sites of the form
        # ``kwargs.setdefault("add_config_entry_id", entry_id)`` -- neither a key
        # nor a subscript, but a plain argument.  That is the same lesson the
        # migration plan drew for ``getattr``: a rule written against the shapes
        # currently in view is blind to the next one.  Docstrings are exempt,
        # since documenting the old name is how the translator explains itself.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _OWNERSHIP_KWARGS
            and node not in docstrings
            and not in_translator
        ):
            findings.append(
                Finding("kwargs_string", relative, _where(node), node.value)
            )

        # Rule 3 -- DeviceEntry.config_entries, attribute and getattr form.
        if isinstance(node, ast.Attribute) and node.attr == "config_entries":
            receiver = _receiver(node.value)
            if receiver is None:
                undecidable.append(
                    Finding(
                        "config_entries",
                        relative,
                        _where(node),
                        "receiver is not a simple name",
                    )
                )
            elif receiver not in _CONFIG_ENTRIES_OWNERS and not in_translator:
                findings.append(
                    Finding("config_entries", relative, _where(node), receiver)
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
        ):
            attribute = node.args[1].value
            receiver = _receiver(node.args[0])
            if attribute == "config_entries":
                if receiver is None:
                    undecidable.append(
                        Finding(
                            "config_entries",
                            relative,
                            _where(node),
                            "getattr receiver is not a simple name",
                        )
                    )
                elif receiver not in _CONFIG_ENTRIES_OWNERS and not in_translator:
                    findings.append(
                        Finding(
                            "config_entries",
                            relative,
                            _where(node),
                            f"getattr {receiver}",
                        )
                    )
            if attribute == "devices":
                if receiver is None:
                    undecidable.append(
                        Finding(
                            "devices",
                            relative,
                            _where(node),
                            "getattr receiver is not a simple name",
                        )
                    )
                elif receiver not in _DEVICES_NON_REGISTRY_OWNERS and not in_translator:
                    findings.append(
                        Finding(
                            "devices", relative, _where(node), f"getattr {receiver}"
                        )
                    )
            # Rule 5 -- async_get_device through getattr.
            if attribute == "async_get_device" and not in_translator:
                findings.append(
                    Finding(
                        "async_get_device",
                        relative,
                        _where(node),
                        f"getattr {receiver}",
                    )
                )

        # Rule 4 -- the DeviceRegistry.devices mapping, attribute and getattr.
        if isinstance(node, ast.Attribute) and node.attr == "devices":
            receiver = _receiver(node.value)
            if receiver is None:
                undecidable.append(
                    Finding(
                        "devices",
                        relative,
                        _where(node),
                        "receiver is not a simple name",
                    )
                )
            elif receiver not in _DEVICES_NON_REGISTRY_OWNERS and not in_translator:
                findings.append(Finding("devices", relative, _where(node), receiver))

        # Rule 5 -- async_get_device as a direct attribute call.
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "async_get_device"
            and not in_translator
        ):
            findings.append(
                Finding(
                    "async_get_device",
                    relative,
                    _where(node),
                    _receiver(node.value) or "<expression>",
                )
            )

    return findings, undecidable


def scan_production_tree() -> tuple[list[Finding], list[Finding]]:
    """Scan every production module and return ``(findings, undecidable)``."""
    findings: list[Finding] = []
    undecidable: list[Finding] = []
    for path in sorted(_PRODUCTION_ROOT.rglob("*.py")):
        relative = path.relative_to(_PRODUCTION_ROOT).as_posix()
        file_findings, file_undecidable = scan_file(path, relative)
        findings.extend(file_findings)
        undecidable.extend(file_undecidable)
    return findings, undecidable


#: Known limitation: the key groups by function, so swapping one violation for
#: another inside an already-listed function keeps the count and passes both
#: tests.  Accepted deliberately -- the alternative is line numbers, which churn
#: on every unrelated edit and would make the table unmaintainable.  The
#: substitution case is covered by review, not by this file.
#:
#: The ratchet.  Key is ``(rule, file, enclosing function)`` -- no line numbers,
#: so unrelated edits do not churn it -- and the value is how many occurrences
#: that site holds today.  The count is what makes it a ratchet rather than a
#: whitelist: without it a *new* violation could hide inside a function that is
#: already listed.  Every migration work package shrinks this table; nothing may
#: ever add to it.
#:
#: Measured at the starting state (commit 77d9eb97): 88 occurrences at 48 sites.
KNOWN_VIOLATIONS: dict[tuple[str, str, str], int] = {
    (
        "async_get_device",
        "__init__.py",
        "<module>._async_relink_entities_for_entry.lookup_device",
    ): 1,
    (
        "async_get_device",
        "config_flow.py",
        "<module>.ConfigFlow._ensure_service_device_binding",
    ): 1,
    (
        "async_get_device",
        "coordinator/identity.py",
        "<module>.IdentityOperations._reset_resolver_offset",
    ): 1,
    (
        "async_get_device",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices",
    ): 1,
    (
        "async_get_device",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_service_device_exists",
    ): 1,
    ("async_get_device", "services.py", "<module>.async_rebuild_device_registry"): 2,
    (
        "config_entries",
        "__init__.py",
        "<module>._async_migrate_device_identifiers_to_entry_scope",
    ): 1,
    (
        "config_entries",
        "__init__.py",
        "<module>._async_purge_unloaded_subentry_registrations",
    ): 1,
    ("config_entries", "__init__.py", "<module>._async_refresh_device_urls"): 1,
    (
        "config_entries",
        "__init__.py",
        "<module>._async_relink_button_devices._resolve_button_target",
    ): 1,
    (
        "config_entries",
        "__init__.py",
        "<module>._async_relink_entities_for_entry.lookup_device",
    ): 1,
    (
        "config_entries",
        "__init__.py",
        "<module>._async_relink_subentry_entities._resolve_service_device",
    ): 1,
    ("config_entries", "__init__.py", "<module>._migrate_legacy_unique_ids"): 1,
    ("config_entries", "__init__.py", "<module>._normalize_device_identifier"): 1,
    ("config_entries", "__init__.py", "<module>._self_heal_device_registry"): 1,
    ("config_entries", "__init__.py", "<module>.async_remove_config_entry_device"): 1,
    (
        "config_entries",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices",
    ): 1,
    (
        "config_entries",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices._resolve_hub_name",
    ): 1,
    (
        "config_entries",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_service_device_exists._service_entry_links",
    ): 2,
    (
        "config_entries",
        "diagnostics.py",
        "<module>.async_get_config_entry_diagnostics",
    ): 1,
    ("config_entries", "services.py", "<module>.async_rebuild_device_registry"): 1,
    (
        "config_entries",
        "services.py",
        "<module>.async_rebuild_device_registry._entry_links_for_device",
    ): 1,
    (
        "config_entries",
        "services.py",
        "<module>.async_register_services._resolve_runtime_for_device_id",
    ): 1,
    (
        "config_entries",
        "services.py",
        "<module>.async_register_services.async_rebuild_registry_service",
    ): 1,
    (
        "config_entries",
        "services.py",
        "<module>.async_register_services.async_refresh_device_urls_service",
    ): 1,
    (
        "devices",
        "__init__.py",
        "<module>._async_migrate_device_identifiers_to_entry_scope",
    ): 2,
    ("devices", "__init__.py", "<module>._async_normalize_device_names"): 1,
    ("devices", "__init__.py", "<module>._async_refresh_device_urls"): 1,
    ("devices", "__init__.py", "<module>._async_relink_entities_for_entry"): 1,
    (
        "devices",
        "__init__.py",
        "<module>._async_relink_entities_for_entry.lookup_device",
    ): 1,
    (
        "devices",
        "__init__.py",
        "<module>._async_relink_subentry_entities._resolve_service_device",
    ): 1,
    ("devices", "__init__.py", "<module>._migrate_legacy_unique_ids"): 1,
    (
        "devices",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices",
    ): 1,
    (
        "devices",
        "coordinator/subentry.py",
        "<module>.SubentryOperations._refresh_subentry_index",
    ): 1,
    ("devices", "diagnostics.py", "<module>.async_get_config_entry_diagnostics"): 1,
    (
        "devices",
        "services.py",
        "<module>.async_register_services.async_refresh_device_urls_service",
    ): 1,
    (
        "kwargs",
        "__init__.py",
        "<module>._async_purge_unloaded_subentry_registrations",
    ): 2,
    (
        "kwargs",
        "services.py",
        "<module>.async_rebuild_device_registry._detach_hub_link_from_device",
    ): 3,
    (
        "kwargs_string",
        "config_flow.py",
        "<module>.ConfigFlow._ensure_service_device_binding",
    ): 9,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._call_device_registry_api",
    ): 1,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._device_registry_config_subentry_kwarg_name",
    ): 2,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices",
    ): 10,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices._heal_tracker_device_subentry",
    ): 8,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices._remove_hub_link",
    ): 4,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_registry_for_devices._update_device_with_kwargs",
    ): 2,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_service_device_exists",
    ): 5,
    (
        "kwargs_string",
        "coordinator/registry.py",
        "<module>.RegistryOperations._ensure_service_device_exists._detach_service_hub_link",
    ): 2,
    (
        "kwargs_string",
        "services.py",
        "<module>.async_rebuild_device_registry._detach_hub_link_from_device",
    ): 1,
}


def _current() -> dict[tuple[str, str, str], int]:
    """Group today's findings by site."""
    findings, _ = scan_production_tree()
    counted: Counter[tuple[str, str, str]] = Counter(item.site for item in findings)
    return dict(counted)


def test_no_new_deprecated_registry_usage() -> None:
    """No site may appear that is not in the ratchet, and none may grow."""
    current = _current()

    added = sorted(site for site in current if site not in KNOWN_VIOLATIONS)
    grown = sorted(
        (site, KNOWN_VIOLATIONS[site], count)
        for site, count in current.items()
        if site in KNOWN_VIOLATIONS and count > KNOWN_VIOLATIONS[site]
    )

    problems: list[str] = []
    for rule, path, function in added:
        problems.append(f"  new site: [{rule}] {path} in {function}")
    for (rule, path, function), was, now in grown:
        problems.append(f"  grew from {was} to {now}: [{rule}] {path} in {function}")

    assert not problems, (
        "The device registry ratchet caught new pre-2026.8 usage.\n"
        + "\n".join(problems)
        + "\n\nA device belongs to exactly one config entry and subentry since "
        "Home Assistant 2026.8. Express the intent (MOVE / ENSURE / DETACH) and "
        "let coordinator/helpers/registry.py translate it; see AGENTS.md, "
        "section 'Device registry ownership'."
    )


def test_ratchet_has_no_stale_entries() -> None:
    """Every ratchet entry must still match, with exactly its recorded count.

    A shrinking count means a work package landed and the table was not updated;
    that is a required edit, not a failure of the code.  Leaving stale entries in
    place would let the ratchet drift into a list of historical curiosities that
    no longer describes the tree.
    """
    current = _current()

    stale = sorted(site for site in KNOWN_VIOLATIONS if site not in current)
    shrunk = sorted(
        (site, KNOWN_VIOLATIONS[site], current[site])
        for site in KNOWN_VIOLATIONS
        if site in current and current[site] < KNOWN_VIOLATIONS[site]
    )

    problems = [
        f"  gone, remove the entry: [{rule}] {path} in {function}"
        for rule, path, function in stale
    ]
    problems += [
        f"  now {now} instead of {was}, lower the count: [{rule}] {path} in {function}"
        for (rule, path, function), was, now in shrunk
    ]

    assert not problems, "KNOWN_VIOLATIONS no longer describes the tree:\n" + "\n".join(
        problems
    )


def test_undecidable_constructs_are_reported_not_hidden() -> None:
    """Doubtful constructs pass, but their number is stated.

    The gate lets a construct through when it cannot classify the receiver, so a
    false alarm can never be the reason someone disables it.  The count is
    printed and pinned here, so a sudden rise is visible instead of silent.
    """
    _, undecidable = scan_production_tree()
    for item in undecidable:
        print(
            f"undecidable: [{item.rule}] {item.path} in {item.function}: {item.detail}"
        )
    assert len(undecidable) <= 5, (
        f"{len(undecidable)} undecidable constructs, up from 0 at the time the "
        "gate landed. Review them and either extend the allow lists with a "
        "verified receiver or migrate the sites."
    )
