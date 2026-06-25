# tests/test_decrypt_shared_key_unavailable_authclass.py
"""Absent-shared-key classification of owner-key lookup failures.

When an existing entry has no usable ``shared_key`` (for example a pre-upgrade
incomplete bundle), the shared-key retrieval layer raises in non-interactive
(HA) mode. That is a *genuine credential defect*: the user must re-import a
complete secrets bundle, so the coordinator has to escalate to reauth, not skip
the device as a debug-only transient miss.

The previous classifier recognised this case only via the substring
``"missing or empty"``. The non-interactive absent message
(``"Shared key not available in non-interactive environment..."``) carries no
such substring, so it fell through to the transient default and the entry could
never decrypt without a (never-shown) prompt.

The robust fix raises a typed ``SharedKeyUnavailableError`` at the source and
classifies on the *type*, not the message wording. These are pure synchronous
unit tests against ``_classify_owner_key_failure``; they carry no asyncio
marker. They go RED on HEAD because the typed signal and its classifier branch
do not exist yet, so the positive case currently falls into the transient
default.
"""

from __future__ import annotations

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyLookupTransientError,
    SharedKeyMissingError,
    _classify_owner_key_failure,
)

# Soft import keeps this a clean assertion failure (RED) rather than a module
# collection error while the typed signal is being introduced.
try:
    from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import (
        SharedKeyUnavailableError,
    )

    _SYMBOL_PRESENT = True
except ImportError:  # pragma: no cover - the symbol exists once the fix lands
    _SYMBOL_PRESENT = False
    SharedKeyUnavailableError = None  # type: ignore[assignment,misc]


# The exact message the non-interactive retrieval path raises today. The fix
# must NOT depend on this wording (it classifies on the type), but pinning it
# documents the real-world trigger the Codex review flagged.
_NON_INTERACTIVE_ABSENT_MESSAGE = (
    "Shared key not available in non-interactive environment. "
    "Provide the key via secrets bundle or run the CLI with --reauth."
)


def test_shared_key_unavailable_is_typed_runtimeerror() -> None:
    """The absent-shared-key signal is a ``RuntimeError`` subclass.

    It must remain a ``RuntimeError`` subclass so the existing
    ``except (InvalidTag, RuntimeError)`` owner-key handlers keep catching it and
    the existing non-interactive retrieval test stays green.
    """
    assert _SYMBOL_PRESENT, "SharedKeyUnavailableError must exist (typed absent-key signal)"
    assert issubclass(SharedKeyUnavailableError, RuntimeError)


def test_typed_absent_shared_key_maps_to_reauth() -> None:
    """RED: a typed absent-shared-key error => reauth-worthy, not transient.

    A ``SharedKeyUnavailableError`` must classify as ``SharedKeyMissingError``
    (a ``DecryptionError``, so the coordinator escalates to a bundle re-import /
    reauth) and MUST NOT be an ``OwnerKeyLookupTransientError`` (which the
    poll/locate paths skip silently without advancing the reauth counter).
    RED today: HEAD has no typed branch, so this falls into the transient
    default.
    """
    assert _SYMBOL_PRESENT, "SharedKeyUnavailableError must exist (typed absent-key signal)"
    exc = SharedKeyUnavailableError(_NON_INTERACTIVE_ABSENT_MESSAGE)
    result = _classify_owner_key_failure(exc, context="initial lookup")
    assert isinstance(result, SharedKeyMissingError)
    assert isinstance(result, DecryptionError)
    assert not isinstance(result, OwnerKeyLookupTransientError)


def test_typed_absent_shared_key_robust_to_wording() -> None:
    """The classification holds even if the source message wording changes.

    This is the whole point of the typed signal: classification keys on the
    exception type, so a reworded absent-key message still escalates to reauth
    instead of silently regressing to the transient default (the exact fragility
    the substring matcher had).
    """
    assert _SYMBOL_PRESENT, "SharedKeyUnavailableError must exist (typed absent-key signal)"
    exc = SharedKeyUnavailableError("shared key cannot be obtained right now")
    result = _classify_owner_key_failure(exc, context="forced refresh")
    assert isinstance(result, SharedKeyMissingError)
    assert not isinstance(result, OwnerKeyLookupTransientError)


def test_plain_transient_runtimeerror_still_transient() -> None:
    """FP-pin: an untyped, unrelated RuntimeError stays transient.

    Typing the absent-key case must not widen the reauth path: a generic lookup
    failure with no typed signal and none of the credential-defect substrings
    must remain ``OwnerKeyLookupTransientError`` (no speculative reauth).
    """
    result = _classify_owner_key_failure(
        RuntimeError("temporary lookup hiccup"), context="initial lookup"
    )
    assert type(result) is OwnerKeyLookupTransientError
