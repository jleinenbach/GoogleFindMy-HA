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
#
# Four rules, tried in this order, because guessing where a name ends and an
# address begins cannot be done safely in general:
#
# 1. The token cache's own shape, ``[<namespace>:]<stem><address>``. Here the
#    boundary is known, so the whole rest after the stem is the address,
#    whatever characters it carries (``first/last@example.com`` included).
#    Whatever stands before the stem goes through all four rules itself.
# 2. Addresses that occur as *values* in the payload, replaced verbatim, but
#    only where they cannot be the tail of a longer address: at the start of
#    the name, after ``:`` or after white space. ``_`` and ``/`` are not
#    boundaries here, because both may belong to a local part
#    (``seen_a_b@`` may be the address ``a_b@``); such names go to rule 3.
#    On the right, no further address character may follow, so ``example.co``
#    is not matched inside ``example.com``.
# 3. Whatever still looks like an address is removed greedily, ``/`` and quoted
#    local parts included: correctness of the redaction outranks readability
#    of the key, so a name without a stem may be replaced whole.
# 4. Any word with an ``@`` left after that is replaced as well, backslashes
#    included, so that no domain or local part survives a name that held two
#    addresses or an address the patterns above do not read.
#
# A local part is a run of quoted segments, plain characters, and a lone
# ``"`` that has no partner further right. Inside a quoted segment a
# backslash and the character after it form one pair (RFC 5322 quoted-pair),
# so ``"first\" last"`` is one segment. The alternatives cannot match the same
# text, which keeps the pattern free of exponential backtracking.
_LOCAL_PART = r'(?:"(?:[^"\\]|\\.)*"|"(?![^"]*")|[^\s@\\"])+'
_EMAIL_VALUE = re.compile(r"^" + _LOCAL_PART + r"@[^\s@/\\]+\.[^\s@/\\]+$")
_EMAIL_IN_KEY = re.compile(_LOCAL_PART + r"@[^\s@/\\]+\.[^\s@/\\]+")
_AT_WORD = re.compile(r"[^\s@]*@[^\s@]*")

# Stems the token cache puts in front of the account address when it builds a
# key name at run time (`f"adm_token_{user}"` and friends, spread over the
# `Auth`, `NovaApi` and top-level modules). A guard test walks the package and
# fails when such a stem appears there without being listed here. Longest
# first, so that `adm_token_issued_at_` is tried before `adm_token_`.
RUNTIME_KEY_STEMS: tuple[str, ...] = tuple(
    sorted(
        (
            "aas_best_ttl_sec_",
            "aas_token_issued_at_",
            "adm_best_ttl_sec_",
            "adm_probe_armed_",
            "adm_probe_next_at_",
            "adm_probe_startup_left_",
            "adm_token_",
            "adm_token_issued_at_",
            "android_id_",
            "owner_key_",
            "shared_key_",
            "spot_token_",
        ),
        key=len,
        reverse=True,
    )
)


def _name_starts(key: str) -> list[int]:
    """Return 0 and every position right after a ``:`` in the key.

    The token cache may namespace its keys (``<namespace>:adm_token_<e-mail>``),
    and a quoted local part may itself contain ``:``. Splitting at one fixed
    colon gets one of the two wrong, so every remainder is a candidate; the
    rules can only redact more, never less, than with the key alone.
    """

    return [0] + [index + 1 for index, char in enumerate(key) if char == ":"]


def _name_candidates(key: Any) -> list[Any]:
    """Return the key and every remainder after a ``:`` in it."""

    if not isinstance(key, str):
        return [key]
    return [key[start:] for start in _name_starts(key)]


def async_redact_data[T](
    data: T,
    to_redact: Iterable[Any],
    to_redact_prefixes: Iterable[str] = (),
    accounts: dict[str, str] | None = None,
) -> T:
    """Redact sensitive keys from mappings or lists without importing HA's HTTP stack.

    ``to_redact`` matches key names exactly. ``to_redact_prefixes`` exists for the
    keys whose names are built at runtime and can therefore never appear in a
    fixed list: the token cache stores entries such as ``adm_token_<e-mail>`` and
    ``android_id_<e-mail>``. Without a prefix rule those names pass an exact-match
    filter untouched.

    ``accounts`` shares the account numbering between calls. A caller that
    redacts one part of a document first and the whole document afterwards
    passes the same mapping to both, so that ``<account-1>`` means the same
    account everywhere in the result.
    """

    if not isinstance(data, (Mapping, list, tuple)):
        return data

    shared: dict[str, str] = {} if accounts is None else accounts
    _register_addresses(data, shared)
    return cast(T, _redact(data, to_redact, tuple(to_redact_prefixes), shared))


def _redact(
    data: Any,
    to_redact: Iterable[Any],
    prefixes: tuple[str, ...],
    accounts: dict[str, str],
) -> Any:
    """Walk one level of ``data``; the addresses are already registered."""

    if isinstance(data, list):
        return [_redact(item, to_redact, prefixes, accounts) for item in data]
    if isinstance(data, tuple):
        items = [_redact(item, to_redact, prefixes, accounts) for item in data]
        # A named tuple keeps its type; its constructor takes the fields.
        if hasattr(data, "_fields"):
            return type(data)(*items)
        return tuple(items)
    if not isinstance(data, Mapping):
        return data

    redacted: dict[Any, Any] = {}

    for key, value in dict(data).items():
        out_key = _anonymise_key(key, accounts)
        # Not `out_key != key`: an anonymised name can also collide with a
        # literal key that arrives *later*, and that one would then overwrite
        # the earlier entry. Which field survives would depend on insertion
        # order, in a file whose purpose is to show what is there.
        while out_key in redacted:
            out_key = f"{out_key}-2"
        if value is None or (isinstance(value, str) and not value):
            redacted[out_key] = value
            continue
        names = _name_candidates(key)
        if any(name in to_redact for name in names) or (
            prefixes
            and any(
                isinstance(name, str) and name.startswith(prefixes) for name in names
            )
        ):
            redacted[out_key] = REDACTED
        else:
            redacted[out_key] = _redact(value, to_redact, prefixes, accounts)

    return redacted


def _register_addresses(data: Any, accounts: dict[str, str]) -> None:
    """Collect the e-mail addresses that appear as *values*, before redaction.

    The key names are built from the account address, so knowing the address
    lets the name be cleaned exactly instead of by pattern guessing. Run on the
    whole payload before any value has been replaced. Addresses are compared
    without regard to case: the token cache lower-cases some of the names it
    builds, and the address in the config entry keeps the case it was typed in.
    """

    if isinstance(data, Mapping):
        for value in data.values():
            _register_addresses(value, accounts)
        return
    if isinstance(data, (list, tuple)):
        for item in data:
            _register_addresses(item, accounts)
        return
    if isinstance(data, str) and _EMAIL_VALUE.match(data):
        _placeholder(data, accounts)


def _placeholder(address: str, accounts: dict[str, str]) -> str:
    """Return the stable placeholder of ``address``, numbering it on first sight.

    Numbered rather than hashed: a hash of an e-mail address is reversible with
    a word list, and the only thing a reader needs from the name is whether two
    entries belong to the same account.
    """

    folded = address.lower()
    if folded not in accounts:
        accounts[folded] = f"<account-{len(accounts) + 1}>"
    return accounts[folded]


def _standalone(address: str) -> re.Pattern[str]:
    """Match ``address`` only where it is not part of a longer address.

    On the left it must start the name or follow ``:`` or white space; on the
    right no further address character may follow. Not cached here: ``re``
    keeps its own cache of compiled patterns.
    """

    return re.compile(
        r"(?<![^:\s])" + re.escape(address) + r"(?![A-Za-z0-9.\-])",
        re.IGNORECASE,
    )


# The token cache builds names of the form `<ns>:<stem><address>`, far below
# this length. Rule 1 checks every `:` position against the rest of the name,
# which grows with the square of the length; longer names go straight to the
# pattern rules, which still replace every word with an `@`.
_MAX_STRUCTURED_KEY = 512


def _stem_cut(key: str) -> tuple[int, str] | None:
    """Return the leftmost ``(start, stem)`` where rule 1 applies, if any."""

    for start in _name_starts(key):
        name = key[start:]
        for stem in RUNTIME_KEY_STEMS:
            if name.startswith(stem) and _EMAIL_VALUE.match(name[len(stem) :]):
                return start, stem
    return None


def _anonymise_key(key: Any, accounts: dict[str, str]) -> Any:
    """Replace an account address inside a key *name* with a stable placeholder."""

    if not isinstance(key, str) or "@" not in key:
        return key

    # Rule 1: the token cache's own shape, where the boundary is known. The
    # part before the stem is cut off and handled again, in a loop rather
    # than by recursion, so that a long chain of `:` cannot exhaust the stack.
    segments: list[tuple[bool, str, str]] = []
    while (
        "@" in key
        and len(key) <= _MAX_STRUCTURED_KEY
        and (cut := _stem_cut(key)) is not None
    ):
        start, stem = cut
        segments.append((start > 0, stem, key[start + len(stem) :]))
        key = key[: start - 1] if start else ""
    # Left to right, so that the numbering follows the name.
    result = _pattern_rules(key, accounts)
    for after_colon, stem, address in reversed(segments):
        result += (":" if after_colon else "") + stem + _placeholder(address, accounts)
    return result


def _pattern_rules(key: str, accounts: dict[str, str]) -> str:
    """Rules 2 to 4, for a name or the part of it before a stem."""

    if "@" not in key:
        return key

    # Rule 2: known addresses, longest first, only where they stand alone.
    for folded in sorted(accounts, key=len, reverse=True):
        placeholder = accounts[folded]
        key = _standalone(folded).sub(lambda _match: placeholder, key)
        if "@" not in key:
            return key

    # Rule 3: anything still address-shaped.
    key = _EMAIL_IN_KEY.sub(lambda match: _placeholder(match.group(0), accounts), key)
    if "@" not in key:
        return key

    # Rule 4: the rest of a name that held more than one address.
    return _AT_WORD.sub(lambda match: _placeholder(match.group(0), accounts), key)


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
