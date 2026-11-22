# custom_components/googlefindmy/KeyBackup/vault_web_api.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Parse the account's owner-key vault entries from the Find My Device web app.

The accounts.google.com unlock page that ``shared_key_flow`` talks to hands back
a single shared key without any version tag. The Find My Device web app instead
calls its internal "batchexecute" RPC ``FAvFKc``, whose answer lists every
owner-key version the account vault currently holds, each wrapped with the shared
key. A tracker whose ``ownerKeyVersion`` is ahead of the version Spot serves can
only be decrypted with such an entry (issue #246).

This module is the pure half of that path: constants for the browser driver
(``FIND_MY_DEVICE_URL``, ``FETCH_SCRIPT``), the response parser, the
``finder_hw`` selection and the conversion to and from the bundle field
``owner_key_versions``. It imports only the standard library, so it loads inside
Home Assistant without any browser package; the driver that runs
``FETCH_SCRIPT`` lives in ``shared_key_flow``.

Provenance: Port of the core of leonboe1/GoogleFindMyTools#114 (head c378f8cf,
author P6g9YHK6), GPL-3.0. Taken over: the URL, the fetch script and the
chunk-walking idea of the parser. Changed: strict shape checks with a closed set
of failure causes instead of best-effort ``[]``, no console output, and blobs
kept out of ``repr``.

Shape assumption (as of 2026-11-15): the frame (``)]}'`` prefix, length-prefixed
chunks for ``rt=c``, the ``wrb.fr`` tag) matches several independent public
descriptions of batchexecute. The inner form of the ``FAvFKc`` payload is not
publicly documented; it is reconstructed from the parser in the pull request
above: the entry list sits at ``inner[0][5]`` and each entry starts with
``key_b64, version, domain, epoch``. If Google changes that form, the parser
reports ``foreign_shape`` or ``wrong_length`` instead of handing a misread blob
to AES-GCM.

Nothing here logs or prints. ``VaultEntry.key`` is excluded from ``repr`` and
``VaultFetchResult`` renders only the entry count and the cause, so an
exception handler that formats one of them cannot leak blob bytes.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeGuard

__all__ = [
    "ENCRYPTED_OWNER_KEY_LEN",
    "FETCH_SCRIPT",
    "FINDER_HW_DOMAIN",
    "FIND_MY_DEVICE_URL",
    "OWNER_KEY_VERSIONS_FIELD",
    "RPC_ID",
    "SharedKeyFlowDeadlineError",
    "VaultCause",
    "VaultEntry",
    "VaultFetchResult",
    "from_bundle_field",
    "parse_batchexecute",
    "select_finder_hw",
    "to_bundle_field",
]

# Top-level bundle field written by the login tool. The ``owner_key`` prefix is
# deliberate: ``main.py --reauth`` deletes every ``owner_key*`` field and the
# diagnostics redaction covers the same prefix.
OWNER_KEY_VERSIONS_FIELD = "owner_key_versions"

FINDER_HW_DOMAIN = "finder_hw"
RPC_ID = "FAvFKc"

FIND_MY_DEVICE_URL = "https://www.google.com/android/find/?login=&device=1&rs=1"

# Length of one wrapped owner key: AES-GCM ``IV || CIPHERTEXT || TAG`` around a
# 32-byte owner key. The three terms mirror ``cloud_key_decryptor.GCM_IV_LEN_DEFAULT``,
# ``get_owner_key._OWNER_KEY_LENGTH`` and the 16-byte GCM tag; they are restated
# here because this module must not import the cryptography stack, and
# ``tests/test_vault_web_api.py`` pins them against those sources.
_GCM_IV_LEN = 12
_OWNER_KEY_LEN = 32
_GCM_TAG_LEN = 16
ENCRYPTED_OWNER_KEY_LEN = _GCM_IV_LEN + _OWNER_KEY_LEN + _GCM_TAG_LEN

_XSSI_PREFIX = ")]}'"
_FRAME_TAG = "wrb.fr"
_ENTRY_LIST_INDEX = 5
# ``["wrb.fr", rpc_id, payload, ...]`` and ``[key_b64, version, domain, epoch, ...]``.
_FRAME_ITEM_MIN_LEN = 3
_ENTRY_MIN_LEN = 4
_HEX_BLOB = re.compile(rf"[0-9a-f]{{{2 * ENCRYPTED_OWNER_KEY_LEN}}}")

# Runs inside an already signed-in Selenium session on ``FIND_MY_DEVICE_URL``.
# ``window.WIZ_global_data`` carries the batchexecute parameters of that page:
# ``FdrFJe`` is the session id (``f.sid``), ``cfb2h`` the build label (``bl``)
# and ``SNlM0e`` the anti-CSRF value (``at``). The script hands back
# ``{ok, status, text}`` or ``{ok: false, error}`` through the async callback.
FETCH_SCRIPT = """
const callback = arguments[arguments.length - 1];
(async () => {
    try {
        const g = window.WIZ_global_data;
        const params = new URLSearchParams({
            "rpcids": "FAvFKc",
            "source-path": "/android/find/",
            "f.sid": g.FdrFJe,
            "bl": g.cfb2h,
            "hl": "en-GB",
            "_reqid": String(Math.floor(Math.random() * 900000) + 100000),
            "rt": "c",
        });
        const body = new URLSearchParams({
            "f.req": JSON.stringify([[["FAvFKc", "[]", null, "generic"]]]),
            "at": g.SNlM0e,
        });
        const resp = await fetch(
            "https://www.google.com/android/find/_/BoqWebFindMyDeviceUi/data/batchexecute?" + params.toString(),
            {
                method: "POST",
                headers: {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8", "X-Same-Domain": "1"},
                body: body.toString(),
                credentials: "include",
            }
        );
        callback({ok: resp.ok, status: resp.status, text: await resp.text()});
    } catch (e) {
        callback({ok: false, error: String(e)});
    }
})();
"""


class VaultCause(StrEnum):
    """Closed set of reasons why a vault fetch produced no usable entries.

    The parser in this module emits the first four; the browser driver and the
    login tool emit the remaining three.
    """

    NO_FRAME = "no_frame"
    EMPTY_PAYLOAD = "empty_payload"
    FOREIGN_SHAPE = "foreign_shape"
    WRONG_LENGTH = "wrong_length"
    HTTP_STATUS = "http_status"
    SELF_TEST_FAILED = "self_test_failed"
    ALERT_DEADLINE = "alert_deadline"


class SharedKeyFlowDeadlineError(RuntimeError):
    """The unlock page sent no result before the alert deadline ran out.

    Raised by the browser flow after the driver has quit, so the login tool can
    say that the sign-in was not finished in time instead of blaming the
    browser installation. Defined here because this module needs no browser
    package, so the Home Assistant side can import it as well.
    """


@dataclass(frozen=True, slots=True)
class VaultEntry:
    """One vault entry; ``key`` is the owner key still wrapped with the shared key."""

    version: int
    epoch: int
    domain: str
    key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class VaultFetchResult:
    """Outcome of a vault fetch: ``cause is None`` exactly when ``entries`` is non-empty."""

    entries: tuple[VaultEntry, ...]
    cause: VaultCause | None

    def __repr__(self) -> str:
        cause = None if self.cause is None else self.cause.value
        return f"VaultFetchResult(entries={len(self.entries)}, cause={cause})"

    def __str__(self) -> str:
        return self.__repr__()


class _ShapeError(Exception):
    """Internal signal carrying the cause of a rejected response."""

    def __init__(self, cause: VaultCause) -> None:
        super().__init__(cause.value)
        self.cause = cause


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _failure(cause: VaultCause) -> VaultFetchResult:
    return VaultFetchResult(entries=(), cause=cause)


def _find_payload(text: str) -> tuple[bool, object]:
    """Walk the length-prefixed chunks and return the ``FAvFKc`` payload.

    The declared chunk length is only used to recognise a chunk header; the
    pull request notes it was off by one character against a live capture, so
    ``raw_decode`` determines where the JSON really ends. Returns
    ``(found, payload)``.
    """
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX) :]
    text = text.lstrip("\n")

    decoder = json.JSONDecoder()
    pos, end = 0, len(text)
    while pos < end:
        newline = text.find("\n", pos)
        if newline == -1 or not text[pos:newline].isdigit():
            break
        try:
            envelope, pos = decoder.raw_decode(text, newline + 1)
        except ValueError:
            break
        if pos < end and text[pos] == "\n":
            pos += 1
        if not isinstance(envelope, list):
            continue
        for item in envelope:
            if (
                isinstance(item, list)
                and len(item) >= _FRAME_ITEM_MIN_LEN
                and item[0] == _FRAME_TAG
                and item[1] == RPC_ID
            ):
                return True, item[2]
    return False, None


def _parse_entry(raw: object) -> VaultEntry:
    if not isinstance(raw, list) or len(raw) < _ENTRY_MIN_LEN:
        raise _ShapeError(VaultCause.FOREIGN_SHAPE)
    blob_b64, version, domain, epoch = raw[0], raw[1], raw[2], raw[3]
    if not (
        isinstance(blob_b64, str)
        and _is_int(version)
        and isinstance(domain, str)
        and _is_int(epoch)
    ):
        raise _ShapeError(VaultCause.FOREIGN_SHAPE)
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise _ShapeError(VaultCause.FOREIGN_SHAPE) from err
    # The derived length only holds for wrapped owner keys; entries of other
    # domains are kept as they are and dropped later by ``select_finder_hw``.
    if domain == FINDER_HW_DOMAIN and len(blob) != ENCRYPTED_OWNER_KEY_LEN:
        raise _ShapeError(VaultCause.WRONG_LENGTH)
    return VaultEntry(version=version, epoch=epoch, domain=domain, key=blob)


def _parse_payload(payload: object) -> tuple[VaultEntry, ...]:
    if not payload:
        raise _ShapeError(VaultCause.EMPTY_PAYLOAD)
    if not isinstance(payload, str):
        raise _ShapeError(VaultCause.FOREIGN_SHAPE)
    try:
        inner = json.loads(payload)
    except (ValueError, RecursionError) as err:
        raise _ShapeError(VaultCause.FOREIGN_SHAPE) from err
    if (
        not isinstance(inner, list)
        or not inner
        or not isinstance(inner[0], list)
        or len(inner[0]) <= _ENTRY_LIST_INDEX
    ):
        raise _ShapeError(VaultCause.FOREIGN_SHAPE)
    raw_entries = inner[0][_ENTRY_LIST_INDEX]
    if not isinstance(raw_entries, list):
        raise _ShapeError(VaultCause.FOREIGN_SHAPE)
    if not raw_entries:
        raise _ShapeError(VaultCause.EMPTY_PAYLOAD)
    return tuple(_parse_entry(raw) for raw in raw_entries)


def parse_batchexecute(text: str) -> VaultFetchResult:
    """Parse a batchexecute response body of the ``FAvFKc`` RPC.

    Never raises. Any rejected response yields ``entries == ()`` and one cause:
    ``no_frame`` when no ``wrb.fr``/``FAvFKc`` item is found (including a broken
    chunk length line), ``empty_payload`` for a null or empty payload or an empty
    entry list, ``foreign_shape`` for any other unexpected structure or invalid
    Base64, and ``wrong_length`` for a ``finder_hw`` blob whose length is not
    ``ENCRYPTED_OWNER_KEY_LEN``. A single bad entry rejects the whole response, so
    a format change shows up as a cause rather than as a partial key set.
    """
    try:
        found, payload = _find_payload(text)
        if not found:
            return _failure(VaultCause.NO_FRAME)
        return VaultFetchResult(entries=_parse_payload(payload), cause=None)
    except _ShapeError as err:
        return _failure(err.cause)
    except (ValueError, TypeError, IndexError, KeyError, RecursionError):
        # Defensive net for the "never raises" contract; every expected shape
        # error above is mapped explicitly.
        return _failure(VaultCause.FOREIGN_SHAPE)


def select_finder_hw(entries: Iterable[VaultEntry]) -> tuple[VaultEntry, ...]:
    """Keep ``finder_hw`` entries only, one per version with the highest epoch.

    Versions below 1 are dropped: the runtime only ever looks up positive
    versions, and ``from_bundle_field`` rejects any other. Ties go to the later
    entry, as in ``response_parser.get_fmdn_shared_key``
    (``epoch >= latest_epoch``). The result is ordered by version.
    """
    latest: dict[int, VaultEntry] = {}
    for entry in entries:
        if entry.domain != FINDER_HW_DOMAIN or entry.version < 1:
            continue
        current = latest.get(entry.version)
        if current is None or entry.epoch >= current.epoch:
            latest[entry.version] = entry
    return tuple(latest[version] for version in sorted(latest))


def to_bundle_field(entries: Iterable[VaultEntry]) -> dict[str, dict[str, object]]:
    """Render the bundle field: decimal version -> version, epoch, domain, hex blob.

    Applies ``select_finder_hw`` first, so the field never holds a foreign domain
    or two epochs of one version.
    """
    return {
        str(entry.version): {
            "version": entry.version,
            "epoch": entry.epoch,
            "domain": entry.domain,
            "key": entry.key.hex(),
        }
        for entry in select_finder_hw(entries)
    }


def _entry_from_field_item(name: object, item: object) -> VaultEntry | None:
    if not isinstance(name, str) or not isinstance(item, dict):
        return None
    version = item.get("version")
    epoch = item.get("epoch")
    domain = item.get("domain")
    blob_hex = item.get("key")
    if not (
        _is_int(version)
        and version > 0
        and _is_int(epoch)
        and domain == FINDER_HW_DOMAIN
        and isinstance(blob_hex, str)
        and _HEX_BLOB.fullmatch(blob_hex)
        and name == str(version)
    ):
        return None
    return VaultEntry(
        version=version,
        epoch=epoch,
        domain=FINDER_HW_DOMAIN,
        key=bytes.fromhex(blob_hex),
    )


def from_bundle_field(value: object) -> tuple[VaultEntry, ...] | None:
    """Read the bundle field back; ``None`` for any foreign shape, never raises.

    Accepted only in the exact shape ``to_bundle_field`` writes: a dict whose keys
    are the decimal versions and whose values carry a positive int ``version``
    equal to the key, an int ``epoch``, the domain ``finder_hw`` and the lowercase hex of a
    blob of ``ENCRYPTED_OWNER_KEY_LEN`` bytes. One foreign item rejects the whole
    field. An empty dict yields ``()``. Entries are ordered by version.
    """
    if not isinstance(value, dict):
        return None
    entries: list[VaultEntry] = []
    for name, item in value.items():
        entry = _entry_from_field_item(name, item)
        if entry is None:
            return None
        entries.append(entry)
    return tuple(sorted(entries, key=lambda entry: entry.version))
