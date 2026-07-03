# custom_components/googlefindmy/coordinator/_reauth_reason.py
"""Structured, redaction-safe reauth-reason model for coordinator diagnostics.

This module defines the small, self-contained data model used to persist *why*
a re-authentication was triggered so it becomes visible in the diagnostics
download without requiring a ``--debug`` log capture.

Design invariants (see PLAN AE-6/AE-7):

- :class:`ReauthReasonCode` is a :class:`~enum.StrEnum` so serialization to the
  diagnostics payload is trivial (``str(code)``) and every value is a stable,
  literal identifier that is safe to expose verbatim.
- :class:`ReauthReason` only ever carries *literal-only* inputs: a
  :class:`ReauthReasonCode`, a constant literal ``origin`` string supplied at the
  call site, integer counters, and an epoch timestamp. It never stores
  ``str(err)``, tokens, e-mail addresses, or any other free-form/PII content.
  This "literal-only input" invariant is what makes the diagnostics mirror
  redaction-safe by construction (not a length cap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ReauthReasonCode(StrEnum):
    """Stable, redaction-safe identifiers for re-authentication triggers.

    The values are literal identifiers only; they carry no request-specific or
    user-specific content and are therefore safe to expose in diagnostics.
    """

    # api.py device-list / location HTTP 401/403 after a token refresh.
    HTTP_401_AFTER_REFRESH = "http_401_after_refresh"
    # api.py gpsoauth BadAuthentication / missing token.
    BADAUTH_GPSOAUTH = "badauth_gpsoauth"
    # Invalid AAS credential material.
    AAS_INVALID = "aas_invalid"
    # Spot transport permanent auth failure (SpotAuthPermanentError from
    # spot_request.py: AAS token invalid after refresh / gRPC UNAUTHENTICATED /
    # PERMISSION_DENIED). General Spot-session-permanent, not owner-key specific.
    SPOT_AUTH_PERMANENT = "spot_auth_permanent"
    # get_owner_key permanent Spot session failure.
    OWNER_KEY_PERMANENT = "owner_key_permanent"
    # get_owner_key empty/trailers-only SPOT response (transient/auth unproven).
    OWNER_KEY_EMPTY_RESPONSE = "owner_key_empty_response"
    # api.py "TokenCache is closed" invalid-state escalation.
    TOKENCACHE_CLOSED = "tokencache_closed"
    # api.py transient/generic Nova auth failure (single, immediate escalation).
    NOVA_AUTH_FAILED = "nova_auth_failed"
    # poll-cycle transient Nova auth failure that persisted across
    # _MAX_TRANSIENT_AUTH_FAILURES consecutive cycles before escalating. Distinct
    # from NOVA_AUTH_FAILED because "flaky auth that exhausted its retries" is the
    # primary poll-reauth signal (the reported intermittent-network case) and must
    # stay separable from a single hard failure. The counters snapshot carries the
    # consecutive/threshold counts.
    NOVA_AUTH_TRANSIENT_EXHAUSTED = "nova_auth_transient_exhausted"
    # api.py permanent Nova auth failure (NovaAuthPermanentError / is_permanent).
    NOVA_AUTH_PERMANENT = "nova_auth_permanent"
    # coordinator/polling FCM fatal auth error escalation.
    FCM_AUTH_FATAL = "fcm_auth_fatal"
    # Default when a catch site finds no ``reauth_code`` attribute on the
    # exception, or a direct reauth site without a more specific classification.
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ReauthReason:
    """Typed, redaction-safe record of the most recent re-authentication trigger.

    Attributes:
        code: The classified :class:`ReauthReasonCode`.
        origin: A constant literal string identifying the call site (for
            example ``"api.py:1099"``). Never derived from runtime introspection
            and never carries request/user content.
        counters: A snapshot of relevant integer counters at record time (for
            example the consecutive-transient-auth-failure count and its
            threshold). Values are plain ints only.
        recorded_at: Epoch seconds (UTC wall clock) when the reason was recorded.
    """

    code: ReauthReasonCode
    origin: str
    counters: dict[str, int] = field(default_factory=dict)
    recorded_at: float = 0.0
