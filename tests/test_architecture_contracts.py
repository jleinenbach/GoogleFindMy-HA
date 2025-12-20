# tests/test_architecture_contracts.py
from __future__ import annotations

import inspect
from dataclasses import fields

import pytest

from custom_components.googlefindmy import eid_resolver
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.ProtoDecoders.decoder import _DEVICE_STUB_KEYS


def test_decoder_exports_required_keys() -> None:
    """Ensure the decoder promises to emit required anchor keys."""

    required_keys = {
        "pair_date",
        "secrets_creation_date",
        "manufacturer",
        "model",
        "encrypted_account_key",
        "public_key_address",
    }
    missing_keys = required_keys.difference(_DEVICE_STUB_KEYS)

    assert not missing_keys, (
        "CRITICAL CONTRACT VIOLATION: The decoder is no longer exporting "
        "`pair_date`, `secrets_creation_date`, or device metadata keys. The "
        "EID resolver relies on these keys. Please add them back to "
        "`_DEVICE_STUB_KEYS` in "
        "`decoder.py`."
    )


def test_device_identity_definition() -> None:
    """Ensure DeviceIdentity can carry required anchor metadata."""

    required_fields = {
        "pair_date",
        "secrets_creation_date",
        "manufacturer",
        "model",
        "encrypted_account_key",
        "public_key_address",
    }
    available_fields = tuple(field.name for field in fields(DeviceIdentity))
    missing = required_fields.difference(available_fields)

    assert not missing, (
        "CRITICAL SCHEMA MISMATCH: `DeviceIdentity` in `coordinator.py` is "
        "missing metadata fields. The Resolver cannot access "
        "`pair_date`, `secrets_creation_date`, `manufacturer`, `model`, or "
        "encrypted keys without them. Update the NamedTuple definition."
    )


def test_decoder_output_fits_identity() -> None:
    """Ensure decoder output keys align with DeviceIdentity constructor."""

    stub_payload = {key: None for key in _DEVICE_STUB_KEYS}
    identity_fields = {field.name for field in fields(DeviceIdentity)}
    shared_fields = identity_fields.intersection(stub_payload)

    base_kwargs = {
        "registry_id": "registry-id",
        "canonical_id": "canonical-id",
        "identity_key": b"",
    }
    identity_kwargs = {key: stub_payload[key] for key in shared_fields if key not in base_kwargs}
    kwargs = {**base_kwargs, **identity_kwargs}

    signature = inspect.signature(DeviceIdentity)
    unexpected = set(identity_kwargs).difference(signature.parameters)
    assert not unexpected, (
        "INTEGRATION BREAKAGE: The keys provided by `decoder.py` do not match "
        "the arguments expected by `DeviceIdentity` in `coordinator.py`. You "
        "renamed a key in one place but not the other."
    )

    try:
        DeviceIdentity(**kwargs)
    except TypeError:  # pragma: no cover - loud fail path
        pytest.fail(
            "INTEGRATION BREAKAGE: The keys provided by `decoder.py` do not "
            "match the arguments expected by `DeviceIdentity` in "
            "`coordinator.py`. You renamed a key in one place but not the "
            "other."
        )


def test_resolver_safety_constants_exist() -> None:
    """Ensure Deep Scan safety constants remain present and conservative."""

    value = getattr(eid_resolver, "MIN_UNIX_WINDOW_SIZE", None)
    assert isinstance(value, int) and value >= 128, (
        "CRITICAL SAFETY: The Deep Scan constant `MIN_UNIX_WINDOW_SIZE` is "
        "missing or dangerously small."
    )
