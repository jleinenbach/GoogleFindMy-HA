from __future__ import annotations

import asyncio
import logging
import random
import socket
from collections.abc import Iterable

import grpclib.exceptions
from grpclib.client import UnaryUnaryMethod
from grpclib.const import Status

from custom_components.googlefindmy.Auth.adm_token_retrieval import (
    async_get_adm_token as async_get_adm_token_api,
)
from custom_components.googlefindmy.Auth.spot_token_retrieval import (
    async_get_spot_token,
)
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.Auth.username_provider import async_get_username
from custom_components.googlefindmy.const import DATA_AAS_TOKEN
from custom_components.googlefindmy.exceptions import MissingTokenCacheError
from custom_components.googlefindmy.SpotApi.spot_grpc_transport import (
    SPOT_GRPC_TRANSPORT,
    SpotGrpcTransport,
)

_LOGGER = logging.getLogger(__name__)

_SPOT_MAX_RETRIES = 3
_SPOT_INITIAL_BACKOFF_S = 1.0
_SPOT_BACKOFF_FACTOR = 2.0
_USER_AGENT = "com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT"
_async_sleep = asyncio.sleep


class SpotError(Exception):
    """Base exception for SPOT request failures."""


class SpotAuthPermanentError(SpotError):
    """Authentication failed after refresh; re-authentication is required."""


class SpotRateLimitError(SpotError):
    """Rate limited after bounded retries."""


class SpotGrpcStatusError(SpotError):
    """Non-auth gRPC status error outside the retry policy."""


class SpotNetworkError(SpotError):
    """Transport-layer error after bounded retries."""


class SpotTrailersOnlyError(SpotError):
    """OK status with missing or empty payload after retries."""


class SpotRequestFailedAfterRetries(SpotError):
    """Transient failures exhausted the retry budget."""


def _compute_delay(attempt: int) -> float:
    """Compute exponential backoff with jitter bounded to sixty seconds."""

    base = (_SPOT_BACKOFF_FACTOR ** (attempt - 1)) * _SPOT_INITIAL_BACKOFF_S
    return min(random.uniform(0.0, base), 60.0)


async def _pick_auth_token_async(
    *, prefer_adm: bool = False, cache: TokenCache
) -> tuple[str, str, str]:
    """Select an authentication token using the entry-scoped cache."""

    if cache is None:
        raise MissingTokenCacheError()

    username = await async_get_username(cache=cache)
    if not username:
        raise RuntimeError("Username is not configured; cannot select auth token.")

    if not prefer_adm:
        try:
            spot_token = await async_get_spot_token(username, cache=cache)
        except InvalidAasTokenError:
            raise
        except Exception as err:  # pragma: no cover - defensive logging only
            _LOGGER.debug("SPOT token retrieval failed: %s", err)
        else:
            if spot_token:
                return spot_token, "spot", username

    adm_token = await async_get_adm_token_api(username, cache=cache)
    if adm_token:
        return adm_token, "adm", username

    raise RuntimeError("No valid SPOT or ADM token available for the current user.")


async def _invalidate_token_async(kind: str, username: str, *, cache: TokenCache | None = None) -> None:
    """Invalidate cached tokens in the entry-scoped cache only."""

    if cache is None:
        raise MissingTokenCacheError()

    if kind == "spot":
        await cache.set(f"spot_token_{username}", None)
    if kind == "adm":
        await cache.set(f"adm_token_{username}", None)
    await cache.set(DATA_AAS_TOKEN, None)


async def _clear_aas_token_async(*, cache: TokenCache | None = None) -> None:
    """Clear the cached AAS token in the entry-scoped cache."""

    if cache is None:
        raise MissingTokenCacheError()

    await cache.set(DATA_AAS_TOKEN, None)


async def async_spot_request(
    api_scope: str,
    payload: bytes,
    *,
    cache: TokenCache,
    transport: SpotGrpcTransport | None = None,
) -> bytes:
    """
    Perform a SPOT unary gRPC request using grpclib.

    Design intent:
    - Reuse the shared grpclib channel for HTTP/2 multiplexing.
    - Preserve entry-scoped token isolation for multi-account setups.
    - Retry bounded times for transient statuses and rate limits.
    - Refresh authentication once before surfacing permanent failures.
    - Treat empty replies as transport anomalies before raising trailers-only.
    """

    active_transport = transport or SPOT_GRPC_TRANSPORT
    method_path = f"/google.internal.spot.v1.SpotService/{api_scope}"

    refreshed_once = False
    retries_used = 0
    aas_cleared_once = False

    while True:
        attempt = retries_used + 1
        prefer_adm = refreshed_once

        try:
            token, token_kind, token_user = await _pick_auth_token_async(
                prefer_adm=prefer_adm,
                cache=cache,
            )
        except InvalidAasTokenError as err:
            if not aas_cleared_once:
                aas_cleared_once = True
                await _clear_aas_token_async(cache=cache)
                continue
            raise SpotAuthPermanentError("AAS token invalid after refresh.") from err

        metadata: Iterable[tuple[str, str]] = (
            ("authorization", f"Bearer {token}"),
            ("user-agent", _USER_AGENT),
        )
        channel = await active_transport.get_channel()
        method = UnaryUnaryMethod(channel, method_path, bytes, bytes)

        try:
            async with method.open(metadata=metadata, timeout=30.0) as stream:
                await stream.send_message(payload, end=True)
                reply_bytes = await stream.recv_message()
        except grpclib.exceptions.GRPCError as err:
            status = err.status

            if status in (Status.UNAUTHENTICATED, Status.PERMISSION_DENIED):
                if not refreshed_once:
                    refreshed_once = True
                    await _invalidate_token_async(token_kind, token_user, cache=cache)
                    continue
                raise SpotAuthPermanentError("Authentication failed after refresh.") from err

            if status == Status.RESOURCE_EXHAUSTED:
                if retries_used < _SPOT_MAX_RETRIES:
                    retries_used += 1
                    await _async_sleep(_compute_delay(attempt))
                    continue
                raise SpotRateLimitError("Rate limited after retries.") from err

            if status in (
                Status.UNAVAILABLE,
                Status.INTERNAL,
                Status.UNKNOWN,
                Status.DEADLINE_EXCEEDED,
            ):
                if retries_used < _SPOT_MAX_RETRIES:
                    retries_used += 1
                    await _async_sleep(_compute_delay(attempt))
                    continue
                raise SpotRequestFailedAfterRetries(
                    f"Transient gRPC error ({status.name}) after retries."
                ) from err

            raise SpotGrpcStatusError(f"gRPC error: {status.name}") from err

        except (
            grpclib.exceptions.ProtocolError,
            grpclib.exceptions.StreamTerminatedError,
            ConnectionResetError,
            BrokenPipeError,
        ) as err:
            await active_transport.reset()
            if retries_used < _SPOT_MAX_RETRIES:
                retries_used += 1
                await _async_sleep(_compute_delay(attempt))
                continue
            raise SpotNetworkError("Fatal transport error after retries.") from err

        except TimeoutError as err:
            if retries_used < _SPOT_MAX_RETRIES:
                retries_used += 1
                await _async_sleep(_compute_delay(attempt))
                continue
            raise SpotNetworkError("Timeout after retries.") from err

        except (OSError, socket.gaierror) as err:
            if retries_used < _SPOT_MAX_RETRIES:
                retries_used += 1
                await _async_sleep(_compute_delay(attempt))
                continue
            await active_transport.reset()
            raise SpotNetworkError("Transport error after retries.") from err

        if reply_bytes is None or len(reply_bytes) == 0:
            if retries_used < _SPOT_MAX_RETRIES:
                retries_used += 1
                await _async_sleep(_compute_delay(attempt))
                continue
            raise SpotTrailersOnlyError("OK status but empty reply payload.")

        return reply_bytes
