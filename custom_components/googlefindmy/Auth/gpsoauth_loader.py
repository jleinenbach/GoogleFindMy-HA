from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import Any, Protocol, cast


class GpsoauthModule(Protocol):
    """Subset of the gpsoauth runtime API used by the integration."""

    def perform_oauth(  # noqa: PLR0913
        self,
        email: str,
        aas_token: str,
        android_id: int,
        *,
        service: str,
        app: str,
        client_sig: str,
    ) -> dict[str, Any]:
        """Exchange an AAS token for a scoped OAuth token."""

    def exchange_token(
        self, username: str, oauth_token: str, android_id: int
    ) -> dict[str, Any]:
        """Exchange an OAuth token for an AAS token."""


@lru_cache(maxsize=1)
def _gpsoauth_available() -> bool:
    """Return True when the gpsoauth dependency is importable."""

    return find_spec("gpsoauth") is not None


@lru_cache(maxsize=1)
def require_gpsoauth() -> GpsoauthModule:
    """Import and return the gpsoauth module.

    The import is deferred until runtime so the integration can be imported
    in environments where the optional dependency is absent. Callers should
    invoke this helper immediately before using gpsoauth APIs.

    A monkey-patch is applied on first import to inject the
    ``google_play_services_version`` parameter into every auth request.
    Without this parameter Google returns ``ServiceDisabled`` since ~May 2025.
    The patch is safe for gpsoauth >=2.0.0 (which already sets the value)
    because it only adds the key when missing.
    See: https://github.com/simon-weber/gpsoauth/pull/74
    """

    mod = import_module("gpsoauth")

    # --- Backport: inject google_play_services_version for gpsoauth <2.0.0 ---
    _orig = getattr(mod, "_perform_auth_request", None)
    if _orig is not None and not getattr(_orig, "_gps_version_patched", False):

        def _patched_perform_auth(
            data: dict[str, Any], proxies: Any = None
        ) -> Any:
            if "google_play_services_version" not in data:
                data["google_play_services_version"] = 240913000
            return _orig(data, proxies)

        _patched_perform_auth._gps_version_patched = True  # type: ignore[attr-defined]
        mod._perform_auth_request = _patched_perform_auth  # type: ignore[attr-defined]
    # --- End backport ---

    return cast(GpsoauthModule, mod)


@lru_cache(maxsize=1)
def load_gpsoauth_exceptions() -> ModuleType | None:
    """Return the gpsoauth exceptions module when available."""

    if not _gpsoauth_available():
        return None

    if find_spec("gpsoauth.exceptions") is None:
        return None

    return import_module("gpsoauth.exceptions")


class _GpsoauthProxy:
    """Provide a lazy gpsoauth module proxy for monkeypatching in tests."""

    def __getattr__(self, name: str) -> Any:
        return getattr(require_gpsoauth(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(require_gpsoauth(), name, value)


gpsoauth: GpsoauthModule = _GpsoauthProxy()
