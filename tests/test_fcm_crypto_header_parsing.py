# tests/test_fcm_crypto_header_parsing.py
"""Tests for FcmPushClient._extract_header_param."""
from __future__ import annotations

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
)


class TestExtractHeaderParam:
    """Verify semicolon-separated header values are parsed correctly."""

    def test_simple_dh(self) -> None:
        assert FcmPushClient._extract_header_param("dh=BPxxx", "dh") == "BPxxx"

    def test_dh_with_vapid_key(self) -> None:
        header = "dh=BPxxx;p256ecdsa=BYyyy"
        assert FcmPushClient._extract_header_param(header, "dh") == "BPxxx"

    def test_simple_salt(self) -> None:
        assert FcmPushClient._extract_header_param("salt=abc", "salt") == "abc"

    def test_salt_with_extra_params(self) -> None:
        header = "salt=abc;rs=4096"
        assert FcmPushClient._extract_header_param(header, "salt") == "abc"

    def test_whitespace_around_parts(self) -> None:
        header = "dh=BPxxx ; p256ecdsa=BYyyy"
        assert FcmPushClient._extract_header_param(header, "dh") == "BPxxx"
        assert FcmPushClient._extract_header_param(header, "p256ecdsa") == "BYyyy"

    def test_missing_param_raises(self) -> None:
        with pytest.raises(ValueError, match="Parameter 'dh' not found"):
            FcmPushClient._extract_header_param("salt=abc", "dh")
