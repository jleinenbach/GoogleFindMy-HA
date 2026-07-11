# tests/test_google_uploader_contracts.py
"""Contract tests for ``fmdn_finder/google_uploader`` (Coverage W1, AP-W1.4).

The star is the H6 regression guard: ``FMDN_UPLOAD_ENABLED`` is the single
switch standing between Home Assistant and a live gRPC upload attempt against
Google. While it is ``False`` the transport path must never be reached. The
remaining tests pin the public diagnostics API, every terminal exit of
``_get_cache_from_hass`` and the still-uncovered exits of ``_try_grpc_upload``
(success, gRPC status error, connection error). The two asyncio SSL-teardown
exits are already covered by ``test_google_uploader_teardown.py`` and are not
duplicated here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.fmdn_finder import google_uploader as gu
from custom_components.googlefindmy.fmdn_finder.google_uploader import (
    _get_cache_from_hass,
    _try_grpc_upload,
    async_upload_to_google_fmdn,
    get_upload_status,
    is_upload_enabled,
)
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module
from custom_components.googlefindmy.SpotApi.spot_grpc_transport import SpotGrpcTransport

_EID_HEX = "0011223344556677"


# ---------------------------------------------------------------------------
# H6: the disabled guard must fail closed and never touch the transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_disabled_returns_false_and_skips_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H6: while FMDN_UPLOAD_ENABLED is False, no upload machinery runs."""
    assert gu.FMDN_UPLOAD_ENABLED is False  # baseline invariant

    grpc = AsyncMock()
    cache_getter = AsyncMock()
    monkeypatch.setattr(gu, "_try_grpc_upload", grpc)
    monkeypatch.setattr(gu, "_get_cache_from_hass", cache_getter)

    result = await async_upload_to_google_fmdn(
        hass=SimpleNamespace(data={}),  # type: ignore[arg-type]
        payload=b"payload-bytes",
        truncated_eid_hex=_EID_HEX,
    )

    assert result is False
    grpc.assert_not_called()
    cache_getter.assert_not_called()


@pytest.mark.asyncio
async def test_upload_enabled_success_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gu, "FMDN_UPLOAD_ENABLED", True)
    monkeypatch.setattr(gu, "_get_cache_from_hass", AsyncMock(return_value=object()))
    grpc = AsyncMock(return_value=b"response-bytes")
    monkeypatch.setattr(gu, "_try_grpc_upload", grpc)

    result = await async_upload_to_google_fmdn(
        hass=SimpleNamespace(data={}),  # type: ignore[arg-type]
        payload=b"payload",
        truncated_eid_hex=_EID_HEX,
    )

    assert result is True
    grpc.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_enabled_failure_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gu, "FMDN_UPLOAD_ENABLED", True)
    monkeypatch.setattr(gu, "_get_cache_from_hass", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        gu, "_try_grpc_upload", AsyncMock(side_effect=RuntimeError("boom"))
    )

    with pytest.raises(ValueError, match="Upload failed"):
        await async_upload_to_google_fmdn(
            hass=SimpleNamespace(data={}),  # type: ignore[arg-type]
            payload=b"payload",
            truncated_eid_hex=_EID_HEX,
        )


# ---------------------------------------------------------------------------
# Public diagnostics API
# ---------------------------------------------------------------------------


def test_is_upload_enabled_reflects_flag() -> None:
    assert is_upload_enabled() is False


def test_get_upload_status_reports_disabled_config() -> None:
    status = get_upload_status()
    assert status["enabled"] == "False"
    assert status["blocker"] == "DroidGuard attestation required"
    assert status["server"] == gu.FMDN_UPLOAD_SERVER


# ---------------------------------------------------------------------------
# _get_cache_from_hass: five terminal exits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cache_raises_when_integration_not_loaded() -> None:
    hass = SimpleNamespace(data={})
    with pytest.raises(RuntimeError, match="not loaded"):
        await _get_cache_from_hass(hass)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_cache_raises_when_no_entries() -> None:
    hass = SimpleNamespace(data={"googlefindmy": {"entries": {}}})
    with pytest.raises(RuntimeError, match="No GoogleFindMy entries"):
        await _get_cache_from_hass(hass)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_cache_returns_token_cache_attribute() -> None:
    sentinel = object()
    entry = SimpleNamespace(token_cache=sentinel)
    hass = SimpleNamespace(data={"googlefindmy": {"entries": {"e1": entry}}})
    assert await _get_cache_from_hass(hass) is sentinel  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_cache_returns_dict_style_cache() -> None:
    sentinel = object()
    entry = {"cache": sentinel}  # dict-style entry, no token_cache attr
    hass = SimpleNamespace(data={"googlefindmy": {"entries": {"e1": entry}}})
    assert await _get_cache_from_hass(hass) is sentinel  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_cache_raises_when_cache_missing() -> None:
    entry = SimpleNamespace(token_cache=None)  # attr present but None, not a dict
    hass = SimpleNamespace(data={"googlefindmy": {"entries": {"e1": entry}}})
    with pytest.raises(RuntimeError, match="TokenCache not available"):
        await _get_cache_from_hass(hass)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _try_grpc_upload: remaining terminal exits (teardown races: see
# test_google_uploader_teardown.py)
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, reply: Any = b"", exc: Exception | None = None) -> None:
        self._reply = reply
        self._exc = exc
        self.sent: list[bytes] = []

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send_message(self, msg: bytes, end: bool = True) -> None:
        self.sent.append(msg)

    async def recv_message(self) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._reply


def _install_transport(monkeypatch: pytest.MonkeyPatch, stream: _Stream) -> None:
    import grpclib.client  # noqa: PLC0415

    class _StubMethod:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def open(self, metadata: Any = None, timeout: float | None = None) -> _Stream:
            return stream

    monkeypatch.setattr(grpclib.client, "UnaryUnaryMethod", _StubMethod)
    monkeypatch.setattr(
        spot_request_module,
        "_pick_auth_token_async",
        AsyncMock(return_value=("token", "kind", "user")),
    )
    monkeypatch.setattr(
        SpotGrpcTransport, "get_channel", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(SpotGrpcTransport, "async_close", AsyncMock())


async def _call_grpc() -> bytes:
    return await _try_grpc_upload(
        cache=object(),  # type: ignore[arg-type]
        payload=b"payload",
        server="spot-pa.googleapis.com",
        service="google.internal.spot.v1.SpotService",
        method="UploadLocationReports",
    )


@pytest.mark.asyncio
async def test_grpc_upload_returns_reply_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Stream(reply=b"the-reply")
    _install_transport(monkeypatch, stream)
    assert await _call_grpc() == b"the-reply"
    assert stream.sent == [b"payload"]


@pytest.mark.asyncio
async def test_grpc_upload_empty_reply_becomes_empty_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(monkeypatch, _Stream(reply=None))
    assert await _call_grpc() == b""


@pytest.mark.asyncio
async def test_grpc_upload_grpc_error_becomes_spot_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grpclib.const
    import grpclib.exceptions

    from custom_components.googlefindmy.SpotApi.spot_request import SpotGrpcStatusError

    exc = grpclib.exceptions.GRPCError(grpclib.const.Status.UNIMPLEMENTED)
    _install_transport(monkeypatch, _Stream(exc=exc))
    with pytest.raises(SpotGrpcStatusError, match="DroidGuard attestation"):
        await _call_grpc()


@pytest.mark.asyncio
async def test_grpc_upload_connection_error_becomes_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_transport(monkeypatch, _Stream(exc=ConnectionError("refused")))
    with pytest.raises(ValueError, match="Connection to .* failed"):
        await _call_grpc()


@pytest.mark.asyncio
async def test_grpc_upload_closes_transport_even_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Stream(reply=b"ok")
    close_mock = AsyncMock()
    _install_transport(monkeypatch, stream)
    monkeypatch.setattr(SpotGrpcTransport, "async_close", close_mock)
    await _call_grpc()
    close_mock.assert_awaited()  # finally-block always tears the transport down


@pytest.mark.asyncio
async def test_grpc_upload_swallows_teardown_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while closing the transport must not mask the reply."""
    stream = _Stream(reply=b"ok")
    _install_transport(monkeypatch, stream)
    monkeypatch.setattr(
        SpotGrpcTransport,
        "async_close",
        AsyncMock(side_effect=RuntimeError("close race")),
    )
    assert await _call_grpc() == b"ok"
