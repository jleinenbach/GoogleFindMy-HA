# custom_components/googlefindmy/discovery.py
"""Discovery runtime helpers for the Google Find My Device integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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


def _log_task_exception(task: asyncio.Future[Any]) -> None:
    """Log and suppress exceptions raised by cloud discovery tasks."""

    try:
        task.result()
    except asyncio.CancelledError:  # pragma: no cover - cancellation is expected
        return
    except Exception as err:  # noqa: BLE001 - logging best effort
        _LOGGER.debug("Suppressed cloud discovery task exception: %s", err)


CLOUD_DISCOVERY_NAMESPACE = f"{DOMAIN}.cloud_scan"
SECRETS_DISCOVERY_NAMESPACE = f"{DOMAIN}.secrets_file"
_DEFAULT_SECRETS_SCAN_INTERVAL = timedelta(seconds=30)


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
    ) -> None:
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
        self._schedule(coro)

    def _schedule(self, coro: Coroutine[Any, Any, object]) -> None:
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
                task.add_done_callback(_log_task_exception)
            elif hasattr(task, "add_done_callback"):
                try:
                    task.add_done_callback(_log_task_exception)
                except Exception:  # noqa: BLE001 - defensive best effort
                    _LOGGER.debug(
                        "Unable to attach discovery task callback", exc_info=True
                    )
            return

        try:
            task = asyncio.create_task(cast(Coroutine[Any, Any, Any], coro))
        except RuntimeError:
            _LOGGER.debug(
                "Cloud discovery append scheduling skipped: event loop not running"
            )
            return

        self._register_handle(task)
        task.add_done_callback(_log_task_exception)

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
) -> bool:
    """Create or resume a config flow based on cloud-scan discovery data."""

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
            return False

        results_list.append(payload, trigger=False)
        if stable_key in runtime.active_keys:
            _LOGGER.debug(
                "Cloud discovery request deduplicated for %s (flow already active)",
                _redact_account_for_log(email, stable_key),
            )
            return False
        runtime.active_keys.add(stable_key)

    triggered = False
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
                await helper(
                    hass,
                    DOMAIN,
                    context=context,
                    data=payload,
                    discovery_key=discovery_key,
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
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context=context,
                data=payload,
            )
            triggered = True

        if triggered:
            _LOGGER.info(
                "Cloud discovery flow queued for %s (namespace=%s)",
                _redact_account_for_log(email, stable_key),
                ns,
            )
        else:
            _LOGGER.debug(
                "Cloud discovery flow skipped for %s (namespace=%s)",
                _redact_account_for_log(email, stable_key),
                ns,
            )

        return triggered
    finally:
        async with lock:
            runtime.active_keys.discard(stable_key)


@dataclass(slots=True)
class _SecretsScanResult:
    """Structured result produced by reading Auth/secrets.json."""

    email: str
    token: str | None
    bundle: dict[str, Any]
    digest: str
    stable_key: str


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
        self._unsubscribers: list[CALLBACK_TYPE] = []

    @property
    def watch_paths(self) -> tuple[Path, ...]:
        """Return the immutable tuple of observed secrets.json paths."""

        return tuple(self._paths)

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
        self._last_signature = None

    async def async_force_scan(self) -> None:
        """Force an immediate scan of the secrets.json bundle."""

        await self._scan(reason="manual")

    @callback
    def _handle_interval(self, _now: datetime | None) -> None:
        self._hass.async_create_task(self._scan(reason="interval"))

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
                self._last_signature = None
                return

            signature = f"{result.stable_key}:{result.digest}"
            if signature == self._last_signature:
                return

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

            if not isinstance(results_list, _CloudDiscoveryResults):
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
                results_list.append(payload)
            except Exception as err:  # noqa: BLE001 - keep watcher alive
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

        Runs in the executor. Each existing, parseable path yields a candidate;
        the winner is the one with the newest modification time, breaking mtime
        ties deterministically by the SHA-256 digest (mtime resolution on network
        mounts/QNAP can be coarse, so the digest tiebreak keeps selection stable).
        Missing paths are silently skipped, so empty/absent extra paths are a
        no-op and a single-path watcher behaves exactly as before.
        """

        best_result: _SecretsScanResult | None = None
        best_mtime: float = float("-inf")
        for path in self._paths:
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError as err:
                _LOGGER.debug(
                    "Unable to stat secrets bundle",
                    extra={"bundle_path": str(path)},
                    exc_info=err,
                )
                continue

            result = self._read_single_bundle(path)
            if result is None:
                continue

            if best_result is None or mtime > best_mtime:
                best_result, best_mtime = result, mtime
            elif mtime == best_mtime and result.digest > best_result.digest:
                # Deterministic tiebreak on identical mtimes (coarse-resolution
                # filesystems / clock skew).
                best_result = result

        return best_result

    def _read_single_bundle(self, path: Path) -> _SecretsScanResult | None:
        try:
            raw_text = path.read_text(encoding="utf-8")
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

        email = self._extract_email(parsed)
        if not email:
            _LOGGER.debug(
                "Ignoring secrets bundle: Google account email missing",
                extra={"bundle_path": str(path)},
            )
            return None

        token = self._extract_token(parsed)
        digest = hashlib.sha256(
            json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        stable_key = _cloud_discovery_stable_key(email, token, parsed)
        return _SecretsScanResult(
            email=email,
            token=token,
            bundle=dict(parsed),
            digest=digest,
            stable_key=stable_key,
        )

    @staticmethod
    def _extract_email(bundle: Mapping[str, Any]) -> str | None:
        for key in ("google_email", "email", "username", "Email"):
            value = bundle.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_token(bundle: Mapping[str, Any]) -> str | None:
        for key in ("oauth_token", "aas_token", "token"):
            value = bundle.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


def _collect_extra_watch_paths(hass: HomeAssistant) -> list[Path]:
    """Return additional secrets.json watch paths configured via options.

    The option ``SECRETS_EXTRA_WATCH_PATHS`` may hold a single path string or a
    list of path strings on any googlefindmy config entry. Empty, blank or
    non-string values are ignored so a missing/empty option is a strict no-op.
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
        # here so the delete-ALL hook (which reads the manager's watch_paths)
        # also clears it after a successful import.
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

    @property
    def watch_paths(self) -> tuple[Path, ...]:
        """Return every secrets.json path observed across all watchers.

        Consumers (for example the discovery flow's delete-after-import hook) use
        this to remove *all* observed bundles once one has been imported, so no
        stale secret file is left behind in the installation tree.
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
    "SecretsJSONWatcher",
    "DiscoveryManager",
    "async_initialize_discovery_runtime",
    "_cloud_discovery_runtime",
    "_trigger_cloud_discovery",
    "_redact_account_for_log",
]
