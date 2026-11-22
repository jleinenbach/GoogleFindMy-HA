from __future__ import annotations

import ast
import asyncio
import inspect
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy import eid_resolver
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.FMDNCrypto._lazy_crypto import (
    get_aesgcm_class,
    get_invalid_tag_exception,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as _dl,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    SharedKeyMismatchError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_owner_key as _gok,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    begin_owner_key_poll_cycle,
    claim_owner_key_spot_refresh,
    consume_resolved_owner_key_versions,
    open_owner_key_version_gaps,
    owner_key_scope,
    record_owner_key_version_gap,
)
from tests.helpers import DummyCache
from tests.helpers.owner_key_vault_fixtures import (
    OWNER_KEY_VERSIONS_FIELD,
    make_vault_world,
)


@pytest.mark.asyncio
async def test_try_decrypt_identity_key_refreshes_owner_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DummyCache()
    calls: list[bool] = []
    decrypt_inputs: list[bytes] = []

    async def fake_async_get_owner_key(
        *, cache: object, force_refresh: bool = False, **kwargs
    ):  # type: ignore[no-untyped-def]
        calls.append(force_refresh)
        version = 2 if force_refresh else 1
        key = bytes([version]) * 32
        return eid_resolver.OwnerKeyInfo(key=key, version=version)

    def fake_decrypt_eik(owner_key: bytes, encrypted_identity_key: bytes) -> bytes:  # noqa: ARG001
        decrypt_inputs.append(owner_key)
        return b"decoded"

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "decrypt_eik", fake_decrypt_eik)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)
    monkeypatch.setattr(eid_resolver, "flip_bits", lambda data, _: data)

    identity = DeviceIdentity(
        registry_id="reg-1",
        canonical_id="can-1",
        identity_key=None,
        encrypted_identity_key=b"\x01" * 32,
        owner_key_version=2,
        device_type=None,
        config_entry_id="entry-1",
    )

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == b"decoded"
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["key_source"] == "owner"
    assert calls == [False, True]
    assert decrypt_inputs == [bytes([2]) * 32]


@pytest.mark.asyncio
async def test_try_decrypt_identity_key_unwraps_aes_gcm_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DummyCache()
    key = b"\x02" * 32
    nonce = b"\x00" * 12
    plaintext = b"\x99" * 32
    aad = b"reg-wrap"
    AESGCM = get_aesgcm_class()
    InvalidTag = get_invalid_tag_exception()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = nonce + ciphertext

    async def fake_async_get_owner_key(*, cache: object, **kwargs):  # type: ignore[no-untyped-def]
        return eid_resolver.OwnerKeyInfo(key=key, version=None)

    async def fake_async_get_shared_key(*, cache: object, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("shared key unavailable")

    def fake_decrypt_eik(owner_key: bytes, encrypted_identity_key: bytes) -> bytes:  # noqa: ARG001
        raise InvalidTag("wrapped")

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "async_get_shared_key", fake_async_get_shared_key)
    monkeypatch.setattr(eid_resolver, "decrypt_eik", fake_decrypt_eik)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)
    monkeypatch.setattr(eid_resolver, "flip_bits", lambda data, _: data)

    identity = DeviceIdentity(
        registry_id="reg-wrap",
        canonical_id="can-wrap",
        identity_key=None,
        encrypted_identity_key=envelope,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-wrap",
    )

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == plaintext
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["mode"] == "aesgcm_envelope"
    assert result.metadata["aad_label"] == "registry_id"
    assert result.metadata["key_source"] == "owner"


@pytest.mark.asyncio
async def test_try_decrypt_identity_key_gap_uses_vault_entry_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #246: tracker version 2, Spot serves 1, vault entry 2 in the bundle.

    The resolver must take the owner key of version 2 from the bundle field
    instead of forcing a Spot refresh. Spot keeps serving version 1 even on
    ``force_refresh``, which is the situation of the report. The shared key sits
    in the entry cache, so the real retrieval returns it through any binding.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    cache = DummyCache()
    world.seed_entry_cache(cache.values, world.bundle_field)
    assert OWNER_KEY_VERSIONS_FIELD in cache.values
    calls: list[bool] = []

    async def fake_async_get_owner_key(
        *,
        cache: object,
        username: str | None = None,
        eid_info_getter: object | None = None,
        shared_key_getter: object | None = None,
        force_refresh: bool = False,
    ) -> eid_resolver.OwnerKeyInfo:
        calls.append(force_refresh)
        return eid_resolver.OwnerKeyInfo(key=world.owner_keys[1], version=1)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)

    identity = DeviceIdentity(
        registry_id="reg-gap",
        canonical_id="can-gap",
        identity_key=None,
        encrypted_identity_key=world.encrypted_eik(2),
        owner_key_version=2,
        device_type=None,
        config_entry_id="entry-gap",
    )

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result is not None
    assert result.key == world.eiks[2]
    assert calls == [False]


# ---------------------------------------------------------------------------
# AP10: the resolver uses its own clock claim, never the poll-cycle claim (F5)
# ---------------------------------------------------------------------------

_CYCLE_CLAIM = "claim_owner_key_spot_refresh"


def _cycle_claim_hits(source: str) -> list[str]:
    """Return every reference to the poll-cycle claim in ``source`` (exact name)."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == _CYCLE_CLAIM:
            hits.append(f"name:{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr == _CYCLE_CLAIM:
            hits.append(f"attribute:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            hits.extend(
                f"import:{node.lineno}"
                for alias in node.names
                if alias.name == _CYCLE_CLAIM
            )
    return hits


def _gap_identity(world: Any, *, version: int = 2) -> DeviceIdentity:
    return DeviceIdentity(
        registry_id="reg-gap",
        canonical_id="can-gap",
        identity_key=None,
        encrypted_identity_key=world.encrypted_eik(version),
        owner_key_version=version,
        device_type=None,
        config_entry_id="entry-gap",
    )


def _install_invalid_tag_refresh(
    monkeypatch: pytest.MonkeyPatch, world: Any
) -> list[bool]:
    """Spot serves version 1; a forced refresh fails with ``InvalidTag``."""
    forced: list[bool] = []

    async def fake_async_get_owner_key(
        *,
        cache: object,
        username: str | None = None,
        eid_info_getter: object | None = None,
        shared_key_getter: object | None = None,
        force_refresh: bool = False,
    ) -> eid_resolver.OwnerKeyInfo:
        if force_refresh:
            forced.append(True)
            raise InvalidTag()
        return eid_resolver.OwnerKeyInfo(key=world.owner_keys[1], version=1)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(_dl, "async_get_owner_key", fake_async_get_owner_key)
    return forced


def _field_less_cache(world: Any) -> DummyCache:
    cache = DummyCache()
    world.seed_entry_cache(cache.values, None)
    assert OWNER_KEY_VERSIONS_FIELD not in cache.values
    return cache


@pytest.mark.asyncio
async def test_resolver_refresh_does_not_consume_poll_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F5: a resolver refresh that fails leaves the cycle's claim to the poll."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    forced = _install_invalid_tag_refresh(monkeypatch, world)
    cache = _field_less_cache(world)
    begin_owner_key_poll_cycle(owner_key_scope(cache))

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    info = await resolver._resolve_owner_key(_gap_identity(world), cache=cache)

    assert info is not None
    assert info.version == 1
    assert forced == [True]
    assert claim_owner_key_spot_refresh(cache) is True


@pytest.mark.asyncio
async def test_resolver_invalid_tag_then_poll_raises_shared_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F5: after the resolver swallowed ``InvalidTag``, the poll still refreshes.

    With the claim intact the poll's forced refresh meets the same rejection and
    reports the shared-key defect; a consumed claim would skip the refresh and
    report the version gap instead.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    forced = _install_invalid_tag_refresh(monkeypatch, world)
    cache = _field_less_cache(world)
    begin_owner_key_poll_cycle(owner_key_scope(cache))

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    await resolver._resolve_owner_key(_gap_identity(world), cache=cache)

    with pytest.raises(SharedKeyMismatchError):
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )
    assert forced == [True, True]


def test_resolver_never_takes_cycle_claim() -> None:
    """F5, N4: ``eid_resolver.py`` never names the poll-cycle claim."""
    assert _cycle_claim_hits(inspect.getsource(eid_resolver)) == []


def test_cycle_claim_ast_detector_controls() -> None:
    """N4: the detector finds call, attribute and aliased import; exact name only."""
    path = (
        "custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key"
    )
    assert _cycle_claim_hits("claim_owner_key_spot_refresh(cache)\n")
    assert _cycle_claim_hits("gok.claim_owner_key_spot_refresh(cache)\n")
    assert _cycle_claim_hits(
        f"from {path} import claim_owner_key_spot_refresh as _c\n_c(cache)\n"
    )
    assert (
        _cycle_claim_hits(
            f"from {path} import claim_owner_key_spot_refresh_by_clock\n"
            "claim_owner_key_spot_refresh_by_clock(cache)\n"
        )
        == []
    )


@pytest.mark.asyncio
async def test_resolver_clock_claim_throttles_spot_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N3, L-4: one forced refresh per 300 s by the resolver's clock."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    now = [1_000.0]
    monkeypatch.setattr(_gok, "_monotonic", lambda: now[0])
    forced: list[bool] = []

    async def fake_async_get_owner_key(
        *, cache: object, force_refresh: bool = False, **_kwargs: Any
    ) -> eid_resolver.OwnerKeyInfo:
        if force_refresh:
            forced.append(True)
        return eid_resolver.OwnerKeyInfo(key=world.owner_keys[1], version=1)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    cache = _field_less_cache(world)
    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    identity = _gap_identity(world)

    await resolver._resolve_owner_key(identity, cache=cache)
    await resolver._resolve_owner_key(identity, cache=cache)
    assert len(forced) == 1

    now[0] += 300.0
    await resolver._resolve_owner_key(identity, cache=cache)
    assert len(forced) == 2


def _plain_success(monkeypatch: pytest.MonkeyPatch) -> DeviceIdentity:
    """Owner key already at version 2; ``decrypt_eik`` succeeds (plain return)."""

    async def fake_async_get_owner_key(
        *, cache: object, force_refresh: bool = False, **_kwargs: Any
    ) -> eid_resolver.OwnerKeyInfo:
        return eid_resolver.OwnerKeyInfo(key=b"\x02" * 32, version=2)

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "decrypt_eik", lambda *_args: b"decoded")
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)
    monkeypatch.setattr(eid_resolver, "flip_bits", lambda data, _: data)
    return DeviceIdentity(
        registry_id="reg-1",
        canonical_id="can-1",
        identity_key=None,
        encrypted_identity_key=b"\x01" * 32,
        owner_key_version=2,
        device_type=None,
        config_entry_id="entry-1",
    )


def _envelope_success(monkeypatch: pytest.MonkeyPatch) -> DeviceIdentity:
    """Owner key already at version 2; the AES-GCM envelope opens (envelope return)."""
    key = b"\x02" * 32
    nonce = b"\x00" * 12
    aesgcm = get_aesgcm_class()
    tag_error = get_invalid_tag_exception()
    envelope = nonce + aesgcm(key).encrypt(nonce, b"\x99" * 32, b"reg-wrap")

    async def fake_async_get_owner_key(
        *, cache: object, force_refresh: bool = False, **_kwargs: Any
    ) -> eid_resolver.OwnerKeyInfo:
        return eid_resolver.OwnerKeyInfo(key=key, version=2)

    async def fake_async_get_shared_key(*, cache: object, **_kwargs: Any) -> bytes:
        raise RuntimeError("shared key unavailable")

    def fake_decrypt_eik(*_args: Any) -> bytes:
        raise tag_error("wrapped")

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "async_get_shared_key", fake_async_get_shared_key)
    monkeypatch.setattr(eid_resolver, "decrypt_eik", fake_decrypt_eik)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)
    monkeypatch.setattr(eid_resolver, "flip_bits", lambda data, _: data)
    return DeviceIdentity(
        registry_id="reg-wrap",
        canonical_id="can-wrap",
        identity_key=None,
        encrypted_identity_key=envelope,
        owner_key_version=2,
        device_type=None,
        config_entry_id="entry-wrap",
    )


@pytest.mark.parametrize("path", ["plain", "envelope"])
@pytest.mark.asyncio
async def test_resolver_success_records_open_gap_version(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """N2: a resolver success moves an open gap version to resolved, both returns."""
    identity = (
        _plain_success(monkeypatch)
        if path == "plain"
        else _envelope_success(monkeypatch)
    )
    cache = DummyCache()
    record_owner_key_version_gap(cache, 2)

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result is not None
    scope = owner_key_scope(cache)
    assert open_owner_key_version_gaps(scope) == ()
    assert consume_resolved_owner_key_versions(scope) == (2,)


@pytest.mark.asyncio
async def test_resolver_success_without_open_gap_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N2: without an open gap a success records nothing."""
    identity = _plain_success(monkeypatch)
    cache = DummyCache()

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result is not None
    assert consume_resolved_owner_key_versions(owner_key_scope(cache)) == ()
