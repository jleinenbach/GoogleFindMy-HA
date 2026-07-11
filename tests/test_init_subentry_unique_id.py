# tests/test_init_subentry_unique_id.py
"""Contract tests for ``_determine_subentry_unique_id`` (W1 AP-W1.10 / H7).

The ``device_tracker`` branch previously carried a redundant guard::

    if uid.count(":") >= 2 and uid.startswith(f"{entry_id}:{identifier}:"):
        return uid
    if uid.startswith(f"{entry_id}:{identifier}:"):
        return uid

The prefix ``{entry_id}:{identifier}:`` already contains two colons, so the
``count(":") >= 2`` pre-check was strictly implied by the ``startswith`` on the
next line and produced the identical result — dead code (H7). These tests pin
the terminal contract of every device_tracker output path and prove, by
enumeration over the reachable input shapes, that no input reaches a different
result now that the redundant branch has been removed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import (
    _DEFAULT_SUBENTRY_IDENTIFIER,
    _determine_subentry_unique_id,
)
from custom_components.googlefindmy.const import DOMAIN

ENTRY = "entry123"
IDENT = _DEFAULT_SUBENTRY_IDENTIFIER  # "core_tracking"
SUBENTRY_MAP: dict[str, str] = {}  # falls back to _DEFAULT_SUBENTRY_IDENTIFIER


def _tracker_entry(unique_id: str | None) -> SimpleNamespace:
    """Minimal ``er.RegistryEntry`` stub for the device_tracker domain."""
    return SimpleNamespace(unique_id=unique_id, domain="device_tracker")


def _determine(unique_id: str | None) -> str | None:
    return _determine_subentry_unique_id(ENTRY, SUBENTRY_MAP, _tracker_entry(unique_id))


# --- Terminal outputs of the device_tracker branch -------------------------


def test_empty_unique_id_returns_none():
    """Guard: an empty/None unique_id yields None (skip)."""
    assert _determine("") is None
    assert _determine(None) is None


def test_already_fully_scoped_kept_verbatim():
    """Already ``{entry}:{ident}:...`` scoped ids are returned unchanged.

    This is exactly the path the redundant H7 branch also matched.
    """
    uid = f"{ENTRY}:{IDENT}:mac_aabbcc"
    assert _determine(uid) == uid


def test_entry_scoped_but_unscoped_identifier_is_rescoped():
    """``{entry}:remainder`` (no identifier segment) gets the identifier spliced in."""
    assert _determine(f"{ENTRY}:mac_aabbcc") == f"{ENTRY}:{IDENT}:mac_aabbcc"


def test_entry_prefix_with_empty_remainder_returns_none():
    """``{entry}:`` with nothing after the colon is not migratable → None."""
    assert _determine(f"{ENTRY}:") is None


def test_domain_entry_scoped_with_identifier_kept():
    """``{DOMAIN}_{entry}_{ident}_...`` is already identifier-scoped → verbatim."""
    uid = f"{DOMAIN}_{ENTRY}_{IDENT}_mac"
    assert _determine(uid) == uid


def test_domain_entry_scoped_without_identifier_is_rescoped():
    """``{DOMAIN}_{entry}_<other>`` gets converted to colon-scoped form."""
    assert _determine(f"{DOMAIN}_{ENTRY}_mac") == f"{ENTRY}:{IDENT}:mac"


def test_legacy_domain_prefix_is_rescoped():
    """Legacy ``{DOMAIN}_...`` (no entry segment) is rescoped under the entry."""
    assert _determine(f"{DOMAIN}_mac") == f"{ENTRY}:{IDENT}:mac"


def test_unrelated_unique_id_returns_none():
    """A unique_id matching none of the prefixes is skipped."""
    assert _determine("someother_provider_id") is None


# --- H7 redundancy proof ---------------------------------------------------


@pytest.mark.parametrize(
    "uid",
    [
        f"{ENTRY}:{IDENT}:x",  # minimal fully-scoped
        f"{ENTRY}:{IDENT}:a:b:c",  # extra colons
        f"{ENTRY}:{IDENT}:",  # trailing colon, no tail
    ],
)
def test_fully_scoped_prefix_always_has_two_colons(uid):
    """H7: any id matching the scoped prefix necessarily has >= 2 colons.

    This is the invariant that made the removed ``count(":") >= 2`` guard dead:
    the guard could never be False while the ``startswith`` was True.
    """
    assert uid.startswith(f"{ENTRY}:{IDENT}:")
    assert uid.count(":") >= 2
    # And the function keeps it verbatim, exercising the merged branch.
    assert _determine(uid) == uid
