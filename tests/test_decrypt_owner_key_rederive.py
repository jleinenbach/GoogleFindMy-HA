# tests/test_decrypt_owner_key_rederive.py
"""Owner-key RE-DERIVE flow on a local structure defect (v3 R1-R4).

When the INITIAL owner-key lookup reports a LOCAL structure defect (the stored
owner key is missing/invalid, malformed, or the wrong length), the v3 caller must
RE-DERIVE the owner key once via ``async_get_owner_key(force_refresh=True)``
before deciding anything -- never escalate the first defect straight to reauth.

These integration tests drive ``async_retrieve_identity_key`` and monkeypatch the
module-level ``async_get_owner_key`` with a fake that:
  * raises the structure-defect ``RuntimeError`` on the initial (force_refresh=False)
    call, and
  * counts and answers the force_refresh=True call distinctly,
mirroring the existing discriminator integration tests
(``test_owner_key_forced_refresh_invalid_tag_maps_to_mismatch`` and
``test_r3_initial_lookup_spot_error_maps_to_transient``).

They go RED on HEAD because the re-derive-on-structure-defect path does not exist
yet: today the initial structure defect is classified by
``_classify_owner_key_failure`` straight into ``OwnerKeyInvalidError`` (a
reauth-worthy ``DecryptionError``), so ``force_refresh=True`` is never reached
(R1/R4 counter stays 0) and R2/R3 never get a chance to map the refresh outcome.
"""

from __future__ import annotations

import logging

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as _dl,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyInvalidError,
    OwnerKeyLookupTransientError,
    SharedKeyMismatchError,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
)
from custom_components.googlefindmy.SpotApi.spot_request import SpotGrpcStatusError

pytestmark = pytest.mark.asyncio

# The deterministic local structure-defect wording raised by the initial lookup.
# Matches ``get_owner_key.py``'s "Owner key is missing or invalid" branch -- the
# (b2) structure-defect anchor in ``_classify_owner_key_failure``.
_STRUCTURE_DEFECT_MSG = "Owner key is missing or invalid; please re-authenticate."


def _registration() -> object:
    """Build a DeviceRegistration with a 60-byte garbage encrypted identity key."""
    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60
    return registration


class _RederiveFake:
    """Fake ``async_get_owner_key``: structure defect first, scripted on refresh.

    The initial call (``force_refresh`` falsey) raises the structure-defect
    ``RuntimeError``. The ``force_refresh=True`` call is counted and dispatched to
    ``on_refresh`` (a callable returning the ``OwnerKeyInfo`` or raising).
    """

    def __init__(self, on_refresh: object) -> None:
        self._on_refresh = on_refresh
        self.force_refresh_calls = 0

    async def __call__(
        self, *, cache: object, force_refresh: bool = False, **_kw: object
    ) -> OwnerKeyInfo:
        if force_refresh:
            self.force_refresh_calls += 1
            return self._on_refresh()  # type: ignore[operator,no-any-return]
        raise RuntimeError(_STRUCTURE_DEFECT_MSG)


async def test_r1_structure_defect_rederives_once_without_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R1): initial structure defect triggers exactly one force_refresh; the
    refreshed (valid) owner key keeps the flow OFF the reauth path.

    The refresh returns a valid ``OwnerKeyInfo`` so the owner-key phase succeeds;
    the garbage EIK still fails to decrypt, but NOT as a credential defect
    (``SharedKeyMismatchError`` / ``OwnerKeyInvalidError``). We pin the re-derive
    contract precisely: ``force_refresh=True`` is invoked exactly once, and no
    owner-key-path ``DecryptionError`` subclass leaks. RED today: the initial
    defect is classified straight to ``OwnerKeyInvalidError`` and the refresh is
    never reached (counter stays 0).
    """

    def _valid_refresh() -> OwnerKeyInfo:
        return OwnerKeyInfo(key=b"\x00" * 32, version=1)

    fake = _RederiveFake(_valid_refresh)
    monkeypatch.setattr(_dl, "async_get_owner_key", fake)

    # The decrypt of the garbage EIK still fails; assert it is NOT routed to a
    # reauth-worthy credential defect from the owner-key path.
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - shape asserted below
        await _dl.async_retrieve_identity_key(_registration(), cache=object())

    assert fake.force_refresh_calls == 1
    assert not isinstance(excinfo.value, SharedKeyMismatchError)
    assert not isinstance(excinfo.value, OwnerKeyInvalidError)


async def test_r2_structure_defect_then_invalid_tag_maps_to_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R2): initial structure defect, then the force_refresh raises InvalidTag
    => ``SharedKeyMismatchError`` (the shared key cannot decrypt the owner key).

    After the re-derive carve-out lands, the refresh failure is classified exactly
    like the existing forced-refresh InvalidTag path. RED today: the initial defect
    short-circuits to ``OwnerKeyInvalidError`` before any refresh runs.
    """

    def _refresh_invalid_tag() -> OwnerKeyInfo:
        raise InvalidTag()

    fake = _RederiveFake(_refresh_invalid_tag)
    monkeypatch.setattr(_dl, "async_get_owner_key", fake)

    with pytest.raises(SharedKeyMismatchError):
        await _dl.async_retrieve_identity_key(_registration(), cache=object())

    assert fake.force_refresh_calls == 1


async def test_r3_structure_defect_then_spot_error_maps_to_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (R3): initial structure defect, then the force_refresh raises a typed
    ``SpotError`` => ``OwnerKeyLookupTransientError`` (transient transport failure),
    text carries the "transient" marker, never stale/reauth.

    RED today: the initial defect short-circuits to ``OwnerKeyInvalidError`` before
    any refresh runs.
    """

    def _refresh_spot_error() -> OwnerKeyInfo:
        raise SpotGrpcStatusError("gRPC error: UNAVAILABLE")

    fake = _RederiveFake(_refresh_spot_error)
    monkeypatch.setattr(_dl, "async_get_owner_key", fake)

    with pytest.raises(OwnerKeyLookupTransientError) as excinfo:
        await _dl.async_retrieve_identity_key(_registration(), cache=object())

    assert fake.force_refresh_calls == 1
    text = str(excinfo.value).lower()
    assert "transient" in text
    assert "stale" not in text
    assert "re-authenticate" not in text


async def test_r4_structure_defect_persists_after_refresh_is_transient(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED (R4 default): the structure defect REPEATS on force_refresh => transient.

    When even the re-derived owner key reports the same structure defect, the flow
    must NOT escalate to reauth: it surfaces as ``OwnerKeyLookupTransientError`` and
    emits exactly one WARNING record (the re-derive-exhausted notice). RED today:
    the initial defect short-circuits to ``OwnerKeyInvalidError`` (reauth), and no
    such WARNING is emitted.
    """

    def _refresh_still_defective() -> OwnerKeyInfo:
        raise RuntimeError(_STRUCTURE_DEFECT_MSG)

    fake = _RederiveFake(_refresh_still_defective)
    monkeypatch.setattr(_dl, "async_get_owner_key", fake)

    caplog.set_level(logging.WARNING, logger=_dl.__name__)

    with pytest.raises(OwnerKeyLookupTransientError):
        await _dl.async_retrieve_identity_key(_registration(), cache=object())

    assert fake.force_refresh_calls == 1
    warning_records = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warning_records) == 1
    # The structure defect must never be re-routed to a DecryptionError/reauth.
    transient_exc = OwnerKeyLookupTransientError
    assert not issubclass(transient_exc, DecryptionError)


async def test_error_kind_auth_failure_surfaces_as_reauth_from_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1 integration: a non-retryable auth error (structured ``error_kind``) raised
    on the initial owner-key lookup surfaces from ``async_retrieve_identity_key`` as
    the reauth-worthy ``OwnerKeyInvalidError``.

    Defect 2 hinges on a passed-through bare ``RuntimeError`` that carries
    ``error_kind`` (e.g. ``badauthentication`` from the ADM/AAS token exchange)
    reaching ``_classify_owner_key_failure`` via the ``except (InvalidTag,
    RuntimeError)`` block. Unlike the (b2) structure-defect wordings this is NOT
    re-derivable: the (auth) branch must classify it reauth-worthy, so the caller
    re-raises it (no force_refresh, no transient swallow). This exercises the real
    propagation path the unit tests in ``test_decrypt_owner_key_authclass.py``
    only cover at the classifier boundary.
    """

    auth_exc = RuntimeError(
        "Missing 'Token'/'Auth' in gpsoauth response (kind=badauthentication)"
    )
    auth_exc.error_kind = "badauthentication"  # type: ignore[attr-defined]

    call_count = {"n": 0}

    async def _auth_boom(
        *, cache: object, force_refresh: bool = False, **_kw: object
    ) -> object:
        call_count["n"] += 1
        raise auth_exc

    monkeypatch.setattr(_dl, "async_get_owner_key", _auth_boom)

    with pytest.raises(OwnerKeyInvalidError) as excinfo:
        await _dl.async_retrieve_identity_key(_registration(), cache=object())

    # Reauth-worthy DecryptionError, never the transient miss; no re-derive attempted.
    assert isinstance(excinfo.value, DecryptionError)
    assert not isinstance(excinfo.value, OwnerKeyLookupTransientError)
    assert call_count["n"] == 1
    assert "re-authentication" in str(excinfo.value).lower()
