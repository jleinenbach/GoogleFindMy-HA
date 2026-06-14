# tests/test_google_uploader_teardown.py
"""AP9 (finding 6a): SSL-transport teardown-race classification in the FMDN uploader.

``fmdn_finder/google_uploader._try_grpc_upload`` is the second grpclib stream
write site that shared the unclassified ``AttributeError`` teardown race fixed in
``SpotApi/spot_request.py``. These guards pin that it now classifies the asyncio
``_write_appdata`` race as a ``ValueError`` (the path's existing failure type)
while re-raising any unrelated ``AttributeError`` as a real bug.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.fmdn_finder.google_uploader import (
    _try_grpc_upload,
)
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module
from custom_components.googlefindmy.SpotApi.spot_grpc_transport import (
    SpotGrpcTransport,
)

_SSL_TEARDOWN_MSG = "'NoneType' object has no attribute '_write_appdata'"


class _RaisingStream:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> _RaisingStream:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send_message(self, _msg: bytes, end: bool = True) -> None:
        return None

    async def recv_message(self) -> bytes:
        raise self._exc


def _install(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    import grpclib.client  # noqa: PLC0415

    class _StubMethod:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def open(self, metadata: Any = None, timeout: float | None = None) -> _RaisingStream:
            return _RaisingStream(exc)

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


@pytest.mark.asyncio
async def test_upload_ssl_teardown_error_becomes_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asyncio teardown race is classified as the path's ValueError failure."""
    _install(monkeypatch, AttributeError(_SSL_TEARDOWN_MSG))

    with pytest.raises(ValueError, match="SSL transport teardown"):
        await _try_grpc_upload(
            cache=object(),  # type: ignore[arg-type]
            payload=b"payload",
            server="spot-pa.googleapis.com",
            service="google.internal.spot.v1.SpotService",
            method="UploadLocationReports",
        )


@pytest.mark.asyncio
async def test_upload_unrelated_attribute_error_is_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated AttributeError must not be masked as a connection failure."""
    _install(monkeypatch, AttributeError("totally unrelated"))

    with pytest.raises(AttributeError, match="totally unrelated"):
        await _try_grpc_upload(
            cache=object(),  # type: ignore[arg-type]
            payload=b"payload",
            server="spot-pa.googleapis.com",
            service="google.internal.spot.v1.SpotService",
            method="UploadLocationReports",
        )
