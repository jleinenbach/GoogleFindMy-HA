# tests/test_proto3_defaults.py
"""Unit tests for the proto3 default helpers in ``proto3_defaults.py``.

The cache tests cover ``drop_proto3_defaults`` through the real merge. These
tests pin ``proto3_number_or_none``, which producers use to turn an unset int
field into ``None``, against the input forms listed below.
"""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.proto3_defaults import proto3_number_or_none


@pytest.mark.parametrize("value", [1, 1700000000])
def test_number_or_none_keeps_set_int(value: int) -> None:
    """A positive int is a set field and is returned as it is."""
    result = proto3_number_or_none(value)
    assert result == value
    assert type(result) is int


@pytest.mark.parametrize("value", [0, -1])
def test_number_or_none_drops_unset_int(value: int) -> None:
    """Zero is the proto3 default; a negative int is no valid anchor either."""
    assert proto3_number_or_none(value) is None


@pytest.mark.parametrize("value", [True, False, "5", 5.0, None, b"\x01"])
def test_number_or_none_rejects_non_int(value: object) -> None:
    """A bool, string, float, None or bytes is no int field value."""
    assert proto3_number_or_none(value) is None
