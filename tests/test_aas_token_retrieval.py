# tests/test_aas_token_retrieval.py
"""Unit tests for the gpsoauth exchange helper."""

from __future__ import annotations

import logging
import asyncio
from typing import Any, Dict

import pytest

from custom_components.googlefindmy.Auth import aas_token_retrieval


def test_exchange_oauth_for_aas_logs_inputs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Ensure the exchange logs a masked username and token diagnostics."""

    recorded_args: dict[str, Any] = {}

    def fake_exchange(username: str, oauth_token: str, android_id: int) -> Dict[str, Any]:
        recorded_args["username"] = username
        recorded_args["oauth_token"] = oauth_token
        recorded_args["android_id"] = android_id
        return {"Token": "aas-token", "Email": username}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    caplog.set_level(logging.DEBUG, logger=aas_token_retrieval.__name__)

    result = asyncio.run(
        aas_token_retrieval._exchange_oauth_for_aas(
            "user@example.com", "oauth-secret-value", 0x1234
        )
    )

    assert result["Token"] == "aas-token"
    assert recorded_args == {
        "username": "user@example.com",
        "oauth_token": "oauth-secret-value",
        "android_id": 0x1234,
    }

    messages = "\n".join(record.message for record in caplog.records)
    assert "Calling gpsoauth.exchange_token" in messages
    assert "username=u***@example.com" in messages
    assert "oauth_token_len=18" in messages
    assert "gpsoauth exchange response received" in messages


def test_exchange_oauth_for_aas_missing_token_logs_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A missing Token key results in a warning and a RuntimeError."""

    def fake_exchange(*_: Any, **__: Any) -> Dict[str, Any]:
        return {"Error": "BadAuthentication"}

    monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange)

    caplog.set_level(logging.WARNING, logger=aas_token_retrieval.__name__)

    with pytest.raises(RuntimeError, match="Missing 'Token' in gpsoauth response"):
        asyncio.run(
            aas_token_retrieval._exchange_oauth_for_aas(
                "user@example.com", "oauth-secret-value", 0xDEADBEEF
            )
        )

    warnings = [record.message for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("gpsoauth response missing 'Token'" in message for message in warnings)
    assert any("BadAuthentication" in message for message in warnings)
