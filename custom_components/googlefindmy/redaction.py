# custom_components/googlefindmy/redaction.py
"""Dependency-free redaction helpers shared by diagnostics and the CLI flows.

Why this module exists
----------------------
``async_redact_data`` used to live in :mod:`diagnostics`, which imports
``homeassistant.config_entries``, ``homeassistant.core``, the device and entity
registries and ``homeassistant.loader`` at module level. The manual key-backup
flow (:mod:`KeyBackup.shared_key_flow`) runs in the *command-line* process, well
outside Home Assistant; importing the diagnostics module from there would pull
half of the Home Assistant load chain into a login run.

This module therefore imports nothing beyond the standard library. Keep it that
way: it is imported from both runtimes.

Note on ``@callback``: the diagnostics copy carried Home Assistant's
``@callback`` marker. The marker is metadata for Home Assistant's job scheduler
and is never consulted for this helper (it is only ever called inline), so it is
dropped here rather than dragging ``homeassistant.core`` back in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sized
from typing import Any, cast

# Consistent placeholder used when redacting fields.
REDACTED = "**REDACTED**"


def async_redact_data[T](data: T, to_redact: Iterable[Any]) -> T:
    """Redact sensitive keys from mappings or lists without importing HA's HTTP stack."""

    if not isinstance(data, (Mapping, list)):
        return data

    if isinstance(data, list):
        return cast(T, [async_redact_data(item, to_redact) for item in data])

    redacted = dict(data)

    for key, value in list(redacted.items()):
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if key in to_redact:
            redacted[key] = REDACTED
        elif isinstance(value, Mapping):
            redacted[key] = async_redact_data(value, to_redact)
        elif isinstance(value, list):
            redacted[key] = [async_redact_data(item, to_redact) for item in value]

    return cast(T, redacted)


def describe_keys(value: Any) -> str:
    """Describe a mapping by its key names only, never by its values.

    For a payload whose shape is *unknown* (a malformed or unrecognised
    response), redacting by key name is not enough: the sensitive field may sit
    under a name nobody anticipated. Key names are metadata and are what a
    maintainer needs; the values are what must not be logged.
    """

    if isinstance(value, Mapping):
        keys = ", ".join(sorted(str(key) for key in value))
        return f"keys=[{keys}]"
    return describe_payload(value)


def describe_payload(value: Any) -> str:
    """Describe a payload by type and size, never by content.

    Used where a value cannot be key-redacted because it is not a mapping (a raw
    alert string, for example). A maintainer needs to tell "absent" from "empty"
    from "wrong shape"; none of that requires the bytes themselves.
    """

    type_name = type(value).__name__
    if value is None:
        return "None"
    if isinstance(value, Sized):
        try:
            return f"{type_name} len={len(value)}"
        except Exception:  # pragma: no cover - a broken __len__ must not break logging
            return type_name
    return type_name
