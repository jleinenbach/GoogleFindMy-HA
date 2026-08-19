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

import re
from collections.abc import Iterable, Mapping, Sized
from typing import Any, cast

# Consistent placeholder used when redacting fields.
REDACTED = "**REDACTED**"

# The token cache builds key *names* from the account address
# (``adm_token_<e-mail>``, ``aas_token_issued_at_<e-mail>``). Redacting the value
# leaves the address standing in the property name, in a file people attach to
# public issues. The address is replaced in the name as well; the rest of the
# name is kept, because ``issued_at`` is what makes the entry readable.
# Two passes, because guessing where a name ends and an address begins cannot
# be done safely. First the addresses actually present in the payload are
# removed from key names verbatim, which keeps the readable part of the name
# (``aas_token_issued_at_...``) intact even for a local part containing an
# underscore. Whatever still looks like an address afterwards is removed
# greedily: correctness of the redaction outranks readability of the key.
#
# The two patterns are deliberately not the same. The first anchors on the
# whole string, so it can accept every character a local part may carry,
# `/` among them (`first/last@example.com` is a valid address and does occur
# on hosted domains). The second has to find an address *inside* a longer
# name and stops at `/` and a backslash on purpose, so a path-shaped key name
# cannot be swallowed whole.
_EMAIL_VALUE = re.compile(r"^[^\s@]+@[^\s@/\\]+\.[^\s@/\\]+$")
_EMAIL_IN_KEY = re.compile(r"[^\s@/\\]+@[^\s@/\\]+\.[^\s@/\\]+")


def async_redact_data[T](
    data: T,
    to_redact: Iterable[Any],
    to_redact_prefixes: Iterable[str] = (),
    _accounts: dict[str, str] | None = None,
) -> T:
    """Redact sensitive keys from mappings or lists without importing HA's HTTP stack.

    ``to_redact`` matches key names exactly. ``to_redact_prefixes`` exists for the
    keys whose names are built at runtime and can therefore never appear in a
    fixed list: the token cache stores entries such as ``adm_token_<e-mail>`` and
    ``android_id_<e-mail>``. Without a prefix rule those names pass an exact-match
    filter untouched.
    """

    if not isinstance(data, (Mapping, list)):
        return data

    accounts = {} if _accounts is None else _accounts
    if _accounts is None:
        _register_addresses(data, accounts)

    if isinstance(data, list):
        return cast(
            T,
            [
                async_redact_data(item, to_redact, to_redact_prefixes, accounts)
                for item in data
            ],
        )

    prefixes = tuple(to_redact_prefixes)
    redacted: dict[Any, Any] = {}

    for key, value in dict(data).items():
        out_key = _anonymise_key(key, accounts)
        while out_key != key and out_key in redacted:
            out_key = f"{out_key}-2"
        if value is None or (isinstance(value, str) and not value):
            redacted[out_key] = value
            continue
        if key in to_redact or (
            prefixes and isinstance(key, str) and key.startswith(prefixes)
        ):
            redacted[out_key] = REDACTED
        elif isinstance(value, Mapping):
            redacted[out_key] = async_redact_data(value, to_redact, prefixes, accounts)
        elif isinstance(value, list):
            redacted[out_key] = [
                async_redact_data(item, to_redact, prefixes, accounts) for item in value
            ]
        else:
            redacted[out_key] = value

    return cast(T, redacted)


def _register_addresses(data: Any, accounts: dict[str, str]) -> None:
    """Collect the e-mail addresses that appear as *values*, before redaction.

    The key names are built from the account address, so knowing the address
    lets the name be cleaned exactly instead of by pattern guessing. Run once,
    on the whole payload, before any value has been replaced.
    """

    if isinstance(data, Mapping):
        for value in data.values():
            _register_addresses(value, accounts)
        return
    if isinstance(data, list):
        for item in data:
            _register_addresses(item, accounts)
        return
    if isinstance(data, str) and _EMAIL_VALUE.match(data) and data not in accounts:
        accounts[data] = f"<account-{len(accounts) + 1}>"


def _anonymise_key(key: Any, accounts: dict[str, str]) -> Any:
    """Replace an account address inside a key *name* with a stable placeholder.

    Numbered rather than hashed: a hash of an e-mail address is reversible with
    a word list, and the only thing a reader needs from the name is whether two
    entries belong to the same account.
    """

    if not isinstance(key, str) or "@" not in key:
        return key

    # Longest first, so a shorter address that is a suffix of a longer one
    # cannot claim the match.
    for address in sorted(accounts, key=len, reverse=True):
        if address in key:
            key = key.replace(address, accounts[address])
            if "@" not in key:
                return key

    def _replace(match: re.Match[str]) -> str:
        address = match.group(0)
        if address not in accounts:
            accounts[address] = f"<account-{len(accounts) + 1}>"
        return accounts[address]

    return _EMAIL_IN_KEY.sub(_replace, key)


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
