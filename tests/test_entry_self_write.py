# tests/test_entry_self_write.py
"""Unit tests for the receiver self-write receipts (``entry_self_write``)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.entry_self_write import (
    SELF_WRITE_RECEIPTS_KEY,
    consume_self_write,
    credential_member_digest,
    discard_self_write,
    note_self_write,
)


def test_the_digest_ignores_key_order_and_keeps_no_plaintext() -> None:
    first = credential_member_digest({"a": "token-value", "b": [1, 2]})
    second = credential_member_digest({"b": [1, 2], "a": "token-value"})

    assert first == second
    assert "token-value" not in first
    assert len(first) == 64
    assert credential_member_digest(None) != credential_member_digest({})
    # Values JSON cannot encode fall back to ``repr`` instead of raising.
    assert credential_member_digest({"x": object()}) != first


def test_a_receipt_is_consumed_exactly_once() -> None:
    hass = SimpleNamespace(data={})

    assert note_self_write(hass, "entry", "digest") is True
    assert hass.data[DOMAIN][SELF_WRITE_RECEIPTS_KEY] == {"entry": "digest"}
    assert consume_self_write(hass, "entry") == "digest"
    assert consume_self_write(hass, "entry") is None


def test_a_later_receipt_replaces_the_earlier_one() -> None:
    hass = SimpleNamespace(data={})

    note_self_write(hass, "entry", "first")
    note_self_write(hass, "entry", "second")

    assert consume_self_write(hass, "entry") == "second"


def test_discarding_drops_only_the_named_entry() -> None:
    hass = SimpleNamespace(data={})
    note_self_write(hass, "a", "digest-a")
    note_self_write(hass, "b", "digest-b")

    discard_self_write(hass, "a")
    discard_self_write(hass, "unknown")

    assert hass.data[DOMAIN][SELF_WRITE_RECEIPTS_KEY] == {"b": "digest-b"}


def test_missing_storage_is_tolerated() -> None:
    """Without ``hass.data`` nothing is stored and nothing raises."""

    no_data = SimpleNamespace()
    assert note_self_write(no_data, "entry", "digest") is False
    assert consume_self_write(no_data, "entry") is None
    discard_self_write(no_data, "entry")

    # Reading never creates the domain bucket or the receipt map.
    empty = SimpleNamespace(data={})
    assert consume_self_write(empty, "entry") is None
    discard_self_write(empty, "entry")
    assert empty.data == {}
    bucket_only = SimpleNamespace(data={DOMAIN: {}})
    assert consume_self_write(bucket_only, "entry") is None
    assert bucket_only.data == {DOMAIN: {}}


def test_foreign_shapes_in_hass_data_are_not_trusted() -> None:
    """A domain bucket or receipt map of the wrong type is not written into."""

    wrong_bucket = SimpleNamespace(data={DOMAIN: "not-a-mapping"})
    assert note_self_write(wrong_bucket, "entry", "digest") is False
    assert wrong_bucket.data == {DOMAIN: "not-a-mapping"}

    wrong_map = SimpleNamespace(data={DOMAIN: {SELF_WRITE_RECEIPTS_KEY: ["x"]}})
    assert consume_self_write(wrong_map, "entry") is None
    assert note_self_write(wrong_map, "entry", "digest") is True
    assert wrong_map.data[DOMAIN][SELF_WRITE_RECEIPTS_KEY] == {"entry": "digest"}
