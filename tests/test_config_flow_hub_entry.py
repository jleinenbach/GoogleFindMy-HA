# tests/test_config_flow_hub_entry.py
"""Tests covering hub subentry registration, delegation, and legacy-core fallbacks."""

from __future__ import annotations

import inspect
import logging
from dataclasses import is_dataclass
from types import SimpleNamespace
from typing import Protocol

import pytest
from homeassistant.core import HomeAssistant

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    ConfigEntriesFlowManagerStub,
    attach_config_entries_flow_manager,
)


class _SubentrySupportToggle(Protocol):
    """Protocol covering the shared fixture interface for subentry toggles."""

    def as_modern(self) -> object | None:
        """Restore native subentry support."""

    def as_legacy(self) -> type[object]:
        """Simulate legacy cores lacking subentry support."""


@pytest.mark.parametrize(
    "simulate_legacy_core",
    [False, True],
)
def test_supported_subentry_types_returns_empty_to_hide_ui(
    subentry_support: _SubentrySupportToggle,
    simulate_legacy_core: bool,
) -> None:
    """Subentry mapping must be empty to hide manual add buttons in HA UI."""

    if simulate_legacy_core:
        subentry_support.as_legacy()
    else:
        subentry_support.as_modern()

    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(  # type: ignore[arg-type]
        SimpleNamespace()
    )

    # Must return empty dict to hide "Add hub feature group" and
    # "Add service feature group" buttons. Subentries are provisioned
    # programmatically by the coordinator, not manually by users.
    assert mapping == {}
    assert SUBENTRY_TYPE_HUB not in mapping
    assert SUBENTRY_TYPE_SERVICE not in mapping
    assert SUBENTRY_TYPE_TRACKER not in mapping


@pytest.mark.asyncio
async def test_hub_flow_creates_entry_when_requested(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hub entry point should create a subentry with proper handler registration."""

    caplog.set_level(logging.INFO)

    entry = make_config_entry(entry_id="entry-123", subentries={})

    class _ConfigEntriesManager(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.lookups: list[str] = []
            self.entry = entry
            attach_config_entries_flow_manager(self)

        def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
            self.lookups.append(entry_id)
            if entry_id == entry.entry_id:
                return entry
            return None

    hass = SimpleNamespace(config_entries=_ConfigEntriesManager())

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": entry.entry_id}
    flow.config_entry = entry  # type: ignore[assignment]

    result = await flow.async_step_hub()
    if inspect.isawaitable(result):
        result = await result

    # Flow should create an entry (not abort)
    assert result["type"] == "create_entry"
    assert any(
        "Hub subentry flow requested" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_hub_flow_aborts_without_entry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Add Hub flows without entry context should abort."""

    flow_manager = ConfigEntriesFlowManagerStub()
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=lambda _: None,
            flow=flow_manager.flow,
            flow_manager=flow_manager,
            async_progress=flow_manager.async_progress,
            async_progress_by_handler=flow_manager.async_progress_by_handler,
        )
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": "missing"}

    result = await flow.async_step_hub()
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_hub_subentry_flow_logs_and_delegates(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hub subentry handler should log and delegate to the base flow implementation."""

    caplog.set_level(logging.INFO)

    sentinel: dict[str, object] = {"type": "create_entry", "data": {}}

    async def _fake_async_step_user(self, user_input=None):  # type: ignore[unused-argument]
        return sentinel

    monkeypatch.setattr(
        config_flow._BaseSubentryFlow,  # type: ignore[attr-defined]
        "async_step_user",
        _fake_async_step_user,
        raising=False,
    )

    handler = object.__new__(config_flow.HubSubentryFlowHandler)
    handler.config_entry = make_config_entry(entry_id="entry-1")  # type: ignore[attr-defined]
    handler.subentry = None  # type: ignore[attr-defined]
    handler.hass = SimpleNamespace()  # type: ignore[assignment]

    result = await config_flow.HubSubentryFlowHandler.async_step_user(handler, None)
    if inspect.isawaitable(result):
        result = await result

    assert result is sentinel
    assert any(
        "Hub subentry flow requested" in record.getMessage()
        for record in caplog.records
    ), "Expected hub subentry flow to log when invoked"


@pytest.mark.asyncio
async def test_visibility_hub_selection_filters_ignored_entries() -> None:
    """The hub picker offers every entry except the ignored one.

    This pins the filter itself, not the way the module constant is resolved.
    Under an ordinary run the production module binds the *package attribute*
    ``homeassistant.config_entries``, which is the installed module rather than
    the ``sys.modules`` stub this suite installs, so ``SOURCE_IGNORE`` is
    present and the ``getattr`` default there never fires; turning the default
    back into a direct attribute access leaves this test green. The step had no
    test at all before, which is how a filter over ``SOURCE_*`` names stayed
    unexercised.
    """

    primary = make_config_entry(entry_id="hub-1", title="Primary", source="user")
    secondary = make_config_entry(entry_id="hub-2", title="Secondary", source="user")
    ignored = make_config_entry(entry_id="hub-3", title="Ignored", source="ignore")

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[SimpleNamespace]:
            assert domain == config_flow.DOMAIN
            return [primary, secondary, ignored]

    hass = HomeAssistant()
    hass.config_entries = _ConfigEntries()  # type: ignore[attr-defined]

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await flow.async_step_select_hub_for_visibility()

    assert result["step_id"] == "select_hub_for_visibility"
    marker = next(iter(result["data_schema"].schema))
    choices = result["data_schema"].schema[marker].container
    assert set(choices) == {"hub-1", "hub-2"}, (
        "The ignored entry must not be offered as a visibility hub"
    )


# --- B15: the flow-side resolver reads the type axis ------------------------
#
# Attribute form on purpose (``tests/AGENTS.md``, point 10): ``conftest.py``
# installs a synthetic ``homeassistant.config_entries`` in ``sys.modules``, so
# ``from homeassistant.config_entries import ConfigSubentry`` binds the *stub*,
# while ``import homeassistant.config_entries as ...`` reaches the real
# submodule through the package attribute.
#
# The *genuine* core class is what the scan doubles below want, because it is a
# frozen dataclass and the stub is not: a double that silently tolerated a
# write would make every assertion here weaker than it reads. This is
# explicitly **not** an argument about the ``isinstance`` branch -- that branch
# tests against whatever ``config_flow`` bound, which under the suite is the
# stub, so a double for *it* must be built from ``config_flow.ConfigSubentry``
# instead. The two needs pull in opposite directions and are served
# separately: this helper for the scan, and the double at
# ``test_the_flow_resolver_keeps_the_subentry_the_flow_manager_handed_over``
# for the hand-over branch. Do not unify them.
import homeassistant.config_entries as _ha_config_entries  # noqa: E402

_ConfigSubentry = _ha_config_entries.ConfigSubentry

# Assert the resolution rather than trusting the import form to have worked
# (``tests/AGENTS.md`` point 10, closing sentence): getting this wrong is not a
# red test, the guarantees simply become vacuous and stay green.
assert is_dataclass(_ConfigSubentry), (
    "the scan doubles must be built from the real core ConfigSubentry; "
    f"got {_ConfigSubentry!r}"
)
assert _ConfigSubentry.__dataclass_params__.frozen, (  # type: ignore[attr-defined]
    "the real core ConfigSubentry is frozen; a mutable binding means the "
    "stub was resolved and the doubles below prove less than they claim"
)


def _resolver_subentry(
    subentry_id: str, group_key: str, subentry_type: str | None
) -> object:
    """Build a subentry double for the resolver scan."""

    return _ConfigSubentry(
        data={"group_key": group_key, "visible_device_ids": ["dev-1"]},
        subentry_type=subentry_type,
        title=f"title-{subentry_id}",
        unique_id=f"uid-{subentry_id}",
        subentry_id=subentry_id,
    )


def _resolve_with(handler_cls: type, subentries: list[object]) -> str | None:
    handler = object.__new__(handler_cls)
    handler.config_entry = make_config_entry(  # type: ignore[attr-defined]
        entry_id="entry-resolver",
        subentries={s.subentry_id: s for s in subentries},  # type: ignore[attr-defined]
    )
    handler.subentry = None  # type: ignore[attr-defined]
    resolved = handler._resolve_existing()
    return None if resolved is None else resolved.subentry_id


@pytest.mark.parametrize(
    ("handler_name", "subentry_type", "group_key", "expected"),
    [
        ("HubSubentryFlowHandler", SUBENTRY_TYPE_TRACKER, "service", None),
        ("TrackerSubentryFlowHandler", SUBENTRY_TYPE_HUB, "core_tracking", None),
        ("HubSubentryFlowHandler", SUBENTRY_TYPE_SERVICE, "service", "id-only"),
        ("HubSubentryFlowHandler", None, "service", "id-only"),
        (
            "TrackerSubentryFlowHandler",
            SUBENTRY_TYPE_TRACKER,
            "core_tracking",
            "id-only",
        ),
        ("HubSubentryFlowHandler", SUBENTRY_TYPE_SERVICE, "core_tracking", None),
    ],
    ids=[
        "hub-refuses-parked-tracker",
        "tracker-refuses-hub-twin",
        "hub-keeps-real-service",
        "hub-keeps-untyped-legacy",
        "tracker-keeps-own-key-tracker",
        "hub-refuses-foreign-key",
    ],
)
def test_the_flow_resolver_judges_a_lone_candidate_on_both_axes(
    handler_name: str, subentry_type: str | None, group_key: str, expected: str | None
) -> None:
    """``_BaseSubentryFlow._resolve_existing`` must not resolve on the key alone.

    Measured against the real handlers before the axis existed: a
    ``tracker``-typed subentry storing the service key was resolved by
    ``HubSubentryFlowHandler`` and ``async_step_user`` then overwrote it with
    the id-less service payload, the service title and
    ``unique_id = "entry-resolver-service"`` -- the device assignment it held
    was dropped. The mirror direction handed a ``hub``-typed twin storing
    ``core_tracking`` to ``TrackerSubentryFlowHandler``.

    The three positive cases are the boundary, and they are what stops the
    axis from being a blanket refusal: a genuinely ``service``-typed subentry,
    an *untyped* legacy one (``_canonical_core_key_of`` declines to judge, so
    the stored key keeps deciding alone) and a tracker on its own key must all
    still resolve.

    The last case covers the *other* guard in the same loop: the axis is an
    additional condition beside the stored key, not a replacement for it, so a
    genuinely ``service``-typed subentry filed under ``core_tracking`` is
    refused by the key check before the type check is ever asked. Without it
    the key branch is a line no test in this suite executes.
    """

    subentry = _resolver_subentry("id-only", group_key, subentry_type)
    assert _resolve_with(getattr(config_flow, handler_name), [subentry]) == expected


@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
def test_the_flow_resolver_is_not_decided_by_subentry_order(reverse: bool) -> None:
    """A real service group beside a parked tracker wins in either order.

    Not a restatement of the case above: there the parked tracker is alone and
    the answer is ``None``. Here both answer on the key, so before the axis the
    winner was whichever ``entry.subentries`` yielded first -- the same
    load-order dependency the manager side removed in AP3, arriving through a
    different door. The scan returns on its first match, so this is the shape
    where a refusal that merely *skipped* would still have been wrong.
    """

    real = _resolver_subentry("id-real", "service", SUBENTRY_TYPE_SERVICE)
    parked = _resolver_subentry("id-parked", "service", SUBENTRY_TYPE_TRACKER)
    order = [parked, real] if reverse else [real, parked]

    assert _resolve_with(config_flow.HubSubentryFlowHandler, order) == "id-real"


def test_the_flow_resolver_keeps_the_subentry_the_flow_manager_handed_over() -> None:
    """The axis guards the scan, not the reconfigure hand-over.

    ``_resolve_existing`` returns ``self.subentry`` before scanning at all, and
    that object identifies the subentry the user opened. Filtering it on the
    type axis would make a mis-typed subentry unreconfigurable -- a fail-closed
    guard turned into a dead end.

    The branch is unreachable in production today, and that is stated rather
    than glossed: ``async_get_supported_subentry_types`` returns ``{}``
    unconditionally, so core never registers these handlers with its subentry
    flow manager, and ``async_step_hub``, the one entry point that builds a
    handler, passes no ``subentry``. It is left open deliberately, so this pins
    the asymmetry as intended rather than as an oversight: the same parked
    tracker that the scan refuses above is returned here, and it stays right
    the day core does hand a subentry over.
    """

    # Built from the class ``config_flow`` bound rather than from the core one
    # the scans above use, because this branch is an ``isinstance`` check and
    # the suite's ``conftest`` binds a stub there. In production the two are
    # the same object; under the suite they are not, and a double of the wrong
    # one would fall through to the scan and quietly pin the opposite.
    parked = config_flow.ConfigSubentry(
        data={"group_key": "service", "visible_device_ids": ["dev-1"]},
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="title-id-parked",
        unique_id="uid-id-parked",
        subentry_id="id-parked",
    )
    handler = object.__new__(config_flow.HubSubentryFlowHandler)
    handler.config_entry = make_config_entry(  # type: ignore[attr-defined]
        entry_id="entry-resolver",
        subentries={"id-parked": parked},
    )
    handler.subentry = parked  # type: ignore[attr-defined]

    assert isinstance(parked, config_flow.ConfigSubentry), (
        "the hand-over branch is an isinstance check against the class "
        "config_flow bound; a different binding would skip it silently and "
        "this test would pass through the scan instead"
    )
    resolved = handler._resolve_existing()
    assert resolved is parked
