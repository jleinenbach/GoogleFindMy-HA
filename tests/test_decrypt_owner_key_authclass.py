# tests/test_decrypt_owner_key_authclass.py
"""AUTH-vs-transient classification of owner-key lookup failures (v3 R5/R6).

The v3 contract REPURPOSES ``OwnerKeyInvalidError`` into the reauth-worthy class
for the NON-retryable AUTH case, recognised ONLY by an ``error_kind`` attribute on
the raised exception -- never by substring matching of the message text. A bare
``RuntimeError`` carrying ``error_kind in {"badauthentication", "auth_error",
"invalid_grant"}`` must map to ``OwnerKeyInvalidError`` (a ``DecryptionError``,
NOT transient). Everything else -- including messages that merely *contain*
auth-looking substrings ("retry after 4012 ms" holds "401"), token-availability
misses, and other ``error_kind`` values such as ``"exchange_error"`` -- stays an
``OwnerKeyLookupTransientError``.

These are pure synchronous unit tests against ``_classify_owner_key_failure``;
they carry no asyncio marker. They go RED on HEAD because the classifier has no
``error_kind`` AUTH branch yet, so the positive case currently falls into the
transient default.
"""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyInvalidError,
    _classify_owner_key_failure,
)

# ``OwnerKeyLookupTransientError`` is the transient base class. It exists on HEAD,
# but importing it softly keeps the negative-case identity checks robust even in a
# hypothetical RED phase where the symbol is being moved/renamed; the AUTH-branch
# tests stay the load-bearing RED assertions either way.
try:
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (  # noqa: E501
        OwnerKeyLookupTransientError,
    )
except ImportError:  # pragma: no cover - defensive, symbol present on HEAD

    class _MissingTransientError:
        """Sentinel so identity checks fail (not error) if the symbol moves."""

    OwnerKeyLookupTransientError = _MissingTransientError  # type: ignore[assignment,misc]


def _runtime_error_with_kind(message: str, error_kind: str) -> RuntimeError:
    """Build a bare ``RuntimeError`` carrying an ``error_kind`` attribute.

    Mirrors how the lower owner-key layers tag a non-retryable auth failure: the
    discriminator must read the structured ``error_kind`` attribute, never parse
    the human-readable message.
    """
    exc = RuntimeError(message)
    exc.error_kind = error_kind  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize(
    "error_kind",
    ["badauthentication", "auth_error", "invalid_grant"],
)
def test_error_kind_auth_maps_to_reauth_owner_key_invalid(error_kind: str) -> None:
    """RED (R5): a structured ``error_kind`` auth tag => reauth-worthy, not transient.

    A bare ``RuntimeError`` tagged with one of the non-retryable auth kinds must
    classify as ``OwnerKeyInvalidError`` (a ``DecryptionError``, so the coordinator
    escalates to account-wide reauth) and MUST NOT be an
    ``OwnerKeyLookupTransientError`` (which the poll/locate paths skip silently).
    RED today: HEAD has no ``error_kind`` branch, so this falls into the transient
    default.
    """
    exc = _runtime_error_with_kind("auth failed", error_kind)
    result = _classify_owner_key_failure(exc, context="initial lookup")
    assert isinstance(result, OwnerKeyInvalidError)
    assert isinstance(result, DecryptionError)
    assert not isinstance(result, OwnerKeyLookupTransientError)


def test_substring_401_lookalike_stays_transient() -> None:
    """RED-guard (R7 FP-pin): a message merely CONTAINING "401" stays transient.

    ``"retry after 4012 ms"`` contains the substring ``"401"`` but is a transient
    backoff notice, not an auth failure. Because AUTH classification is structural
    (``error_kind`` only, no substring matching), this must remain
    ``OwnerKeyLookupTransientError`` -- exact type, never ``OwnerKeyInvalidError``.
    """
    result = _classify_owner_key_failure(
        RuntimeError("retry after 4012 ms"), context="initial lookup"
    )
    assert type(result) is OwnerKeyLookupTransientError


def test_no_valid_token_message_stays_transient() -> None:
    """R6: a token-availability miss is transient, not an auth defect.

    ``"No valid SPOT or ADM token available for the current user."`` is a
    cold-start / transient token-availability condition. With no ``error_kind``
    attribute it must stay ``OwnerKeyLookupTransientError``.
    """
    result = _classify_owner_key_failure(
        RuntimeError("No valid SPOT or ADM token available for the current user."),
        context="initial lookup",
    )
    assert type(result) is OwnerKeyLookupTransientError


def test_non_auth_error_kind_stays_transient() -> None:
    """R6: an ``error_kind`` outside the auth set stays transient.

    ``error_kind="exchange_error"`` is a non-auth structured tag; only the
    explicit auth kinds escalate to reauth. Everything else stays
    ``OwnerKeyLookupTransientError``.
    """
    exc = _runtime_error_with_kind("token exchange failed", "exchange_error")
    result = _classify_owner_key_failure(exc, context="initial lookup")
    assert type(result) is OwnerKeyLookupTransientError
