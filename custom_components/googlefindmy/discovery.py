# custom_components/googlefindmy/discovery.py
"""Discovery runtime helpers for the Google Find My Device integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

try:  # pragma: no cover - stripped test environments may lack CALLBACK_TYPE
    from homeassistant.core import CALLBACK_TYPE
except ImportError:  # pragma: no cover - default to a local alias when absent
    CALLBACK_TYPE = Callable[[], None]

try:  # pragma: no cover - stripped test envs may not include translations helper
    from homeassistant.helpers import translation
except ImportError:  # pragma: no cover - provide a minimal fallback for tests

    class _TranslationFallback:
        LOCALE_EN = "en"

        @staticmethod
        async def async_get_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {}

    translation = cast(Any, _TranslationFallback())

try:  # pragma: no cover - stripped test envs may not provide event helpers
    from homeassistant.helpers.event import async_track_time_interval
except ImportError:  # pragma: no cover - provide a minimal fallback for tests

    def async_track_time_interval(*_args: Any, **_kwargs: Any) -> CALLBACK_TYPE:
        return lambda: None


from . import config_flow as config_flow_module
from .const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    SECRETS_EXTRA_WATCH_PATHS,
)
from .email_utils import normalize_email
from .ha_typing import CloudDiscoveryRuntime, callback
from .shared_helpers import normalize_secrets_bundle

cf = cast(Any, config_flow_module)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_DISCOVERY_SOURCE: str = getattr(cf, "SOURCE_DISCOVERY", "discovery")


def _home_assistant_discovery_sources() -> set[str]:
    """Return the set of discovery sources supported by Home Assistant."""

    cached: set[str] | None = getattr(_home_assistant_discovery_sources, "_cache", None)
    if cached is not None:
        return cached

    sources: set[str] = set()

    modules_to_inspect: list[Any] = []

    config_entries_module = getattr(cf, "config_entries", None)
    if config_entries_module is not None:
        modules_to_inspect.append(config_entries_module)

    try:  # pragma: no cover - optional in stripped test envs
        from homeassistant import config_entries as ha_config_entries
    except Exception:  # noqa: BLE001 - absence is acceptable in tests
        ha_config_entries = None
    if ha_config_entries is not None:
        modules_to_inspect.append(ha_config_entries)

    for module in modules_to_inspect:
        try:
            attributes = dir(module)
        except Exception:  # noqa: BLE001 - defensive fallback
            continue
        for name in attributes:
            if not name.startswith("SOURCE_"):
                continue
            value = getattr(module, name, None)
            if isinstance(value, str) and value:
                sources.add(value)

    if not sources:
        for attr in (
            "SOURCE_DISCOVERY",
            "SOURCE_RECONFIGURE",
        ):
            fallback = getattr(cf, attr, None)
            if isinstance(fallback, str) and fallback:
                sources.add(fallback)
        discovery_update_source = getattr(
            cf,
            "DISCOVERY_UPDATE_SOURCE",
            "discovery_update_info",
        )
        if isinstance(discovery_update_source, str) and discovery_update_source:
            sources.add(discovery_update_source)

    setattr(_home_assistant_discovery_sources, "_cache", sources)
    return sources


def _invoke_failure_hook(on_failure: Callable[[], None] | None) -> None:
    """Run a discovery failure hook without letting it break its caller.

    The hook is a plain synchronous callback owned by the *producer* of the
    discovery payload (see :meth:`SecretsJSONWatcher._invalidate_signature`).
    It runs either from a task done-callback or straight from the scheduling
    call, so it must never raise into the event loop.
    """

    if on_failure is None:
        return
    try:
        on_failure()
    except Exception:  # noqa: BLE001 - defensive best effort
        _LOGGER.debug("Discovery failure hook raised", exc_info=True)


def _hass_is_stopping(hass: Any) -> bool:
    """Return ``True`` only when Home Assistant itself is shutting down.

    Reads :attr:`homeassistant.core.HomeAssistant.is_stopping` defensively:
    stripped cores and test doubles may not expose the attribute at all, and a
    missing/odd value must never raise out of a done-callback. An *unknown*
    state deliberately resolves to ``False`` (= "not a shutdown"), because the
    two error costs are asymmetric: reading a live cancel as shutdown swallows
    the retry for good, while reading a shutdown cancel as failure only re-arms
    a watcher that is about to be discarded anyway.
    """

    return getattr(hass, "is_stopping", False) is True


class CloudDiscoveryOutcome(StrEnum):
    """What a cloud discovery trigger achieved, seen from the *producer*.

    The three members answer exactly one question -- "must the producer re-arm
    its change detection?" -- and nothing else. They exist because the plain
    "did the coroutine raise?" signal cannot answer it: a config flow reports a
    refused import through its :class:`FlowResult`, not through an exception, so
    a flow that aborted with ``cannot_connect`` looks byte-for-byte like a
    successful import to a done-callback that only inspects the exception.

    * ``ACCEPTED`` -- a flow was created or was already running for this
      payload, and it ended in a state that no retry can improve. Nothing to
      re-arm.
    * ``SKIPPED`` -- the request never reached a flow because this integration
      deduplicated it (``runtime.active_keys``) or the runtime container was
      torn down mid-unload. Re-arming would schedule a second attempt for work
      that is already running, so this deliberately does not re-arm either.
    * ``RETRY`` -- a flow ran and aborted for a *transient* reason. The bundle
      was not imported and the next scan should try again.
    """

    ACCEPTED = "accepted"
    SKIPPED = "skipped"
    RETRY = "retry"


#: ``FlowResultType.ABORT`` as its wire value. Compared as a string on purpose:
#: ``homeassistant.data_entry_flow.FlowResultType`` is a ``StrEnum``, and
#: stripped test cores hand out plain strings, so both forms have to match.
_ABORT_FLOW_RESULT_TYPE = "abort"

#: Abort reasons that end a discovery flow in a state a retry cannot improve.
#:
#: Every entry is a *legitimate* end state, not a failure, and re-arming on it
#: would burn the bounded retry budget (:data:`_MAX_SECRETS_RETRY_ATTEMPTS`) and
#: recreate work at every scan interval:
#:
#: * ``already_configured`` -- Home Assistant's own unique-id guard
#:   (``ConfigEntries._abort_if_unique_id_configured``) *and* the way this
#:   integration's discovery-update path signals **success**: it applies the
#:   updated credentials to the existing entry and then aborts with this reason
#:   (see ``config_flow.ConfigFlow.async_step_discovery_update_info``). Treating
#:   it as a failure would re-import an already imported bundle on every scan.
#: * ``already_in_progress`` -- ``ConfigEntries`` raises it for a flow that is
#:   already running, and *both* synthesis sites in ``config_flow`` produce it
#:   when Home Assistant's fire-and-forget helper answers with ``None``: the
#:   proxy ``config_flow.async_create_discovery_flow`` (used when
#:   ``homeassistant.config_entries`` exports ``async_create_discovery_flow``)
#:   and the module-level fallback ``config_flow._async_create_discovery_flow``
#:   (used when it does not, which is the case on current cores and therefore
#:   the ordinary production path). ``helpers.discovery_flow.async_create_flow``
#:   is declared ``-> None`` and dispatches through
#:   ``async_create_background_task``, so ``None`` means "a flow owns this
#:   payload, its result is not observable from the caller" -- the normal
#:   production outcome, not a failure.
#: * ``single_instance_allowed`` -- ``ConfigEntriesFlowManager.async_finish_flow``
#:   refuses a second entry for a single-instance integration. Permanent.
#: * ``not_implemented`` -- ``ConfigFlow.async_step_*`` default. Permanent.
#: * ``invalid_discovery_info`` -- this integration rejects the *payload* after
#:   validating it. The next scan would submit the identical payload and be
#:   rejected identically.
#: * ``keys_missing`` -- same class as ``invalid_discovery_info``, one validation
#:   rule further in: ``config_flow._normalize_and_validate_discovery_payload``
#:   raises it for a bundle without a usable ``shared_key``, and
#:   ``async_step_discovery`` forwards it verbatim as ``async_abort(reason=
#:   err.reason)``. A bundle without a shared key is a non-renewable dead end
#:   (the owner key can only be refreshed *with* the shared key), so the next
#:   scan submits the identical bytes and is rejected identically.
#: * ``reauth_successful`` / ``reconfigure_successful`` / ``migration_successful``
#:   -- success reasons of the update paths, which finish by aborting on purpose.
#:
#: Deliberately **absent**, i.e. treated as transient, are the remaining reasons
#: the discovery path can produce:
#:
#: * ``invalid_auth`` -- tempting to call terminal, but it is a *server verdict*,
#:   not a payload property: ``config_flow._map_api_exc_to_error_key`` derives it
#:   from an auth-shaped exception or a plain HTTP 401/403, and Google returns
#:   those for rate limiting and temporary blocks as well as for genuinely dead
#:   credentials. The error costs are asymmetric -- classifying it terminal by
#:   mistake means never retrying a bundle that would have worked, while
#:   classifying it transient by mistake costs the bounded retry budget and one
#:   warning -- so the uncertain case belongs on the transient side. Contrast
#:   ``keys_missing``: that one is decided by the bytes we hold, not by a peer.
#: * ``cannot_connect`` -- network failure, and it doubles as the "payload
#:   carried no usable token candidate" rejection, so it cannot be made terminal
#:   without silencing genuine network retries.
#: * ``dependency_not_ready`` -- a missing runtime dependency can become
#:   available.
#: * ``unknown`` -- an unclassified exception, which is exactly the case the
#:   bounded retry budget exists for.
#:
#: Anything **not** listed here counts as transient (see
#: :func:`_classify_discovery_flow_result` for why the default points that way).
_TERMINAL_ABORT_REASONS: frozenset[str] = frozenset(
    {
        "already_configured",
        "already_in_progress",
        "single_instance_allowed",
        "not_implemented",
        "invalid_discovery_info",
        "keys_missing",
        "reauth_successful",
        "reconfigure_successful",
        "migration_successful",
    }
)


def _flow_result_field(result: Mapping[str, Any], field_name: str) -> str:
    """Return ``field_name`` of a FlowResult as plain text (``""`` if absent)."""

    raw = result.get(field_name)
    # ``StrEnum`` members compare equal to their value, but a plain ``Enum`` in a
    # stripped test core does not, so unwrap ``.value`` before the comparison.
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else ""


def _classify_discovery_flow_result(result: Any) -> CloudDiscoveryOutcome:
    """Map the FlowResult of a discovery flow onto a producer-facing outcome.

    This is the whole point of the fix for "a completed coroutine is not a
    completed import": ``ConfigEntries.flow.async_init`` and
    ``config_flow.async_create_discovery_flow`` both *return* an abort instead
    of raising one, so the outcome has to be read off the returned value.

    Unknown abort reasons resolve to :attr:`CloudDiscoveryOutcome.RETRY`, and
    that direction is chosen deliberately, from the asymmetry of the two error
    costs (the same asymmetry :func:`_hass_is_stopping` argues from):

    * Reading a *terminal* reason as transient costs at most
      :data:`_MAX_SECRETS_RETRY_ATTEMPTS` extra scans, after which the watcher
      gives up on the bundle with one warning. Bounded, and self-healing.
    * Reading a *transient* reason as terminal stalls the import indefinitely --
      until the bundle changes, the watch paths change or Home Assistant
      restarts. Unbounded, and silent.

    So the terminal set is enumerated explicitly (it is small, stable and
    grounded in Home Assistant's own sources plus this integration's
    ``strings.json``) while the open-ended remainder takes the recoverable
    branch.

    A result that is not a readable FlowResult mapping, or one that is not an
    abort at all, is ``ACCEPTED``: only a *recognised* abort is evidence that
    the import did not happen, and a missing/foreign shape is no such evidence.
    """

    if not isinstance(result, Mapping):
        return CloudDiscoveryOutcome.ACCEPTED
    if _flow_result_field(result, "type") != _ABORT_FLOW_RESULT_TYPE:
        return CloudDiscoveryOutcome.ACCEPTED
    reason = _flow_result_field(result, "reason")
    if reason in _TERMINAL_ABORT_REASONS:
        return CloudDiscoveryOutcome.ACCEPTED
    _LOGGER.debug(
        "Cloud discovery flow aborted with a transient reason (%s); "
        "the payload was not imported",
        reason or "<unknown>",
    )
    return CloudDiscoveryOutcome.RETRY


def _handle_discovery_task_result(
    task: asyncio.Future[Any],
    *,
    hass: Any = None,
    on_failure: Callable[[], None] | None = None,
) -> None:
    """Consume a cloud discovery task result and report failures upstream.

    Discovery tasks are fire-and-forget, so exceptions are logged and
    suppressed here. When ``on_failure`` is supplied the producer additionally
    learns that the attempt did *not* succeed and can re-arm whatever change
    detection gated the task, otherwise a transient flow-creation error would
    stall the import until the source file changes again.

    A task that finished **without** raising is not proof that the payload was
    imported. A config flow that hits a transient error handles it and returns
    ``FlowResultType.ABORT``; the coroutine then completes normally and would,
    if only the exception were inspected, be indistinguishable from a
    successful import -- the failure hook would never fire, the watcher's
    signature would stay armed and the documented bounded retries would never
    happen. The outcome is therefore read off the returned
    :class:`CloudDiscoveryOutcome`, and only
    :attr:`CloudDiscoveryOutcome.RETRY` re-arms the producer. Any other value,
    including a legacy ``bool`` or ``None`` from a test double, is treated as
    "no retry warranted", which is the pre-existing behaviour for everything
    that is not a recognised transient abort.

    ``asyncio.CancelledError`` counts as a failure *unless* Home Assistant is
    actually shutting down. Cancellation is not a reliable shutdown signal
    here: ``_cleanup_cloud_discovery_runtime`` cancels exactly these handles on
    every entry unload, and an unload happens on every reload (an options
    change, a reauth, a repair). The producing watcher is a Home Assistant
    instance singleton that survives such a reload, so treating a reload-cancel
    as "no failure" would leave its change detection armed on a bundle that was
    never imported -- the precise stall this feedback channel exists to
    prevent. Only a real shutdown makes re-arming pointless.
    """

    try:
        outcome = task.result()
    except asyncio.CancelledError:
        if _hass_is_stopping(hass):
            return
        _LOGGER.debug(
            "Cloud discovery task cancelled while Home Assistant keeps running "
            "(entry unload/reload); treating the attempt as failed"
        )
    except Exception as err:  # noqa: BLE001 - logging best effort
        _LOGGER.debug("Suppressed cloud discovery task exception: %s", err)
    else:
        if outcome is not CloudDiscoveryOutcome.RETRY:
            return
        _LOGGER.debug(
            "Cloud discovery flow ended in a transient abort; re-arming the "
            "producer so the bundle is retried"
        )

    _invoke_failure_hook(on_failure)


CLOUD_DISCOVERY_NAMESPACE = f"{DOMAIN}.cloud_scan"
SECRETS_DISCOVERY_NAMESPACE = f"{DOMAIN}.secrets_file"
_DEFAULT_SECRETS_SCAN_INTERVAL = timedelta(seconds=30)

#: How often a *single* secrets bundle may be re-queued after a failed import
#: attempt before the watcher stops re-arming it. The budget exists because a
#: retry is not free: every attempt appends another copy of the bundle (OAuth
#: token included) to ``runtime.results`` and can emit another warning, so an
#: endlessly failing bundle would grow memory and flood the log at scan rate.
#: Four attempts spread over ~90 s cover the transient causes this feedback
#: channel was built for (startup races, a config-flow backend that is not
#: ready yet) without turning a permanent failure into a permanent leak.
_MAX_SECRETS_RETRY_ATTEMPTS = 3


class _CloudDiscoveryResults(list[dict[str, Any]]):
    """Results container that triggers config flows on append."""

    __slots__ = ("_entry", "_hass", "_runtime")

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | None = None,
        *,
        runtime: CloudDiscoveryRuntime | None = None,
    ) -> None:
        super().__init__()
        self._hass = hass
        self._entry = entry
        self._runtime = runtime

    def bind(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | None,
        runtime: CloudDiscoveryRuntime,
    ) -> None:
        """Rebind the backing hass, entry, and runtime references."""

        self._hass = hass
        self._entry = entry
        self._runtime = runtime

    def append(
        self,
        item: Mapping[str, Any],
        *,
        trigger: bool = True,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        """Record a discovery payload and (optionally) queue the config flow.

        ``on_failure`` is an opaque callback supplied by the producer of the
        payload. This container stays deliberately ignorant of *what* it means
        (it knows nothing about watchers or file signatures).

        The promise is narrower than "the attempt did not succeed": the hook
        runs when the queued coroutine **raised**, when it was cancelled
        outside a Home Assistant shutdown, when its outcome cannot be observed
        at all (no usable done-callback, no running loop), or when the flow ran
        and ended in a *transient* abort
        (:attr:`CloudDiscoveryOutcome.RETRY`). A
        :attr:`CloudDiscoveryOutcome.SKIPPED` result from
        :func:`_trigger_cloud_discovery` deliberately does **not** run it. Every
        ``SKIPPED`` path there is a *deduplication* path -- an identical flow is
        already in flight (``runtime.active_keys``) or the runtime container was
        torn down mid-unload -- so re-arming the producer would schedule a
        second attempt for work that is already running and recreate exactly the
        duplicate flows the dedup exists to suppress.
        """

        payload = dict(item)
        super().append(payload)
        if not trigger:
            return

        email = payload.get("email") or payload.get(CONF_GOOGLE_EMAIL)
        token = payload.get("token") or payload.get(CONF_OAUTH_TOKEN)
        secrets_raw = payload.get("secrets_bundle") or payload.get(DATA_SECRET_BUNDLE)
        secrets_bundle = secrets_raw if isinstance(secrets_raw, Mapping) else None
        discovery_ns = payload.get("discovery_ns")
        stable_key = payload.get("discovery_stable_key")
        source = payload.get("discovery_source")
        title = payload.get("title")

        coro = _trigger_cloud_discovery(
            self._hass,
            email=email if isinstance(email, str) else None,
            token=token if isinstance(token, str) else None,
            secrets_bundle=secrets_bundle,
            discovery_ns=discovery_ns if isinstance(discovery_ns, str) else None,
            discovery_stable_key=stable_key if isinstance(stable_key, str) else None,
            source=source if isinstance(source, str) else None,
            title=title if isinstance(title, str) else None,
            entry=self._entry,
        )
        self._schedule(coro, on_failure=on_failure)

    def _schedule(
        self,
        coro: Coroutine[Any, Any, object],
        *,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        """Queue ``coro`` and route its outcome to ``on_failure``.

        Every branch that cannot observe the outcome (no done-callback support,
        no running loop) counts as a failure: the attempt is then either lost or
        unverifiable, and re-arming the producer costs one extra scan while not
        re-arming stalls the import indefinitely.
        """

        done_callback = partial(
            _handle_discovery_task_result,
            hass=self._hass,
            on_failure=on_failure,
        )
        create_task = getattr(self._hass, "async_create_task", None)
        if callable(create_task):
            try:
                task = create_task(
                    coro,
                    name="googlefindmy.cloud_discovery",
                )
            except TypeError:
                task = create_task(coro)
            if isinstance(task, asyncio.Future):
                self._register_handle(task)
                task.add_done_callback(done_callback)
                return
            if hasattr(task, "add_done_callback"):
                try:
                    task.add_done_callback(done_callback)
                except Exception:  # noqa: BLE001 - defensive best effort
                    _LOGGER.debug(
                        "Unable to attach discovery task callback", exc_info=True
                    )
                    _invoke_failure_hook(on_failure)
                return
            _LOGGER.debug(
                "Discovery task handle does not support done callbacks; "
                "treating the attempt as unverified"
            )
            _invoke_failure_hook(on_failure)
            return

        try:
            task = asyncio.create_task(cast(Coroutine[Any, Any, Any], coro))
        except RuntimeError:
            _LOGGER.debug(
                "Cloud discovery append scheduling skipped: event loop not running"
            )
            _invoke_failure_hook(on_failure)
            return

        self._register_handle(task)
        task.add_done_callback(done_callback)

    def _register_handle(self, handle: asyncio.Future[Any]) -> None:
        if self._runtime is None:
            return
        try:
            self._runtime.retry_handles.add(handle)
        except Exception:  # noqa: BLE001 - defensive best effort
            _LOGGER.debug("Unable to record discovery task handle", exc_info=True)
            return

        try:
            handle.add_done_callback(self._runtime.retry_handles.discard)
        except Exception:  # noqa: BLE001 - defensive best effort
            _LOGGER.debug("Unable to attach discovery handle cleanup", exc_info=True)


def _cloud_discovery_runtime(
    hass: HomeAssistant, entry: ConfigEntry | None = None
) -> CloudDiscoveryRuntime:
    """Return the mutable runtime bucket tracking cloud discovery state."""

    runtime_owner: Any | None = None
    runtime_entry: ConfigEntry | None = entry
    if runtime_entry is not None:
        runtime_owner = getattr(runtime_entry, "runtime_data", None)
    else:
        manager = getattr(hass, "config_entries", None)
        async_entries = getattr(manager, "async_entries", None)
        if callable(async_entries):
            try:
                for candidate in async_entries(DOMAIN):
                    runtime_candidate = getattr(candidate, "runtime_data", None)
                    if runtime_candidate is None:
                        continue
                    runtime_entry = candidate
                    runtime_owner = runtime_candidate
                    break
            except Exception:  # noqa: BLE001 - defensive best effort
                _LOGGER.debug("Cloud discovery runtime lookup failed", exc_info=True)

    if runtime_owner is None:
        # hass.data[DOMAIN] is compatible with HassKey-based DATA_DOMAIN in __init__.
        domain_data = hass.data.setdefault(DOMAIN, {})
        runtime_owner = domain_data.get("cloud_discovery_runtime_owner")
        if not isinstance(runtime_owner, SimpleNamespace):
            runtime_owner = SimpleNamespace()
            domain_data["cloud_discovery_runtime_owner"] = runtime_owner

    runtime = getattr(runtime_owner, "cloud_discovery", None)
    if not isinstance(runtime, CloudDiscoveryRuntime):
        runtime = CloudDiscoveryRuntime()
        setattr(runtime_owner, "cloud_discovery", runtime)

    if not isinstance(runtime.lock, asyncio.Lock):
        runtime.lock = asyncio.Lock()

    if not isinstance(runtime.active_keys, set):
        runtime.active_keys = set()

    results = runtime.results
    if isinstance(results, _CloudDiscoveryResults):
        results.bind(hass, runtime_entry, runtime)
    else:
        replacement = _CloudDiscoveryResults(hass, runtime_entry, runtime=runtime)
        if isinstance(results, list):
            replacement.extend(results)
        runtime.results = replacement

    return runtime


def _cloud_discovery_stable_key(
    email: str | None,
    token: str | None,
    secrets_bundle: Mapping[str, Any] | None,
) -> str:
    """Generate a stable identifier used to deduplicate discovery flows."""

    normalized_email = normalize_email(email if isinstance(email, str) else None)
    if not normalized_email and isinstance(secrets_bundle, Mapping):
        for key in ("google_email", "email", "username", "Email"):
            value = secrets_bundle.get(key)
            if isinstance(value, str) and value:
                normalized_email = normalize_email(value)
                if normalized_email:
                    break

    if normalized_email:
        return f"email:{normalized_email}"

    candidate_token: str | None = token if isinstance(token, str) and token else None
    if not candidate_token and isinstance(secrets_bundle, Mapping):
        for key in ("aas_token", "oauth_token", "token"):
            value = secrets_bundle.get(key)
            if isinstance(value, str) and value:
                candidate_token = value
                break

    if candidate_token:
        digest = hashlib.sha256(candidate_token.encode("utf-8")).hexdigest()
        return f"token:{digest[:16]}"

    return f"anonymous:{uuid.uuid4().hex[:12]}"


def _redact_account_for_log(email: str | None, stable_key: str) -> str:
    """Return a partially redacted account identifier safe for logging."""

    normalized = normalize_email(email if isinstance(email, str) else None)
    if normalized:
        local_part, _, domain = normalized.partition("@")
        if local_part:
            prefix = local_part[:3] if len(local_part) >= 3 else local_part[:1]
            redacted_local = f"{prefix}***"
        else:
            redacted_local = "***"
        return f"{redacted_local}@{domain}" if domain else redacted_local

    if stable_key.startswith("token:"):
        return f"{stable_key[:10]}…"

    if stable_key.startswith("anonymous:"):
        return stable_key

    return f"{stable_key[:12]}…" if len(stable_key) > 12 else stable_key


def _assemble_cloud_discovery_payload(
    *,
    email: str | None,
    token: str | None,
    secrets_bundle: Mapping[str, Any] | None,
    discovery_ns: str,
    discovery_stable_key: str,
    title: str | None,
    source: str | None,
) -> dict[str, Any]:
    """Prepare the payload forwarded to the config flow discovery handler."""

    clean_email = normalize_email(email if isinstance(email, str) else None)
    payload: dict[str, Any] = {
        "email": clean_email,
        CONF_GOOGLE_EMAIL: clean_email,
        "discovery_ns": discovery_ns,
        "discovery_stable_key": discovery_stable_key,
    }

    if isinstance(token, str) and token:
        payload["token"] = token
        payload[CONF_OAUTH_TOKEN] = token

    if secrets_bundle is not None:
        secrets_copy = dict(secrets_bundle)
        payload["secrets_bundle"] = secrets_copy
        payload[DATA_SECRET_BUNDLE] = secrets_copy

    if title:
        payload["title"] = title

    if source:
        payload["discovery_source"] = source

    return payload


@dataclass(slots=True)
class _DiscoveryKeyCandidate:
    """Fallback discovery-key representation when helpers are unavailable."""

    domain: str
    namespace: str
    stable_key: str
    version: int = 1
    key: tuple[str, str] = field(init=False)

    def __post_init__(self) -> None:
        """Populate the combined key tuple for helper compatibility."""

        object.__setattr__(self, "key", (self.namespace, self.stable_key))


async def _trigger_cloud_discovery(
    hass: HomeAssistant,
    *,
    email: str | None,
    token: str | None,
    secrets_bundle: Mapping[str, Any] | None = None,
    discovery_ns: str | None = None,
    discovery_stable_key: str | None = None,
    source: str | None = None,
    title: str | None = None,
    entry: ConfigEntry | None = None,
) -> CloudDiscoveryOutcome:
    """Create or resume a config flow based on cloud-scan discovery data.

    Returns a :class:`CloudDiscoveryOutcome` rather than a bare ``bool``,
    because the two "nothing was imported" cases must stay distinguishable:
    a locally deduplicated request (``SKIPPED``) must not re-arm the producer,
    while a flow that ran and aborted transiently (``RETRY``) must.
    """

    runtime = _cloud_discovery_runtime(hass, entry)
    ns = discovery_ns or CLOUD_DISCOVERY_NAMESPACE
    secrets_copy = dict(secrets_bundle) if isinstance(secrets_bundle, Mapping) else None
    stable_key = discovery_stable_key or _cloud_discovery_stable_key(
        email,
        token,
        secrets_copy,
    )

    payload = _assemble_cloud_discovery_payload(
        email=email,
        token=token,
        secrets_bundle=secrets_copy,
        discovery_ns=ns,
        discovery_stable_key=stable_key,
        title=title,
        source=source,
    )

    lock = runtime.lock
    async with lock:
        results_list = runtime.results
        if not isinstance(results_list, _CloudDiscoveryResults):
            # Defensive, and not reachable through an entry unload: that path
            # cancels the registered handles *before* it clears ``results``, so
            # a coroutine suspended on a contended ``lock`` wakes up with
            # ``CancelledError`` and never gets here. What can get here is a
            # producer whose task is not registered in ``retry_handles`` (the
            # cloud scanner), and that one passes no failure hook anyway, so
            # returning ``SKIPPED`` loses nothing.
            return CloudDiscoveryOutcome.SKIPPED

        results_list.append(payload, trigger=False)
        if stable_key in runtime.active_keys:
            _LOGGER.debug(
                "Cloud discovery request deduplicated for %s (flow already active)",
                _redact_account_for_log(email, stable_key),
            )
            return CloudDiscoveryOutcome.SKIPPED
        runtime.active_keys.add(stable_key)

    triggered = False
    outcome = CloudDiscoveryOutcome.ACCEPTED
    try:
        helper = getattr(cf, "async_create_discovery_flow", None)
        try:
            discovery_key = cf.DiscoveryKey(domain=DOMAIN, key=(ns, stable_key))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("DiscoveryKey instantiation failed (%s); using fallback", err)
            discovery_key = _DiscoveryKeyCandidate(
                domain=DOMAIN,
                namespace=ns,
                stable_key=stable_key,
            )

        supported_sources = _home_assistant_discovery_sources()
        use_candidate = isinstance(source, str) and source in supported_sources
        context_source = source if use_candidate else _DEFAULT_DISCOVERY_SOURCE
        context = {"source": context_source}

        if callable(helper):
            try:
                outcome = _classify_discovery_flow_result(
                    await helper(
                        hass,
                        DOMAIN,
                        context=context,
                        data=payload,
                        discovery_key=discovery_key,
                    )
                )
                triggered = True
            except (AttributeError, NotImplementedError) as err:
                _LOGGER.debug(
                    "Discovery helper unavailable (%s); falling back to async_init",
                    err,
                )
            except Exception as err:  # noqa: BLE001 - surface unexpected errors
                _LOGGER.warning(
                    "Cloud discovery flow creation failed for %s: %s",
                    _redact_account_for_log(email, stable_key),
                    err,
                )
                raise

        if not triggered:
            outcome = _classify_discovery_flow_result(
                await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context=context,
                    data=payload,
                )
            )
            triggered = True

        if outcome is CloudDiscoveryOutcome.RETRY:
            _LOGGER.debug(
                "Cloud discovery flow aborted transiently for %s (namespace=%s); "
                "the producer is asked to retry",
                _redact_account_for_log(email, stable_key),
                ns,
            )
        else:
            _LOGGER.info(
                "Cloud discovery flow queued for %s (namespace=%s)",
                _redact_account_for_log(email, stable_key),
                ns,
            )

        return outcome
    finally:
        async with lock:
            runtime.active_keys.discard(stable_key)


@dataclass(slots=True)
class _SecretsScanResult:
    """Structured result produced by reading Auth/secrets.json.

    ``mtime`` is the modification time of the file the payload was read from.
    It travels with the content identity (``digest``) on purpose: the
    delete-after-import hook has to decide whether a *differing* bundle can be
    older than the imported one, and reading a second, independently taken
    ``stat()`` there would compare two unrelated snapshots.
    """

    email: str
    token: str | None
    bundle: dict[str, Any]
    digest: str
    stable_key: str
    mtime: float


def _default_secrets_path() -> Path:
    """Return the canonical bundled ``Auth/secrets.json`` watch path."""

    return Path(__file__).resolve().parent / "Auth" / "secrets.json"


def _default_container_data_path() -> Path:
    """Return the deterministic same-machine login-container output path.

    The login container (``docker-login/docker-compose.yml``) writes its
    produced ``secrets.json`` to the writable ``./data`` volume, which is the
    ``docker-login/data`` sibling directory of this integration package. On a
    single-host install (Home Assistant and the login container share a
    filesystem) that file is directly readable, so watching it makes Track A
    zero-config: a one-click login lands the bundle here and discovery imports it
    without the user having to configure any extra watch path. It is deliberately
    a *deterministic default* (not an option) so the common case needs no setup;
    :data:`SECRETS_EXTRA_WATCH_PATHS` remains the advanced override for
    non-default layouts.
    """

    return Path(__file__).resolve().parent / "docker-login" / "data" / "secrets.json"


def _default_watch_paths() -> list[Path]:
    """Return the zero-config default watch paths (bundled Auth + container data)."""

    return [_default_secrets_path(), _default_container_data_path()]


def _extract_bundle_email(bundle: Mapping[str, Any]) -> str | None:
    for key in ("google_email", "email", "username", "Email"):
        value = bundle.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_bundle_token(bundle: Mapping[str, Any]) -> str | None:
    for key in ("oauth_token", "aas_token", "token"):
        value = bundle.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def secrets_bundle_digest(bundle: Mapping[str, Any]) -> str:
    """Return the canonical content digest of a secrets bundle.

    This is the single definition of "same bundle content" in the integration.
    It is used for the watcher's change signature, for the newest-wins tiebreak
    on identical modification times, and by the delete-after-import hook to tell
    the imported bundle apart from a *newer* one of the same account.

    The bundle is first run through :func:`normalize_secrets_bundle` and then
    serialized canonically (sorted keys, no separators whitespace) before
    hashing with SHA-256. Normalizing first is what makes the value
    identity-safe across the import boundary: the config flow stores the
    *normalized* bundle in its discovery payload, so hashing the raw file
    content instead would make the on-disk file and the payload it produced hash
    differently whenever normalization changed anything (a stray whitespace, a
    promoted scoped ``shared_key``). ``normalize_secrets_bundle`` is idempotent,
    so normalizing an already normalized payload is a no-op.
    """

    normalized = normalize_secrets_bundle(dict(bundle))
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_secrets_bundle(path: Path) -> _SecretsScanResult | None:
    """Read, parse and identify a single secrets.json bundle.

    Executor-safe, watcher-independent core: a missing/unreadable/unparseable
    file (or one without a Google account email) yields ``None`` and only debug
    logs. Both the watcher and the delete-after-import hook share this single
    read path so the JSON parsing and stable-key derivation live in one place.

    The ``stat()`` for :attr:`_SecretsScanResult.mtime` is taken *after* the
    read, deliberately: if the file is rewritten in between, the returned mtime
    belongs to the newer content while ``digest`` still describes the older one.
    Consumers that use the mtime as a "can this be fresher than my import?"
    guard therefore err towards *keeping* the file, never towards deleting a
    bundle they have not seen.
    """

    try:
        raw_text = path.read_text(encoding="utf-8")
        # Vanishing between read and stat is a FileNotFoundError like any other:
        # without a timestamp the bundle has no place in the newest-wins order.
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as err:
        _LOGGER.debug(
            "Unable to read secrets bundle",
            extra={"bundle_path": str(path)},
            exc_info=err,
        )
        return None

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as err:
        _LOGGER.debug(
            "Invalid secrets.json content",
            extra={"bundle_path": str(path)},
            exc_info=err,
        )
        return None

    if not isinstance(parsed, dict):
        _LOGGER.debug(
            "Ignoring secrets bundle: not a JSON object",
            extra={"bundle_path": str(path)},
        )
        return None

    email = _extract_bundle_email(parsed)
    if not email:
        _LOGGER.debug(
            "Ignoring secrets bundle: Google account email missing",
            extra={"bundle_path": str(path)},
        )
        return None

    token = _extract_bundle_token(parsed)
    stable_key = _cloud_discovery_stable_key(email, token, parsed)
    return _SecretsScanResult(
        email=email,
        token=token,
        bundle=dict(parsed),
        digest=secrets_bundle_digest(parsed),
        stable_key=stable_key,
        mtime=mtime,
    )


def scan_secrets_bundles(
    paths: Iterable[Path],
) -> list[tuple[Path, _SecretsScanResult]]:
    """Read every identifiable bundle among ``paths``, newest first.

    Single definition of the newest-wins ordering, shared by the watcher (which
    only needs the winner) and the delete-after-import hook (which needs the
    winner *and* every candidate). Pairs are returned sorted by modification
    time descending, breaking ties on identical mtimes with the larger content
    digest (mtime resolution on network mounts/QNAP can be coarse, so the digest
    tiebreak keeps the selection stable). ``result[0]`` is therefore the winner.
    Note what the tiebreak does *not* say: on identical mtimes the digest order
    is arbitrary with respect to age, so "is the scan winner" must never be used
    as a proof of "is the freshest content" (see
    ``config_flow._async_delete_watched_secrets``, which compares mtimes per
    candidate instead).

    A path that is missing, unreadable, unparseable or lacking an account email
    is skipped, so an absent path is a strict no-op and the caller can only
    conclude "account not determinable" for the paths that are missing from the
    result. The ordering key comes from :func:`read_secrets_bundle` (which stats
    the file it read), so the mtime and the digest of an entry always describe
    the same snapshot. Blocking file IO: call it from an executor job.
    """

    candidates: list[tuple[Path, _SecretsScanResult]] = []
    for path in paths:
        result = read_secrets_bundle(path)
        if result is None:
            continue
        candidates.append((path, result))

    candidates.sort(
        key=lambda candidate: (candidate[1].mtime, candidate[1].digest), reverse=True
    )
    return candidates


class SecretsJSONWatcher:
    """Poll one or more secrets.json files and trigger discovery on change.

    The watcher observes a *list* of candidate paths. In production the
    :class:`DiscoveryManager` supplies the zero-config defaults
    (:func:`_default_watch_paths`: the bundled ``Auth/secrets.json`` plus the
    deterministic login-container ``docker-login/data/secrets.json``), and users
    may add further paths via the :data:`SECRETS_EXTRA_WATCH_PATHS` option (for a
    non-default container-data layout). When several candidate files exist
    simultaneously the *newest* one
    (by modification time, with a SHA-256 digest tiebreak on identical mtimes)
    wins the signature, so a single freshly written bundle is imported
    deterministically regardless of how many paths are observed.
    """

    __slots__ = (
        "_hass",
        "_paths",
        "_namespace",
        "_lock",
        "_last_signature",
        "_retry_signature",
        "_retry_attempts",
        "_unsubscribers",
    )

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        path: Path | None = None,
        paths: list[Path] | None = None,
        namespace: str = SECRETS_DISCOVERY_NAMESPACE,
    ) -> None:
        self._hass = hass
        if paths is not None:
            resolved = list(paths)
        elif path is not None:
            resolved = [path]
        else:
            resolved = [_default_secrets_path()]
        # De-duplicate while preserving order so repeated paths do not double-scan.
        seen: set[Path] = set()
        self._paths: list[Path] = []
        for candidate in resolved:
            if candidate in seen:
                continue
            seen.add(candidate)
            self._paths.append(candidate)
        self._namespace = namespace
        self._lock = asyncio.Lock()
        self._last_signature: str | None = None
        self._retry_signature: str | None = None
        self._retry_attempts = 0
        self._unsubscribers: list[CALLBACK_TYPE] = []

    @property
    def watch_paths(self) -> tuple[Path, ...]:
        """Return the immutable tuple of observed secrets.json paths."""

        return tuple(self._paths)

    async def async_update_paths(
        self, paths: list[Path], *, forget_signature: bool = True
    ) -> None:
        """Replace the observed paths using the same dedup/order as ``__init__``.

        Lock-guarded against a concurrent scan.

        ``forget_signature`` (default ``True``, the historic behaviour) resets
        ``_last_signature`` *and* the retry budget via
        :meth:`_forget_signature`, so the next scan re-evaluates the changed
        path set from scratch. Callers that only *shrink* the path set pass
        ``False``: nothing new can be discovered there, and re-arming would make
        the watcher import a bundle it has already seen. Leaving the signature
        in place is safe because :meth:`_scan` forgets it anyway as soon as no
        bundle is found on the remaining paths.
        """

        async with self._lock:
            seen: set[Path] = set()
            deduped: list[Path] = []
            for candidate in paths:
                if candidate in seen:
                    continue
                seen.add(candidate)
                deduped.append(candidate)
            self._paths = deduped
            if forget_signature:
                self._forget_signature()

    async def async_start(self) -> None:
        """Begin watching for secrets.json updates."""

        await self.async_force_scan()
        self._unsubscribers.append(
            async_track_time_interval(
                self._hass,
                self._handle_interval,
                _DEFAULT_SECRETS_SCAN_INTERVAL,
            )
        )

    async def async_stop(self) -> None:
        """Stop watching for secrets.json updates."""

        while self._unsubscribers:
            unsub = self._unsubscribers.pop()
            try:
                unsub()
            except Exception as err:  # noqa: BLE001 - defensive best effort
                _LOGGER.debug(
                    "Error while unsubscribing secrets watcher",
                    exc_info=err,
                )
        self._forget_signature()

    async def async_force_scan(self) -> None:
        """Force an immediate scan of the secrets.json bundle."""

        await self._scan(reason="manual")

    @callback
    def _handle_interval(self, _now: datetime | None) -> None:
        self._hass.async_create_task(self._scan(reason="interval"))

    @callback
    def _forget_signature(self) -> None:
        """Drop the armed signature together with its retry budget.

        Single writer for "this watcher currently knows nothing": used by
        stop/path-change/bundle-missing. Keeping both fields in one helper is
        what makes the budget reset rules complete -- a bundle that disappears
        and comes back byte-identical gets a fresh budget, not the exhausted
        one from its previous life.
        """

        self._last_signature = None
        self._retry_signature = None
        self._retry_attempts = 0

    @callback
    def _invalidate_signature(self, signature: str) -> None:
        """Re-arm ``signature`` so the next scan retries the same bundle.

        Called from the discovery task's done-callback (or straight from the
        scheduling call when the outcome is unobservable), i.e. *after* the
        :meth:`_scan` that armed the signature has already released the lock.

        Deliberately lock-free: the callback runs on the event loop thread, and
        acquiring ``self._lock`` would need a task (the callback is sync) and
        could interleave with a scan that is already running. A single attribute
        assignment on the loop thread is atomic with respect to every other
        watcher operation, and the identity check keeps it safe against a newer
        scan: if a *different* bundle has been armed meanwhile, that newer state
        wins and the stale failure is ignored.

        Retries are **bounded** per signature. Re-arming is not free: each new
        attempt appends another full copy of the bundle (OAuth token included)
        to ``runtime.results`` and may emit another warning, so an unbounded
        retry turns a deterministic failure into unbounded credential copies in
        memory plus a permanent log flood at scan rate. After
        :data:`_MAX_SECRETS_RETRY_ATTEMPTS` retries the signature therefore
        stays armed (= pre-feedback-channel behaviour), and the operator is told
        once, at warning level, that the bundle was given up on. This method is
        the single owner of the budget: it rebases it on the first failure of a
        signature it has not seen yet, and :meth:`_forget_signature` drops it
        whenever the bundle vanishes, the watch paths change or the watcher
        stops. Every operator reaction (rewrite the bundle, remove and restore
        it, change the paths, restart Home Assistant) therefore buys a full new
        budget.
        """

        if self._last_signature != signature:
            return

        if self._retry_signature != signature:
            self._retry_signature = signature
            self._retry_attempts = 0
        self._retry_attempts += 1

        if self._retry_attempts > _MAX_SECRETS_RETRY_ATTEMPTS:
            _LOGGER.warning(
                "Secrets discovery gave up on the current bundle after %s failed "
                "attempts; it will be retried once the bundle changes, the watch "
                "paths change or Home Assistant restarts",
                _MAX_SECRETS_RETRY_ATTEMPTS + 1,
                # The signature itself is never logged: it embeds the account
                # e-mail. The observed paths identify the bundle well enough.
                extra={"bundle_paths": [str(path) for path in self._paths]},
            )
            return

        self._last_signature = None

    async def _scan(self, *, reason: str) -> None:
        async with self._lock:
            result = await self._hass.async_add_executor_job(self._read_bundle)
            if result is None:
                if self._last_signature is not None:
                    _LOGGER.debug(
                        "Secrets discovery reset; bundle missing",
                        extra={
                            "reason": reason,
                            "bundle_paths": [str(path) for path in self._paths],
                        },
                    )
                self._forget_signature()
                return

            signature = f"{result.stable_key}:{result.digest}"
            if signature == self._last_signature:
                return

            # Arming a *different* bundle retires the budget of the previous
            # one. Without this, an exhausted signature keeps its spent counter
            # while another bundle is armed in between, and when it comes back
            # (two watched paths plus the delete-after-import hook make that
            # routine) it would get a single attempt instead of a full budget --
            # and the give-up warning would then claim attempts that never
            # happened in this life. ``_invalidate_signature`` still owns the
            # counting; this is the ownership *transfer*.
            if self._retry_signature is not None and self._retry_signature != signature:
                self._retry_signature = None
                self._retry_attempts = 0

            self._last_signature = signature

            existing_entry = None
            try:
                existing_entry = cf._find_entry_by_email(self._hass, result.email)
            except Exception as err:  # noqa: BLE001 - defensive
                _LOGGER.debug("Failed to query existing entries for discovery: %s", err)

            update_source = getattr(
                cf,
                "DISCOVERY_UPDATE_SOURCE",
                "discovery_update_info",
            )
            discovery_source = getattr(
                cf,
                "SOURCE_DISCOVERY",
                "discovery",
            )
            source = update_source if existing_entry is not None else discovery_source
            title = await self._async_render_title(
                result.email, is_update=existing_entry is not None
            )

            payload = _assemble_cloud_discovery_payload(
                email=result.email,
                token=result.token,
                secrets_bundle=result.bundle,
                discovery_ns=self._namespace,
                discovery_stable_key=result.stable_key,
                title=title,
                source=source,
            )

            runtime = _cloud_discovery_runtime(self._hass, existing_entry)
            results_list = runtime.results

            if not isinstance(  # pragma: no cover - unreachable, kept for typing
                results_list, _CloudDiscoveryResults
            ):
                # Not reachable in production and not pretended to be: the call
                # to ``_cloud_discovery_runtime`` right above *guarantees* a
                # ``_CloudDiscoveryResults`` (it even replaces a foreign list),
                # and no ``await`` sits between that guarantee and this check,
                # so nothing can swap the container in between. The branch stays
                # because ``CloudDiscoveryRuntime.results`` is typed
                # ``_CloudDiscoveryResults | list[...] | None`` and mypy needs
                # the narrowing before the keyword-only ``append`` call below;
                # deleting it would mean an ``assert``/``cast`` instead, which
                # buys nothing. It is marked ``no cover`` rather than covered by
                # a test that monkeypatches ``_cloud_discovery_runtime``: such a
                # test would fabricate a state production cannot produce.
                self._invalidate_signature(signature)
                _LOGGER.debug(
                    "Secrets discovery results missing runtime container",
                    extra={
                        "account": _redact_account_for_log(
                            result.email, result.stable_key
                        ),
                    },
                )
                return

            try:
                results_list.append(
                    payload,
                    on_failure=partial(self._invalidate_signature, signature),
                )
            except Exception as err:  # noqa: BLE001 - keep watcher alive
                # Queueing never got far enough to report back, so re-arm here.
                self._invalidate_signature(signature)
                _LOGGER.warning(
                    "Secrets discovery flow queueing failed",
                    extra={
                        "account": _redact_account_for_log(
                            result.email, result.stable_key
                        )
                    },
                    exc_info=err,
                )
                return

            _LOGGER.debug(
                "Queued secrets discovery",
                extra={
                    "account": _redact_account_for_log(result.email, result.stable_key),
                    "reason": reason,
                },
            )

    async def _async_render_title(self, email: str, *, is_update: bool) -> str | None:
        language = (
            getattr(self._hass.config, "language", translation.LOCALE_EN)
            or translation.LOCALE_EN
        )
        try:
            resources = await translation.async_get_translations(
                self._hass,
                language,
                "component",
                integrations={DOMAIN},
            )
        except Exception as err:  # noqa: BLE001 - translation backend optional
            _LOGGER.debug("Translation lookup failed: %s", err)
            resources = {}

        key = (
            f"component.{DOMAIN}.config.progress.discovery_secrets_update"
            if is_update
            else f"component.{DOMAIN}.config.progress.discovery_secrets_new"
        )
        template = resources.get(key)
        if isinstance(template, str):
            try:
                return template.format(email=email)
            except Exception as err:  # noqa: BLE001 - fallback to raw string
                _LOGGER.debug("Failed to format discovery title %s: %s", key, err)
                return template
        return None

    def _read_bundle(self) -> _SecretsScanResult | None:
        """Return the newest valid bundle across all observed paths.

        Runs in the executor. Delegates to the shared
        :func:`scan_secrets_bundles` ordering so the watcher and the
        delete-after-import hook agree on which bundle is the current winner;
        missing paths are silently skipped, so empty/absent extra paths are a
        no-op and a single-path watcher behaves exactly as before.
        """

        scan = scan_secrets_bundles(self._paths)
        return scan[0][1] if scan else None


def _collect_extra_watch_paths(
    hass: HomeAssistant, *, exclude_entry_id: str | None = None
) -> list[Path]:
    """Return additional secrets.json watch paths configured via options.

    The option ``SECRETS_EXTRA_WATCH_PATHS`` may hold a single path string or a
    list of path strings on any googlefindmy config entry. Empty, blank or
    non-string values are ignored so a missing/empty option is a strict no-op.

    Disabled entries are skipped: ``ConfigEntries.async_entries`` defaults to
    ``include_disabled=True``, so without the check a deliberately switched-off
    account would keep its extra path polled. The check reads
    ``entry.disabled_by`` defensively instead of narrowing the lookup, because
    the duck-typed entry containers used by callers and tests do not all accept
    the ``include_disabled`` keyword. Enabling or disabling an entry does not
    itself run an options update (``async_set_disabled_by`` reloads the entry
    instead of calling ``async_update_entry``), so the change takes effect at
    the next recomputation: an options update, an entry removal or a restart.

    ``exclude_entry_id`` leaves one entry's paths out of the result. Entry
    removal passes the entry being removed. Home Assistant already deletes the
    entry from its registry before it awaits ``async_remove_entry``, so the
    ``async_entries`` lookup below normally no longer sees it; the explicit
    exclusion is a defense-in-depth guard, because that ordering is a Home
    Assistant internal that is neither documented nor stable across releases.
    """

    manager = getattr(hass, "config_entries", None)
    async_entries = getattr(manager, "async_entries", None)
    if not callable(async_entries):
        return []

    seen: set[Path] = set()
    extra: list[Path] = []
    try:
        entries = list(async_entries(DOMAIN))
    except Exception:  # noqa: BLE001 - defensive best effort
        _LOGGER.debug("Extra watch-path lookup failed", exc_info=True)
        return []

    for entry in entries:
        if (
            exclude_entry_id is not None
            and getattr(entry, "entry_id", None) == exclude_entry_id
        ):
            continue
        if getattr(entry, "disabled_by", None) is not None:
            continue
        options = getattr(entry, "options", None) or {}
        raw = options.get(SECRETS_EXTRA_WATCH_PATHS)
        if not raw:
            continue
        candidates = [raw] if isinstance(raw, str) else raw
        if not isinstance(candidates, (list, tuple)):
            continue
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            path = Path(candidate.strip())
            if path in seen:
                continue
            seen.add(path)
            extra.append(path)

    return extra


class DiscoveryManager:
    """Lifecycle manager for discovery watchers."""

    __slots__ = ("_hass", "_watchers", "_stop_unsub", "_started")

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._watchers: list[SecretsJSONWatcher] = []
        self._stop_unsub: CALLBACK_TYPE | None = None
        self._started = False

    async def async_start(self) -> None:
        if self._started:
            return

        # Zero-config defaults (bundled Auth/secrets.json + the deterministic
        # login-container docker-login/data/secrets.json) plus any advanced
        # option override. SecretsJSONWatcher de-duplicates, so an override that
        # repeats a default is harmless. The container-data default is included
        # here so the account-aware delete hook (which reads the manager's
        # watch_paths) also clears it after importing that account.
        watch_paths = [
            *_default_watch_paths(),
            *_collect_extra_watch_paths(self._hass),
        ]
        watcher = SecretsJSONWatcher(self._hass, paths=watch_paths)
        await watcher.async_start()
        self._watchers.append(watcher)
        self._stop_unsub = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._handle_hass_stop
        )
        self._started = True

    async def async_stop(self) -> None:
        if not self._started:
            return

        if self._stop_unsub is not None:
            try:
                self._stop_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._stop_unsub = None

        while self._watchers:
            watcher = self._watchers.pop()
            await watcher.async_stop()

        self._started = False

    async def _handle_hass_stop(self, _event: Any) -> None:
        await self.async_stop()

    async def async_force_secrets_scan(self) -> None:
        for watcher in self._watchers:
            await watcher.async_force_scan()

    async def async_refresh_watch_paths(
        self, *, exclude_entry_id: str | None = None
    ) -> None:
        """Recompute and apply the watch paths after an entry set change.

        Idempotent: it recomputes from the defaults plus every *considered*
        entry's ``SECRETS_EXTRA_WATCH_PATHS``, so with no ``exclude_entry_id``
        it does not matter which entry changed. ``exclude_entry_id`` narrows
        that set by exactly one entry and is passed while that entry is being
        removed, so its extra paths are dropped instead of being polled until
        the next entry update or restart. See :func:`_collect_extra_watch_paths`
        for why the exclusion is explicit.

        Only *added* paths justify re-arming and rescanning. A force scan
        re-imports whatever bundle it finds, and the discovery update flow
        applies such an import to the matching entry without any user
        interaction, so an unconditional rescan on an unchanged or shrunken
        path set would overwrite credentials and delete bundles that nobody
        touched. Therefore, per watcher:

        * identical path set: skip entirely (no update, no scan),
        * paths only removed: update without forgetting the signature and
          without scanning, since nothing new can be discovered there. A stale
          ``_last_signature`` is harmless: :meth:`SecretsJSONWatcher._scan`
          forgets it as soon as no bundle is found on the remaining paths.
        * paths added: forget the signature and force an immediate scan, so a
          freshly configured path is picked up without a Home Assistant restart.

        The comparison is by set, not by order: the winning bundle is chosen by
        modification time (see :meth:`SecretsJSONWatcher._read_bundle`), so a
        pure reordering changes nothing observable.
        """

        if not self._watchers:
            return

        # ``dict.fromkeys`` mirrors the dedup/order semantics of
        # ``async_update_paths`` so the comparison below sees exactly the list
        # the watcher would end up storing.
        watch_paths = list(
            dict.fromkeys(
                [
                    *_default_watch_paths(),
                    *_collect_extra_watch_paths(
                        self._hass, exclude_entry_id=exclude_entry_id
                    ),
                ]
            )
        )
        desired = set(watch_paths)
        for watcher in self._watchers:
            current = set(watcher.watch_paths)
            if desired == current:
                continue
            added = bool(desired - current)
            await watcher.async_update_paths(list(watch_paths), forget_signature=added)
            if added:
                await watcher.async_force_scan()

    @property
    def watch_paths(self) -> tuple[Path, ...]:
        """Return every secrets.json path observed across all watchers.

        Consumers (for example the discovery flow's delete-after-import hook) use
        this to reconcile observed bundles once one has been imported. The hook
        is account- *and* content-aware: it removes the copies that carry the
        imported payload (same account key and same
        :func:`secrets_bundle_digest`) plus same-account copies that are provably
        older, while any bundle of a different account, any bundle that could be
        fresher than the import, and anything unidentifiable stays on disk. So no
        stale secret of the imported account is left behind, and a bundle the
        login container wrote during the confirmation is not destroyed.
        """

        collected: list[Path] = []
        seen: set[Path] = set()
        for watcher in self._watchers:
            for path in watcher.watch_paths:
                if path in seen:
                    continue
                seen.add(path)
                collected.append(path)
        return tuple(collected)


async def async_initialize_discovery_runtime(hass: HomeAssistant) -> DiscoveryManager:
    """Create and start the discovery manager if not already running."""

    manager = DiscoveryManager(hass)
    await manager.async_start()
    return manager


__all__ = [
    "CLOUD_DISCOVERY_NAMESPACE",
    "SECRETS_DISCOVERY_NAMESPACE",
    "CloudDiscoveryOutcome",
    "SecretsJSONWatcher",
    "DiscoveryManager",
    "async_initialize_discovery_runtime",
    "read_secrets_bundle",
    "scan_secrets_bundles",
    "secrets_bundle_digest",
    "_cloud_discovery_runtime",
    "_trigger_cloud_discovery",
    "_redact_account_for_log",
]
