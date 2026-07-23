# tests/test_cloud_discovery_trigger.py
"""Tests for the cloud discovery trigger helper."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy import config_flow, discovery
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.discovery import CloudDiscoveryOutcome
from tests.helpers import config_entry_with_cloud_runtime
from tests.helpers.config_flow import (
    config_entries_flow_stub,
    prepare_flow_hass_config_entries,
)

pytestmark = pytest.mark.asyncio

integration = importlib.import_module("custom_components.googlefindmy")

if TYPE_CHECKING:
    import pytest


def _make_hass(flow_result: dict[str, object] | None = None) -> SimpleNamespace:
    """Return a minimal hass stub suitable for discovery tests.

    ``flow_result`` is what ``hass.config_entries.flow.async_init`` answers. The
    default is a transient ``unknown`` abort, i.e. "a flow ran and imported
    nothing"; tests that exercise a *successful* creation pass their own.
    """

    hass = SimpleNamespace(data={})
    entry = config_entry_with_cloud_runtime()
    prepare_flow_hass_config_entries(
        hass,
        lambda: config_entries_flow_stub(
            result=flow_result
            if flow_result is not None
            else {
                "type": config_flow.data_entry_flow.FlowResultType.ABORT,
                "reason": "unknown",
            }
        ),
    )
    hass.config_entries.async_entries = lambda domain: (
        [entry] if domain == DOMAIN else []
    )
    hass.config_entries.async_get_entry = lambda entry_id: (
        entry if entry_id == entry.entry_id else None
    )
    hass._entry = entry
    return hass


async def test_trigger_cloud_discovery_uses_helper(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The helper should prefer async_create_discovery_flow when available."""

    hass = _make_hass()
    captured: list[tuple] = []

    async def _helper(*args, **kwargs):
        captured.append((args, kwargs))

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> CloudDiscoveryOutcome:
        return await integration._trigger_cloud_discovery(
            hass,
            email="User@Example.com",
            token="aas_et/TOKEN",
            secrets_bundle={"aas_token": "aas_et/TOKEN"},
        )

    caplog.set_level(logging.INFO, "custom_components.googlefindmy.discovery")
    assert await _exercise() is CloudDiscoveryOutcome.ACCEPTED
    assert hass.config_entries.flow.async_init.await_count == 0
    assert len(captured) == 1

    args, kwargs = captured[0]
    call_hass, domain = args
    context = kwargs.get("context", {})
    data = kwargs.get("data", {})
    discovery_key = kwargs.get("discovery_key")

    assert call_hass is hass

    assert domain == DOMAIN
    assert context["source"] == config_flow.SOURCE_DISCOVERY
    assert data["email"] == "user@example.com"
    assert data["token"] == "aas_et/TOKEN"
    assert data["secrets_bundle"] == {"aas_token": "aas_et/TOKEN"}
    assert data["discovery_ns"] == f"{DOMAIN}.cloud_scan"
    assert data["discovery_stable_key"] == "email:user@example.com"
    assert discovery_key is not None

    runtime = integration._cloud_discovery_runtime(hass, hass._entry)
    assert runtime.results, "discovery payload should be recorded"

    assert any(
        "use***@example.com" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "trigger log should redact identifiers"


async def test_trigger_cloud_discovery_sanitizes_context_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom discovery triggers should not leak into the context source."""

    hass = _make_hass()
    captured: list[tuple] = []

    async def _helper(*args, **kwargs):
        captured.append((args, kwargs))

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> CloudDiscoveryOutcome:
        return await integration._trigger_cloud_discovery(
            hass,
            email="User@Example.com",
            token="aas_et/TOKEN",
            secrets_bundle={"aas_token": "aas_et/TOKEN"},
            source="cloud_scanner",
        )

    assert await _exercise() is CloudDiscoveryOutcome.ACCEPTED
    assert len(captured) == 1

    _, kwargs = captured[0]
    context = kwargs.get("context", {})
    data = kwargs.get("data", {})

    assert context["source"] == config_flow.SOURCE_DISCOVERY
    assert data["discovery_source"] == "cloud_scanner"


async def test_trigger_cloud_discovery_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing helper should fall back to config_entries.flow.async_init.

    The redaction assertion is pinned to the INFO record on purpose: the success
    message is the one that names the account, and only a level-filtered
    assertion can show that *it* is redacted. Without the filter any DEBUG line
    mentioning the account satisfies it, which is how a regression that turned
    every successful creation into a transient RETRY (and thus silenced the INFO
    message altogether) passed unnoticed.
    """

    hass = _make_hass(
        # The flow was created and produced an entry, so the trigger reports
        # ACCEPTED and emits the INFO success message this test asserts on.
        {"type": config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY}
    )

    async def _helper(*args, **kwargs):
        raise AttributeError("missing helper")

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> CloudDiscoveryOutcome:
        return await integration._trigger_cloud_discovery(
            hass,
            email="fallback@example.com",
            token=None,
            secrets_bundle=None,
        )

    caplog.set_level(logging.INFO, "custom_components.googlefindmy.discovery")

    assert await _exercise() is CloudDiscoveryOutcome.ACCEPTED
    hass.config_entries.flow.async_init.assert_awaited_once()
    _, kwargs = hass.config_entries.flow.async_init.call_args
    assert kwargs["context"]["source"] == config_flow.SOURCE_DISCOVERY
    assert kwargs["data"]["email"] == "fallback@example.com"
    assert kwargs["data"]["discovery_ns"] == f"{DOMAIN}.cloud_scan"
    assert any(
        "fal***@example.com" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "fallback trigger log should redact identifiers"
    assert all(
        "fallback@example.com" not in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "fallback trigger log must not leak the raw address"


async def test_trigger_cloud_discovery_reports_a_transient_fallback_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same fallback path must still report RETRY for a transient abort.

    Guards the other half of the branch the test above pins to its INFO log:
    the level filter there is only meaningful because a flow that aborts
    transiently produces no INFO success message at all.
    """

    hass = _make_hass()

    async def _helper(*args, **kwargs):
        raise AttributeError("missing helper")

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    outcome = await integration._trigger_cloud_discovery(
        hass,
        email="fallback@example.com",
        token=None,
        secrets_bundle=None,
    )

    assert outcome is CloudDiscoveryOutcome.RETRY
    hass.config_entries.flow.async_init.assert_awaited_once()


async def test_trigger_cloud_discovery_injects_fallback_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DiscoveryKey failures should still provide a structured fallback."""

    hass = _make_hass()
    captured: dict[str, dict[str, object]] = {}

    async def _helper(*_args: object, **kwargs: object) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)
    monkeypatch.delattr(config_flow, "DiscoveryKey", raising=False)

    async def _exercise() -> CloudDiscoveryOutcome:
        return await integration._trigger_cloud_discovery(
            hass,
            email="fallback-key@example.com",
            token=None,
            secrets_bundle=None,
        )

    assert await _exercise() is CloudDiscoveryOutcome.ACCEPTED

    kwargs = captured["kwargs"]
    discovery_key = kwargs.get("discovery_key")
    assert discovery_key is not None

    assert getattr(discovery_key, "domain") == DOMAIN
    assert getattr(discovery_key, "namespace") == f"{DOMAIN}.cloud_scan"
    assert getattr(discovery_key, "stable_key") == "email:fallback-key@example.com"
    assert getattr(discovery_key, "key") == (
        f"{DOMAIN}.cloud_scan",
        "email:fallback-key@example.com",
    )


@pytest.mark.asyncio
async def test_async_create_discovery_flow_handles_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribute errors should fall back to a graceful abort."""

    hass = _make_hass()

    async def _helper(*args, **kwargs):
        raise AttributeError("missing helper attribute")

    monkeypatch.setattr(
        config_flow.config_entries,
        "async_create_discovery_flow",
        _helper,
        raising=False,
    )
    monkeypatch.setattr(config_flow, "_fallback_discovery_flow_helper", None)

    result = await config_flow.async_create_discovery_flow(
        hass,
        DOMAIN,
        context=None,
        data={},
    )

    assert result == {
        "type": config_flow.data_entry_flow.FlowResultType.ABORT,
        "reason": "unknown",
    }


@pytest.mark.asyncio
async def test_async_create_discovery_flow_treats_none_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning None should be treated as an already-in-progress abort."""

    hass = _make_hass()

    async def _helper(*args, **kwargs):
        return None

    monkeypatch.setattr(
        config_flow.config_entries,
        "async_create_discovery_flow",
        _helper,
        raising=False,
    )

    result = await config_flow.async_create_discovery_flow(
        hass,
        DOMAIN,
        context={"source": "discovery"},
        data={},
    )

    assert result == {
        "type": config_flow.data_entry_flow.FlowResultType.ABORT,
        "reason": "already_in_progress",
    }


# ---------------------------------------------------------------------------
# The fallback helper IS the production path (Codex P1)
# ---------------------------------------------------------------------------
# ``homeassistant.config_entries`` does not export ``async_create_discovery_flow``
# on current cores, so ``config_flow._discovery_flow_helper`` is None and every
# real discovery goes through the module-level fallback
# ``config_flow._async_create_discovery_flow`` -- the branch that used to carry a
# "legacy fallback" pragma. That branch ends in Home Assistant's
# ``helpers.discovery_flow.async_create_flow``, which is declared ``-> None``
# and dispatches the flow through ``async_create_background_task``. Its ``None``
# therefore means "a flow owns this payload", not "something failed", and
# synthesizing a transient abort for it classified *every successful discovery*
# as RETRY. A test that monkeypatches ``config_entries.async_create_discovery_flow``
# cannot see this: it exercises the proxy branch, which is dead in production.


async def test_fire_and_forget_flow_creation_is_accepted_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real fire-and-forget helper must yield ACCEPTED, never RETRY."""

    # Read CoreState off the helper module rather than from homeassistant.core:
    # the test suite installs a stripped core stub whose CoreState members are
    # plain strings, and the helper compares with ``is`` against its own.
    from homeassistant.helpers import discovery_flow as ha_discovery_flow

    # Grounding for the whole test: this is why the fallback is production.
    assert (
        getattr(config_flow.config_entries, "async_create_discovery_flow", None) is None
    ), (
        "Home Assistant re-introduced config_entries.async_create_discovery_flow; "
        "re-check which of the two synthesis sites in config_flow is the "
        "production path before trusting this test"
    )
    assert config_flow._fallback_discovery_flow_helper is not None

    hass = _make_hass({"type": config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY})
    # Surface Home Assistant's own async_create_flow reads. ``running`` keeps it
    # out of the start-up dispatcher queue so the flow is initialised right away.
    hass.state = ha_discovery_flow.CoreState.running
    hass.is_stopping = False
    hass.config_entries.flow.async_has_matching_discovery_flow = (
        lambda *_args, **_kwargs: False
    )

    background: list[asyncio.Task[object]] = []

    def _async_create_background_task(coro, name, *, eager_start=False):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro, name=name)
        background.append(task)
        return task

    hass.async_create_background_task = _async_create_background_task

    caplog.set_level(logging.INFO, "custom_components.googlefindmy.discovery")

    outcome = await integration._trigger_cloud_discovery(
        hass,
        email="fireforget@example.com",
        token="aas_et/FF",
        secrets_bundle=None,
    )

    # Nothing was monkeypatched below config_flow: the payload travelled through
    # the proxy, the module fallback and Home Assistant's real helper.
    assert outcome is CloudDiscoveryOutcome.ACCEPTED, (
        "a flow that was successfully handed to the fire-and-forget helper must "
        "not be reported as a transient failure"
    )

    # ... and a flow really was created, even though nothing was returned.
    assert background, "the helper should have dispatched a flow-init task"
    for task in background:
        await task
    hass.config_entries.flow.async_init.assert_awaited_once()

    assert any(
        "fir***@example.com" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "the success path should log the queued discovery with a redacted account"


async def test_trigger_cloud_discovery_deduplicates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Multiple discoveries with the same stable key should deduplicate flows."""

    hass = _make_hass()
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy")
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.discovery")

    gate = asyncio.Event()
    calls: list[dict] = []

    async def _helper(*args, **kwargs):
        calls.append(kwargs.get("data") or args[3])
        await gate.wait()

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> None:
        task = asyncio.create_task(
            integration._trigger_cloud_discovery(
                hass,
                email="dedup@example.com",
                token="aas_et/DUP",
            )
        )
        await asyncio.sleep(0)

        skipped = await integration._trigger_cloud_discovery(
            hass,
            email="dedup@example.com",
            token="aas_et/DUP",
        )
        assert skipped is CloudDiscoveryOutcome.SKIPPED
        assert any(
            "ded***@example.com" in record.getMessage() for record in caplog.records
        )
        assert all("aas_et/DUP" not in record.getMessage() for record in caplog.records)
        assert len(calls) == 1

        gate.set()
        assert await task is CloudDiscoveryOutcome.ACCEPTED

        gate.clear()
        gate.set()
        again = await integration._trigger_cloud_discovery(
            hass,
            email="dedup@example.com",
            token="aas_et/DUP",
        )
        assert again is CloudDiscoveryOutcome.ACCEPTED
        assert len(calls) == 2

    await _exercise()


async def test_trigger_cloud_discovery_admits_changed_payload_for_same_account(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A same-account bundle with *changed content* must not dedup as SKIPPED.

    Regression for the coarse, account-keyed in-flight guard (Codex P2,
    discovery.py active_keys): the same account resolves to one
    ``stable_key`` (``email:<addr>``), so keying the guard on it dropped a
    refreshed bundle (new token/digest) that arrived while the first flow was
    still active, leaving the fresh credentials stalled. The guard is
    content-aware now, so the changed payload reaches a real flow. Identical
    content still deduplicates -- proven by
    ``test_trigger_cloud_discovery_deduplicates``.
    """

    hass = _make_hass()
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.discovery")

    inflight = asyncio.Event()
    calls: list[str | None] = []

    async def _helper(*args, **kwargs):
        data = kwargs.get("data") or args[3]
        token = data.get("token")
        calls.append(token)
        if token == "aas_et/OLD":
            # Keep the first account flow in flight while the second arrives.
            await inflight.wait()

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> None:
        first = asyncio.create_task(
            integration._trigger_cloud_discovery(
                hass,
                email="same@example.com",
                token="aas_et/OLD",
            )
        )
        await asyncio.sleep(0)

        # Same account (identical stable_key), different content, arriving while
        # the first flow is still active. Account-coarse dedup returned SKIPPED
        # here; the content-aware guard admits it.
        second = await integration._trigger_cloud_discovery(
            hass,
            email="same@example.com",
            token="aas_et/NEW",
        )

        assert second is not CloudDiscoveryOutcome.SKIPPED
        # The strongest proof: the changed payload actually reached the flow
        # helper. The buggy account-keyed guard would have stopped at one call.
        assert calls == ["aas_et/OLD", "aas_et/NEW"]
        # Neither raw token may leak into logs.
        assert all("aas_et/" not in record.getMessage() for record in caplog.records)

        inflight.set()
        assert await first is CloudDiscoveryOutcome.ACCEPTED

    await _exercise()


async def test_results_append_triggers_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending to the results list should schedule a discovery flow."""

    hass = _make_hass()
    scheduled: list[Awaitable[bool]] = []

    def _async_create_task(coro):  # type: ignore[no-untyped-def]
        scheduled.append(coro)
        return coro

    hass.async_create_task = _async_create_task  # type: ignore[attr-defined]

    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(config_flow, "async_create_discovery_flow", helper)

    runtime = integration._cloud_discovery_runtime(hass, hass._entry)
    results = runtime.results
    results.append({"email": "append@example.com", "token": "aas_et/APP"})
    assert scheduled, "append should schedule a discovery coroutine"

    async def _drain() -> None:
        for task in scheduled:
            await task

    await _drain()
    helper.assert_awaited_once()
    assert hass.config_entries.flow.async_init.await_count == 0


async def test_results_append_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Appending duplicate payloads should only launch one flow at a time."""

    hass = _make_hass()
    calls: list[dict] = []

    gate_holder = [asyncio.Event()]

    async def _helper(*args, **kwargs):
        calls.append(kwargs.get("data") or args[3])
        await gate_holder[0].wait()

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    scheduled: list[asyncio.Task] = []

    def _async_create_task(coro):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass.async_create_task = _async_create_task  # type: ignore[attr-defined]

    stable_key = discovery._cloud_discovery_stable_key(
        "dedup@example.com",
        "aas_et/DUP",
        {"oauth_token": "aas_et/DUP"},
    )
    payload = discovery._assemble_cloud_discovery_payload(
        email="dedup@example.com",
        token="aas_et/DUP",
        secrets_bundle={"oauth_token": "aas_et/DUP"},
        discovery_ns=discovery.CLOUD_DISCOVERY_NAMESPACE,
        discovery_stable_key=stable_key,
        title=None,
        source=None,
    )

    async def _exercise() -> None:
        results = integration._cloud_discovery_runtime(hass, hass._entry).results

        results.append(payload)
        results.append(payload)

        await asyncio.sleep(0)
        assert len(calls) == 1

        gate_holder[0].set()
        await asyncio.sleep(0)

        gate_holder[0] = asyncio.Event()

        results.append(payload)
        await asyncio.sleep(0)
        assert len(calls) == 2

        gate_holder[0].set()
        await asyncio.sleep(0)

        for task in scheduled:
            await task

    await _exercise()
