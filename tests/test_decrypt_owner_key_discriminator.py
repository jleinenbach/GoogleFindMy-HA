# tests/test_decrypt_owner_key_discriminator.py
"""Discriminator for stale-vs-missing shared key in owner-key decryption.

When ``async_get_owner_key`` fails, the root cause (wrong shared key vs. missing
shared key vs. a transient/partial server response) is otherwise collapsed into a
single opaque, speculative "shared key may be stale; re-authenticate" message.
These tests pin ``_classify_owner_key_failure`` (genuine credential failures stay
reauth-worthy ``DecryptionError`` subclasses; unknown/transient ones map to the
new ``OwnerKeyLookupTransientError``), the Liskov property that keeps every
existing ``except DecryptionError`` handler working, and the upstream
error-surfacing contracts (R8 sync wrapper, R9a gRPC detail, R9b eid_info shape,
R10 empty-response wording) the plan grounds those classifications on.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import grpclib.exceptions
import pytest
from cryptography.exceptions import InvalidTag
from grpclib.const import Status

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as _dl,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyInvalidError,
    SharedKeyMismatchError,
    SharedKeyMissingError,
    StaleOwnerKeyError,
    _classify_owner_key_failure,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.SpotApi import spot_request as _spot
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_eid_info_request as _eidreq,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_owner_key as _gok,
)
from custom_components.googlefindmy.SpotApi.spot_request import (
    SpotError,
    SpotGrpcStatusError,
    SpotNetworkError,
)

# AP3 introduces ``OwnerKeyLookupTransientError`` (NOT a ``DecryptionError``
# subclass; base ``Exception``). Until then this symbol does not exist. Import it
# softly so the *new* transient-contract tests below go RED on a missing/wrong
# type, while the unchanged R2/Liskov characterization tests in this module keep
# passing instead of erroring out the whole collection on a hard ImportError.
try:
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (  # noqa: E501
        OwnerKeyLookupTransientError,
    )
except ImportError:  # pragma: no cover - RED phase before AP3 lands the class

    class _MissingTransientError:
        """Placeholder so transient-contract tests fail (not error) pre-AP3.

        Identity comparisons (``type(result) is OwnerKeyLookupTransientError``)
        and ``isinstance`` checks against this sentinel never match a real
        ``DecryptionError``, so each transient test asserts RED while the R2
        tests stay green.
        """

    OwnerKeyLookupTransientError = _MissingTransientError  # type: ignore[assignment,misc]


# v3 AP1 introduces ``_OwnerKeyRederiveRequired``: an INTERNAL re-derive signal
# raised when a LOCAL owner-key STRUCTURE defect is detected, asking the caller to
# re-derive the owner key (force_refresh) instead of escalating to reauth. It
# subclasses ``OwnerKeyLookupTransientError`` (fail-safe: base ``Exception``, never
# ``DecryptionError``, never reauth). The class does not exist on HEAD yet, so it
# is imported softly through a sentinel -- the v3 re-derive tests below assert RED
# on a missing/wrong type instead of erroring out the whole collection.
try:
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (  # noqa: E501
        _OwnerKeyRederiveRequired,
    )
except ImportError:  # pragma: no cover - RED phase before AP1 lands the signal

    class _MissingRederiveSignal:
        """Placeholder so re-derive-contract tests fail (not error) pre-AP1.

        ``type(result) is _OwnerKeyRederiveRequired`` and the subclass checks
        below never match a real ``OwnerKeyInvalidError`` / ``DecryptionError``,
        so each re-derive assertion is RED while the unchanged tests stay green.
        """

    _OwnerKeyRederiveRequired = _MissingRederiveSignal  # type: ignore[assignment,misc]


def test_liskov_subclasses_are_decryption_errors() -> None:
    """All specific shared-key errors stay DecryptionError subclasses so existing
    ``except DecryptionError`` handlers keep catching them (no silent breakage)."""
    assert issubclass(SharedKeyMismatchError, DecryptionError)
    assert issubclass(SharedKeyMissingError, DecryptionError)
    assert issubclass(StaleOwnerKeyError, DecryptionError)
    # OwnerKeyInvalidError is a reauth-worthy credential defect: it MUST be a
    # DecryptionError (so ``except DecryptionError`` escalates it to reauth) and
    # MUST NOT be an OwnerKeyLookupTransientError (which the poll/locate paths skip
    # as a benign transient miss).
    assert issubclass(OwnerKeyInvalidError, DecryptionError)
    assert not issubclass(OwnerKeyInvalidError, OwnerKeyLookupTransientError)
    # v3 AP1: the internal re-derive signal is fail-safe -- it IS an
    # OwnerKeyLookupTransientError (so a leak is skipped as a benign transient
    # miss, never escalated to reauth) and is NOT a DecryptionError (so the
    # ``except DecryptionError`` reauth handlers never catch it). RED-tolerant:
    # the soft-imported sentinel makes these assertions fail (not error) on HEAD.
    assert issubclass(_OwnerKeyRederiveRequired, OwnerKeyLookupTransientError)
    assert not issubclass(_OwnerKeyRederiveRequired, DecryptionError)


def test_t2_invalid_tag_maps_to_shared_key_mismatch() -> None:
    """InvalidTag (AES-GCM auth failure) => wrong/stale shared key."""
    result = _classify_owner_key_failure(InvalidTag(), context="initial lookup")
    assert isinstance(result, SharedKeyMismatchError)
    assert "InvalidTag" in str(result)
    assert "initial lookup" in str(result)


def test_t2_missing_runtime_maps_to_shared_key_missing() -> None:
    """RuntimeError mentioning 'missing or empty' => incomplete bundle."""
    exc = RuntimeError("Shared key is missing or empty; cannot decrypt owner key")
    result = _classify_owner_key_failure(exc, context="initial lookup")
    assert isinstance(result, SharedKeyMissingError)


def test_t2_generic_runtime_maps_to_transient_not_reauth() -> None:
    """RED (AP3): the inverted default maps any other RuntimeError to the new
    ``OwnerKeyLookupTransientError`` instead of a plain ``DecryptionError``.

    Previously the generic fallback returned a ``DecryptionError`` carrying a
    speculative "shared key may be stale; re-authenticate" wording. After the
    default inversion (plan AP3, F3) an unclassified failure is a transient
    owner-key lookup miss: NO reauth prompt, an explicit transient marker. This
    rewrites the former ``type(result) is DecryptionError`` characterization and
    stays RED until the production class exists.
    """
    result = _classify_owner_key_failure(RuntimeError("boom"), context="x")
    assert type(result) is OwnerKeyLookupTransientError


def test_r1_generic_runtime_message_is_transient_not_stale() -> None:
    """RED (R1): the transient class wording carries a transient marker and makes
    NO stale / re-authenticate claim.

    Negative: ``str(result)`` mentions neither "stale" nor "re-authenticate"
    (the speculative wording the plan removes). Positive: it carries a transient
    marker (``transient`` or ``temporar``) so callers/users see an honest
    "retrieval did not complete" signal instead of a credential accusation.
    """
    result = _classify_owner_key_failure(RuntimeError("boom"), context="x")
    assert type(result) is OwnerKeyLookupTransientError
    text = str(result).lower()
    assert "stale" not in text
    assert "re-authenticate" not in text
    assert "transient" in text or "temporar" in text


def test_r1_server_field_error_is_transient_not_reauth_fallback() -> None:
    """RED (R1): a partial-server-response field error must classify as transient.

    ``get_owner_key.py`` raises ``RuntimeError("Missing or empty
    'encryptedOwnerKey'")`` (capital M) on a degraded/partial SPOT response. The
    legacy case-sensitive ``"missing or empty"`` check misses the capital M, so
    this falls through to the speculative reauth fallback today. It is a transient
    server condition, NOT a missing local bundle => ``OwnerKeyLookupTransientError``.
    """
    exc = RuntimeError("Missing or empty 'encryptedOwnerKey'")
    result = _classify_owner_key_failure(exc, context="x")
    assert type(result) is OwnerKeyLookupTransientError


def test_ap3_shared_key_missing_and_server_field_do_not_collide() -> None:
    """RED (AP3, mandatory boundary): two superficially similar strings must split.

    ``"Shared key is missing or empty; cannot decrypt owner key"`` is the local
    incomplete-bundle case and MUST stay ``SharedKeyMissingError`` (reauth-worthy).
    ``"Missing or empty 'encryptedOwnerKey'"`` is a server-field/partial-response
    case and MUST become ``OwnerKeyLookupTransientError``. The discriminator must
    anchor on "shared key" rather than the shared "missing or empty" substring so
    the two do not collapse into the same class.
    """
    missing_bundle = _classify_owner_key_failure(
        RuntimeError("Shared key is missing or empty; cannot decrypt owner key"),
        context="x",
    )
    assert isinstance(missing_bundle, SharedKeyMissingError)

    server_field = _classify_owner_key_failure(
        RuntimeError("Missing or empty 'encryptedOwnerKey'"),
        context="x",
    )
    assert type(server_field) is OwnerKeyLookupTransientError


def test_f6_shared_key_missing_verbatim_string_is_characterized() -> None:
    """Characterization anchor (F6): pin the exact ``get_owner_key.py:179`` string.

    ``decrypt_locations`` (this discriminator) and ``get_owner_key`` are edited by
    AP3/AP6/AP7b in lockstep. Pin the verbatim source string so a later wording
    drift in ``get_owner_key`` that desyncs the discriminator anchor is caught
    here. This assertion is GREEN today and must stay green (regression guard).
    """
    exc = RuntimeError("Shared key is missing or empty; cannot decrypt owner key")
    result = _classify_owner_key_failure(exc, context="initial lookup")
    assert isinstance(result, SharedKeyMissingError)


@pytest.mark.parametrize(
    "message",
    [
        # get_owner_key.py:421 -- owner key absent/invalid after retrieval.
        "Owner key is missing or invalid; please re-authenticate.",
        # get_owner_key.py:442 -- owner key decoded but is not a valid hex/base64 key.
        "Invalid owner_key format (expect 32-byte key in hex or base64).",
        # get_owner_key.py:465 -- owner key has the wrong byte length.
        "Owner key must be exactly 32 bytes long.",
    ],
)
def test_local_owner_key_defect_requests_rederive_not_reauth(message: str) -> None:
    """RED (v3 AP1): a LOCAL owner-key STRUCTURE defect now requests a RE-DERIVE.

    ``async_get_owner_key`` raises these deterministic ``RuntimeError`` strings
    when the locally stored owner key is missing/invalid, malformed, or the wrong
    length. The v3 contract REPURPOSES this path: instead of escalating straight
    to ``OwnerKeyInvalidError``/reauth, the classifier returns the internal
    ``_OwnerKeyRederiveRequired`` signal so the caller first re-derives the owner
    key (force_refresh). The signal is fail-safe: an ``OwnerKeyLookupTransientError``
    subclass (skipped as a benign miss, never reauth) and NOT a ``DecryptionError``
    (so ``except DecryptionError`` reauth handlers never catch it), and it makes
    no "re-authenticate" claim. RED today: HEAD still returns
    ``OwnerKeyInvalidError`` (a reauth-worthy ``DecryptionError``).
    """
    result = _classify_owner_key_failure(
        RuntimeError(message), context="initial lookup"
    )
    assert type(result) is _OwnerKeyRederiveRequired
    # Fail-safe: a transient subclass, never a reauth-worthy DecryptionError.
    assert isinstance(result, OwnerKeyLookupTransientError)
    assert not isinstance(result, DecryptionError)
    # No speculative credential accusation: never asks to re-authenticate.
    assert "re-authenticat" not in str(result).lower()


def test_local_owner_key_defect_does_not_collide_with_server_field_transient() -> None:
    """The (b2) owner-key-defect anchor must not steal the (c) server-field cases.

    ``"Missing or empty 'encryptedOwnerKey'"`` and ``"Decrypted owner_key is empty
    or invalid type"`` are partial-server-response conditions and MUST stay
    ``OwnerKeyLookupTransientError`` (transient), even though they mention the words
    "owner" and "invalid". Only the deterministic local-defect wordings map to
    ``OwnerKeyInvalidError``.
    """
    for server_msg in (
        "Missing or empty 'encryptedOwnerKey'",
        "Decrypted owner_key is empty or invalid type",
    ):
        result = _classify_owner_key_failure(RuntimeError(server_msg), context="x")
        assert type(result) is OwnerKeyLookupTransientError


@pytest.mark.asyncio
async def test_owner_key_invalid_tag_propagates_as_shared_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: an InvalidTag from the owner-key lookup surfaces from
    async_retrieve_identity_key as SharedKeyMismatchError (the path that used to
    collapse into an opaque, swallowed DecryptionError)."""

    async def _boom(**_kwargs: object) -> object:
        raise InvalidTag()

    monkeypatch.setattr(_dl, "async_get_owner_key", _boom)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60

    with pytest.raises(SharedKeyMismatchError):
        await _dl.async_retrieve_identity_key(registration, cache=object())


@pytest.mark.asyncio
async def test_e2ee_metadata_diagnostic_failure_logs_at_debug_not_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The best-effort E2EE metadata fetch is a non-actionable diagnostic aid.

    When it fails (e.g. transient network timeout or SSL teardown), the failure
    is swallowed and the regular decrypt error path continues unaffected, so it
    must be logged at DEBUG, not WARNING, to avoid user-facing log noise.
    """
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
        OwnerKeyInfo,
    )

    async def _owner_ok(
        *, cache: object, force_refresh: bool = False, **_kw: object
    ) -> object:
        # Owner-key acquisition succeeds, so the flow reaches the decrypt attempt.
        return OwnerKeyInfo(key=b"\x00" * 32, version=1)

    async def _meta_boom(**_kwargs: object) -> object:
        raise TimeoutError("Timeout after retries.")

    monkeypatch.setattr(_dl, "async_get_owner_key", _owner_ok)
    monkeypatch.setattr(_dl, "async_get_eid_info", _meta_boom)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    # Garbage identity key: decryption fails, so the flow falls through to the
    # best-effort metadata fetch (which we force to fail). ownerKeyVersion stays
    # 0, so no forced refresh short-circuits the path beforehand.
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60

    caplog.set_level(logging.DEBUG, logger=_dl.__name__)

    # The decrypt itself still fails; the metadata fetch is only a diagnostic aside.
    # _retry=False keeps the path deterministic (single, non-recursive emission;
    # avoids the blind-refresh branch and its unrelated WARNING).
    with pytest.raises(DecryptionError):
        await _dl.async_retrieve_identity_key(
            registration, cache=object(), _retry=False
        )

    diagnostic_records = [
        record
        for record in caplog.records
        if "Failed to retrieve E2EE metadata for diagnostics" in record.getMessage()
    ]
    assert diagnostic_records, (
        "expected the best-effort diagnostic log line to be emitted"
    )
    # Mutation-sharp: reverting debug -> warning flips levelno and fails this assertion.
    assert all(record.levelno == logging.DEBUG for record in diagnostic_records)
    assert not any(record.levelno == logging.WARNING for record in diagnostic_records)


@pytest.mark.asyncio
async def test_owner_key_forced_refresh_invalid_tag_maps_to_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: when a newer owner-key version triggers a forced refresh and
    that refresh also fails with InvalidTag, the failure still surfaces as
    SharedKeyMismatchError (the forced-refresh wrap path)."""
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
        OwnerKeyInfo,
    )

    async def _owner(
        *, cache: object, force_refresh: bool = False, **_kw: object
    ) -> object:
        if force_refresh:
            raise InvalidTag()
        return OwnerKeyInfo(key=b"\x00" * 32, version=1)

    monkeypatch.setattr(_dl, "async_get_owner_key", _owner)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60
    registration.encryptedUserSecrets.ownerKeyVersion = 5  # > cached version 1

    with pytest.raises(SharedKeyMismatchError):
        await _dl.async_retrieve_identity_key(registration, cache=object())


@pytest.mark.asyncio
async def test_r3_initial_lookup_spot_error_maps_to_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 (AP4): a typed ``SpotError`` from the initial owner-key lookup surfaces as
    ``OwnerKeyLookupTransientError`` -- never a stale/reauth ``DecryptionError``.

    ``SpotError`` is not a ``RuntimeError``, so the dedicated ``except SpotError``
    block (placed before the generic handler) classifies the transport/gRPC
    failure as transient and keeps it off the speculative reauth path (Q5).
    """

    async def _boom(**_kwargs: object) -> object:
        raise SpotGrpcStatusError("gRPC error: UNAVAILABLE")

    monkeypatch.setattr(_dl, "async_get_owner_key", _boom)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60

    with pytest.raises(OwnerKeyLookupTransientError) as excinfo:
        await _dl.async_retrieve_identity_key(registration, cache=object())

    text = str(excinfo.value).lower()
    assert "stale" not in text
    assert "re-authenticate" not in text
    assert "transient" in text


@pytest.mark.asyncio
async def test_r3_forced_refresh_spot_error_maps_to_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 (AP4): a typed ``SpotError`` during the forced refresh surfaces as
    ``OwnerKeyLookupTransientError`` (the forced-refresh wrap path mirrors the
    initial-lookup carve-out, never a stale/reauth ``DecryptionError``)."""
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
        OwnerKeyInfo,
    )

    async def _owner(
        *, cache: object, force_refresh: bool = False, **_kw: object
    ) -> object:
        if force_refresh:
            raise SpotNetworkError("Timeout after retries.")
        return OwnerKeyInfo(key=b"\x00" * 32, version=1)

    monkeypatch.setattr(_dl, "async_get_owner_key", _owner)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60
    registration.encryptedUserSecrets.ownerKeyVersion = 5  # > cached version 1

    with pytest.raises(OwnerKeyLookupTransientError) as excinfo:
        await _dl.async_retrieve_identity_key(registration, cache=object())

    assert "transient" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# R8: the synchronous SPOT fallback wrapper must NOT flatten a typed SpotError
# into a bare RuntimeError, or the Q5 invariant (typed transient errors stay
# transient) leaks through the defensive path.
# ---------------------------------------------------------------------------


class _DummyCache(_spot.TokenCache):
    """Minimal entry-scoped cache double for the SPOT request path."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value


@pytest.mark.asyncio
async def test_r8_sync_wrapper_preserves_spot_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R8): a ``SpotError`` raised by the sync SPOT helper stays a ``SpotError``.

    ``_spot_call_async`` falls back to the synchronous ``spot_request`` helper when
    no async helper is available, wrapping ``except Exception`` into
    ``RuntimeError("Synchronous SPOT request helper failed.")``. That flattening
    erases the typed-transient discriminator. After R8 the wrapper must re-raise a
    ``SpotError`` (network/grpc-status) type-preservingly. RED today: the wrapper
    collapses it to a plain ``RuntimeError``.
    """
    sentinel = SpotGrpcStatusError("gRPC error: UNAVAILABLE")

    def _sync_boom(*_a: object, **_k: object) -> bytes:
        raise sentinel

    # Force the sync fallback branch: hide the async helper, expose only sync.
    monkeypatch.setattr(_eidreq.spot_request_module, "async_spot_request", None)
    monkeypatch.setattr(
        _eidreq.spot_request_module, "spot_request", _sync_boom, raising=False
    )

    with pytest.raises(SpotError) as excinfo:
        await _eidreq._spot_call_async("Scope", b"payload", cache=_DummyCache())

    # Type-preserving: not flattened into a bare RuntimeError clone.
    assert type(excinfo.value) is not RuntimeError  # noqa: E721 - exact-type check


# ---------------------------------------------------------------------------
# R9a: the gRPC server detail text (GRPCError.message) must survive into the
# SpotError message instead of being dropped in favor of status.name alone.
# ---------------------------------------------------------------------------


def _install_grpc_outcome(monkeypatch: pytest.MonkeyPatch, outcome: Exception) -> None:
    """Drive ``async_spot_request`` to hit ``outcome`` on the gRPC stream."""

    class _Stream:
        async def __aenter__(self) -> _Stream:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def send_message(self, *_a: object, **_k: object) -> None:
            return None

        async def recv_message(self) -> Any:
            raise outcome

    class _Method:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def open(self, **_k: object) -> _Stream:
            return _Stream()

    monkeypatch.setattr(_spot, "UnaryUnaryMethod", _Method)
    monkeypatch.setattr(_spot, "_async_sleep", AsyncMock())
    monkeypatch.setattr(_spot, "_compute_delay", lambda attempt: 0.0)
    monkeypatch.setattr(_spot, "async_get_username", AsyncMock(return_value="user"))
    monkeypatch.setattr(_spot, "async_get_spot_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(_spot, "async_get_adm_token_api", AsyncMock(return_value="adm"))


@pytest.mark.asyncio
async def test_r9a_grpc_message_and_status_reach_spot_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R9a): a non-retried ``GRPCError`` surfaces both status.name AND message.

    ``FAILED_PRECONDITION`` is outside the retry/auth policy, so it maps directly
    to ``SpotGrpcStatusError``. Today the message is ``f"gRPC error: {status.name}"``
    and the server detail text is dropped. After R9a the resulting message must
    contain both ``status.name`` and the server-sent ``SENTINEL_MSG``.
    """
    err = grpclib.exceptions.GRPCError(Status.FAILED_PRECONDITION, "SENTINEL_MSG")
    _install_grpc_outcome(monkeypatch, err)

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    with pytest.raises(SpotGrpcStatusError) as excinfo:
        await _spot.async_spot_request(
            "Scope", b"payload", cache=_DummyCache(), transport=transport
        )

    message = str(excinfo.value)
    assert "FAILED_PRECONDITION" in message
    assert "SENTINEL_MSG" in message


@pytest.mark.asyncio
async def test_r9a_auth_token_sentinel_not_leaked_into_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R9a redaction): the captured gRPC detail must not carry an auth secret.

    The auth bearer token is injected as a unique sentinel; whatever R9a copies
    from the gRPC error into the ``SpotError`` message must be the server status
    text, never the request's authorization credential.
    """
    auth_sentinel = "AUTHTOKEN-CAFEBABE-DEADBEEF"
    err = grpclib.exceptions.GRPCError(Status.FAILED_PRECONDITION, "server detail")
    _install_grpc_outcome(monkeypatch, err)
    monkeypatch.setattr(
        _spot, "async_get_spot_token", AsyncMock(return_value=auth_sentinel)
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    with pytest.raises(SpotGrpcStatusError) as excinfo:
        await _spot.async_spot_request(
            "Scope", b"payload", cache=_DummyCache(), transport=transport
        )

    assert auth_sentinel not in str(excinfo.value)


@pytest.mark.asyncio
async def test_r9a_retries_exhausted_grpc_message_reaches_spot_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9a: a retryable gRPC status that exhausts the budget carries the server
    detail into the ``SpotRequestFailedAfterRetries`` message.

    ``UNAVAILABLE`` is retried; once the budget is spent the transient error must
    still surface the status name AND the last server-sent detail text (not only
    ``status.name``).
    """
    err = grpclib.exceptions.GRPCError(Status.UNAVAILABLE, "RETRY_SENTINEL_MSG")
    _install_grpc_outcome(monkeypatch, err)

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    from custom_components.googlefindmy.SpotApi.spot_request import (
        SpotRequestFailedAfterRetries,
    )

    with pytest.raises(SpotRequestFailedAfterRetries) as excinfo:
        await _spot.async_spot_request(
            "Scope", b"payload", cache=_DummyCache(), transport=transport
        )

    message = str(excinfo.value)
    assert "UNAVAILABLE" in message
    assert "RETRY_SENTINEL_MSG" in message


def test_r9a_format_grpc_detail_message_details_and_empty() -> None:
    """R9a helper: render message + details, and stay empty when neither is set.

    ``_format_grpc_detail`` appends the server-sent ``message`` and (when present)
    the structured ``details`` to the SpotError message, and returns an empty
    string when the GRPCError carries neither (so a status-only error reads
    cleanly).
    """
    with_message = grpclib.exceptions.GRPCError(
        Status.FAILED_PRECONDITION, "detail-text"
    )
    assert _spot._format_grpc_detail(with_message) == ": detail-text"

    with_details = grpclib.exceptions.GRPCError(
        Status.FAILED_PRECONDITION, "msg", ["extra-1", "extra-2"]
    )
    rendered = _spot._format_grpc_detail(with_details)
    assert "msg" in rendered
    assert "extra-1" in rendered

    status_only = grpclib.exceptions.GRPCError(Status.FAILED_PRECONDITION)
    assert _spot._format_grpc_detail(status_only) == ""


# ---------------------------------------------------------------------------
# R9b: a partial proto response logs the eid_info field shape at DEBUG, listing
# field NAMES only -- never raw values / decrypted bytes / a value sentinel.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r9b_proto_field_shape_logged_at_debug_without_value_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED (R9b): a missing-owner-key field error emits a DEBUG field-shape record.

    ``_retrieve_owner_key`` raises ``RuntimeError("Missing or empty
    'encryptedOwnerKey'...")`` when the server returns metadata without a usable
    owner-key blob. Before that raise, R9b adds a DEBUG record listing the
    available ``eid_info`` field names. The record must list a field name
    (``encryptedOwnerKey``) and must NOT contain an injected value sentinel /
    raw bytes. RED today: no such DEBUG field-shape record is emitted.
    """
    value_sentinel = "VALUE-SENTINEL-D00DFEED"

    metadata = SimpleNamespace(
        encryptedOwnerKey=b"",  # present field, empty value -> triggers the raise
        ownerKeyVersion=value_sentinel,  # a value that must never be logged
    )
    eid_info = SimpleNamespace(encryptedOwnerKeyAndMetadata=metadata)

    async def _eid_getter() -> Any:
        return eid_info

    async def _shared_getter() -> bytes:
        return b"\x11" * 32

    caplog.set_level(logging.DEBUG, logger=_gok.__name__)

    with pytest.raises(RuntimeError):
        await _gok._retrieve_owner_key(
            "user@acct.example",
            cache=_DummyCache(),
            eid_info_getter=_eid_getter,
            shared_key_getter=_shared_getter,
        )

    shape_records = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.DEBUG and "encryptedOwnerKey" in rec.getMessage()
    ]
    assert shape_records, "expected a DEBUG eid_info field-shape record"
    for rec in shape_records:
        assert value_sentinel not in rec.getMessage()


# ---------------------------------------------------------------------------
# R10 (E3-W): the SpotApiEmptyResponseError path is grounded in wording only;
# behavior (ConfigEntryAuthFailed at the outer layer) stays unchanged, and the
# new wording drops the speculative "auth/session issue" / "re-authenticate".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r10_outer_layer_still_raises_auth_failed_with_neutral_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R10 b1): ``async_get_owner_key`` still raises ``ConfigEntryAuthFailed``.

    E3-W keeps the reauth behavior at the outer layer deliberately (transient-vs-
    auth nature unproven), but the wording must be cause-neutral: the message must
    no longer claim ``auth/session issue`` or ``re-authenticate``. RED today: the
    ``:360-364`` branch raises ``ConfigEntryAuthFailed`` with exactly that
    speculative wording.

    Asserts against the ``ConfigEntryAuthFailed`` already present on ``_gok`` (the
    shared ``homeassistant.exceptions`` stub installed once by ``conftest``). It
    must NOT call ``install_config_entries_stubs(_gok)``: that rebinds the module
    global to a fresh stub class without restoring it, so a later test that catches
    the shared ``ConfigEntryAuthFailed`` would miss what ``_gok`` raises -- an
    order-dependent contamination. No module mutation here keeps the suite
    order-independent.
    """

    async def _inner_boom(*_a: object, **_k: object) -> Any:
        raise _eidreq.SpotApiEmptyResponseError("trailers only")

    monkeypatch.setattr(_gok, "_get_or_generate_user_owner_key_hex", _inner_boom)

    with pytest.raises(_gok.ConfigEntryAuthFailed) as excinfo:
        await _gok.async_get_owner_key(cache=_DummyCache(), username="x@acct.example")

    text = str(excinfo.value).lower()
    assert "auth/session issue" not in text
    assert "re-authenticate" not in text


@pytest.mark.asyncio
async def test_r10_inner_layer_reraises_empty_response_with_neutral_wording(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED (R10 b2): the inner retrieval re-raises the EMPTY-response type, not auth.

    ``_retrieve_owner_key`` must re-raise the ``SpotApiEmptyResponseError`` itself
    (the outer layer owns the ConfigEntryAuthFailed conversion), and the
    ``_LOGGER.error`` record must carry the new cause-neutral wording (no
    ``auth/session issue``). The ``except SpotApiEmptyResponseError`` re-raise
    sits in the default branch (``async_get_eid_info``), so no ``eid_info_getter``
    is injected; the module-level fetch is mocked instead. RED today: the
    ``:166-171`` record still says "(likely auth/session issue). Please
    re-authenticate."
    """
    monkeypatch.setattr(
        _gok,
        "async_get_eid_info",
        AsyncMock(side_effect=_eidreq.SpotApiEmptyResponseError("trailers only")),
    )

    async def _shared_getter() -> bytes:
        return b"\x11" * 32

    caplog.set_level(logging.ERROR, logger=_gok.__name__)

    with pytest.raises(_eidreq.SpotApiEmptyResponseError):
        await _gok._retrieve_owner_key(
            "user@acct.example",
            cache=_DummyCache(),
            shared_key_getter=_shared_getter,
        )

    error_text = " ".join(
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR
    ).lower()
    assert "auth/session issue" not in error_text
