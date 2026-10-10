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


# The `Error` values the ClientLogin protocol documents (Google, "ClientLogin
# for Installed Applications", archived 2012, "Error codes") plus NeedsBrowser,
# which gpsoauth surfaces for the same exchange. A value outside this list is
# server-supplied text and never reaches a record or an exception message
# (Auth AGENTS.md, "Logging"); it is reported as UNRECOGNIZED instead.
GPSOAUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "BadAuthentication",
        "NotVerified",
        "TermsNotAgreed",
        "CaptchaRequired",
        "Unknown",
        "AccountDeleted",
        "AccountDisabled",
        "ServiceDisabled",
        "ServiceUnavailable",
        "NeedsBrowser",
    }
)


def classify_gpsoauth_error(value: object) -> str:
    """Return the `Error` value of a gpsoauth response as a loggable kind.

    A documented code is returned in its documented spelling, matched
    case-insensitively; anything else, a non-string included, is
    `UNRECOGNIZED (<n> chars)`. An empty value stays empty.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return ""
    for code in GPSOAUTH_ERROR_CODES:
        if code.lower() == text.lower():
            return code
    return f"UNRECOGNIZED ({len(text)} chars)"


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

    Since ~February 2026 Google requires ``droidguard_results`` with the
    value ``"null"`` in auth requests.  gpsoauth 2.0.0 either omits it
    (``perform_oauth``) or sends ``"dummy123"`` (``perform_master_login``,
    ``exchange_token``), both of which cause authentication failures.

    This patch unconditionally sets the value to ``"null"`` (the string,
    not Python ``None``), matching the fix adopted by AuroraStore
    (commit 3ee8c13, 2026-02-11).

    References:
        https://github.com/simon-weber/gpsoauth/issues/81
        AuroraStore commit 50e3034b (GitLab whyorean/AuroraStore)
        https://github.com/BSkando/GoogleFindMy-HA/issues/114
    """

    if getattr(mod, "_gfm_patched", False):
        return  # already patched — avoid wrapping twice after cache_clear()

    orig = getattr(mod, "_perform_auth_request", None)
    if orig is None:
        return  # function not found — nothing to patch

    def _patched_perform_auth(data: dict[str, Any], proxies: Any = None) -> Any:
        data["droidguard_results"] = "null"
        return orig(data, proxies)

    mod._perform_auth_request = _patched_perform_auth
    mod._gfm_patched = True


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
