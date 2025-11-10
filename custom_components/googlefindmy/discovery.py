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
from typing import Any, cast

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
from .ha_typing import callback
from .const import CONF_GOOGLE_EMAIL, CONF_OAUTH_TOKEN, DATA_SECRET_BUNDLE, DOMAIN
from .email import normalize_email

cf = cast(Any, config_flow_module)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_DISCOVERY_SOURCE: str = getattr(cf, "SOURCE_DISCOVERY", "discovery")


def _home_assistant_discovery_sources() -> set[str]:
    """Return the set of discovery sources supported by Home Assistant."""

    cached: set[str] | None = getattr(
        _home_assistant_discovery_sources, "_cache", None
    )
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

    __slots__ = ("_hass",)

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__()
        self._hass = hass

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

        task.add_done_callback(_log_task_exception)


def _cloud_discovery_runtime(hass: HomeAssistant) -> dict[str, Any]:
    """Return the mutable runtime bucket tracking cloud discovery state."""

    bucket = hass.data.setdefault(DOMAIN, {})
    runtime = bucket.get("cloud_discovery")
    if not isinstance(runtime, dict):
        runtime = {}
        bucket["cloud_discovery"] = runtime

    lock = runtime.get("lock")
    if not getattr(lock, "acquire", None):
        lock = asyncio.Lock()
        runtime["lock"] = lock

    active = runtime.get("active_keys")
    if not isinstance(active, set):
        active = set()
        runtime["active_keys"] = active

    results = runtime.get("results")
    if isinstance(results, _CloudDiscoveryResults):
        if results._hass is not hass:
            replacement = _CloudDiscoveryResults(hass)
            replacement.extend(results)
            runtime["results"] = replacement
    else:
        replacement = _CloudDiscoveryResults(hass)
        if isinstance(results, list):
            replacement.extend(results)
        runtime["results"] = replacement

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
) -> bool:
    """Create or resume a config flow based on cloud-scan discovery data."""

    runtime = _cloud_discovery_runtime(hass)
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

    lock = runtime["lock"]
    async with lock:
        results_list = runtime["results"]
        if isinstance(results_list, _CloudDiscoveryResults):
            results_list.append(payload, trigger=False)
        else:
            try:
                results_list.append(dict(payload))
            except Exception:  # noqa: BLE001 - defensive
                pass
        if stable_key in runtime["active_keys"]:
            _LOGGER.debug(
                "Cloud discovery request deduplicated for %s (flow already active)",
                _redact_account_for_log(email, stable_key),
            )
            return False
        runtime["active_keys"].add(stable_key)

    triggered = False
    try:
        helper = getattr(cf, "async_create_discovery_flow", None)
        try:
            discovery_key = cf.DiscoveryKey(domain=DOMAIN, key=(ns, stable_key))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "DiscoveryKey instantiation failed (%s); using fallback", err
            )
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
            runtime["active_keys"].discard(stable_key)


@dataclass(slots=True)
class _SecretsScanResult:
    """Structured result produced by reading Auth/secrets.json."""

    email: str
    token: str | None
    bundle: dict[str, Any]
    digest: str
    stable_key: str


class SecretsJSONWatcher:
    """Poll the Auth/secrets.json file and trigger discovery flows when it changes."""

    __slots__ = (
        "_hass",
        "_path",
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
        namespace: str = SECRETS_DISCOVERY_NAMESPACE,
    ) -> None:
        self._hass = hass
        self._path = path or Path(__file__).resolve().parent / "Auth" / "secrets.json"
        self._namespace = namespace
        self._lock = asyncio.Lock()
        self._last_signature: str | None = None
        self._unsubscribers: list[CALLBACK_TYPE] = []

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
                _LOGGER.debug("Error while unsubscribing secrets watcher: %s", err)
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
                        "Secrets discovery reset (%s): %s missing",
                        reason,
                        self._path,
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

            runtime = _cloud_discovery_runtime(self._hass)
            results_list = runtime["results"]

            try:
                results_list.append(payload)
            except Exception as err:  # noqa: BLE001 - keep watcher alive
                _LOGGER.warning(
                    "Secrets discovery flow queueing failed for %s: %s",
                    _redact_account_for_log(result.email, result.stable_key),
                    err,
                )
                return

            _LOGGER.debug(
                "Queued secrets discovery for %s (%s)",
                _redact_account_for_log(result.email, result.stable_key),
                reason,
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
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as err:
            _LOGGER.debug("Unable to read secrets bundle %s: %s", self._path, err)
            return None

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Invalid secrets.json content at %s: %s", self._path, err)
            return None

        if not isinstance(parsed, dict):
            _LOGGER.debug(
                "Ignoring secrets bundle at %s: not a JSON object", self._path
            )
            return None

        email = self._extract_email(parsed)
        if not email:
            _LOGGER.debug(
                "Ignoring secrets bundle at %s: Google account email missing",
                self._path,
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

        watcher = SecretsJSONWatcher(self._hass)
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
