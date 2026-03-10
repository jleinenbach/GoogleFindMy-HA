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
    """

    mod = cast(GpsoauthModule, import_module("gpsoauth"))
    _patch_perform_auth(mod)
    return mod


def _patch_perform_auth(mod: Any) -> None:
    """Monkey-patch gpsoauth._perform_auth_request to inject missing params.

    Since ~March 2026 Google requires ``droidguard_results`` in master-login
    requests.  gpsoauth 2.0.0 does not send it, causing "MissingDroidguard"
    errors.  Passing the string ``"null"`` satisfies the server-side check.

    The ``not in data`` guards make this idempotent: if a future gpsoauth
    release adds these fields natively the patch becomes a no-op.

    References:
        https://github.com/simon-weber/gpsoauth/issues/81
        AuroraStore commit 50e3034b (GitLab whyorean/AuroraStore)
        https://github.com/BSkando/GoogleFindMy-HA/issues/114
    """

    orig = getattr(mod, "_perform_auth_request", None)
    if orig is None:
        return  # function not found — nothing to patch

    def _patched_perform_auth(data: dict, proxies: Any = None) -> Any:
        if "droidguard_results" not in data:
            data["droidguard_results"] = "null"  # String "null", NOT Python None
        return orig(data, proxies)

    mod._perform_auth_request = _patched_perform_auth


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
