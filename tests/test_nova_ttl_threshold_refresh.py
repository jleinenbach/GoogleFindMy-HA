# tests/test_nova_ttl_threshold_refresh.py
"""Run the proactive refresh threshold of both ADM TTL policies.

``TTLPolicy.pre_request`` and ``AsyncTTLPolicy.async_pre_request`` refresh the
ADM token once its age reaches the measured TTL minus a safety margin, and log
that step at DEBUG. ``tests/test_unattended_routine_log_levels.py`` pins the
level of both records statically; no test executed the branch itself, so the
two records were the uncovered lines of the patch that demoted them.

Each policy gets one case past the threshold (refresh, header, timestamp, one
DEBUG record, nothing at INFO or above) and one case below it (no refresh, no
cache write, no record).

The threshold is ``best_ttl - TTL_MARGIN_SEC`` with a random jitter of
``JITTER_SEC`` in both directions. With a learned TTL of 3600 s the threshold
lies between 3390 s and 3570 s. Each case pins the jitter to the bound closest
to its token age (3570 s for the 7200 s old token, 3390 s for the 60 s old
one), so the outcome does not depend on the random source, and the ages keep
more than 3000 s from that bound, so no frozen clock is needed.

Not covered: the armed-probe path (it skips the threshold check) and the
``ValueError``/``TypeError`` fallback of the threshold computation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi import nova_request
from custom_components.googlefindmy.NovaApi.nova_request import (
    AsyncTTLPolicy,
    TTLPolicy,
)

_USERNAME = "user@example.com"
_NAMESPACE = "entry-ttl-threshold"
_BEST_TTL_SEC = 3600.0
_AGE_PAST_THRESHOLD_SEC = 7200.0
_AGE_BELOW_THRESHOLD_SEC = 60.0
_SYNC_TEXT = "ADM token reached measured threshold, proactively refreshing"
_ASYNC_TEXT = "ADM token reached measured threshold, proactively refreshing (async)"


def _pin_jitter(monkeypatch: pytest.MonkeyPatch, *, upper: bool) -> None:
    """Pin the threshold jitter to its upper or lower bound."""

    def _bound(low: float, high: float) -> float:
        return high if upper else low

    monkeypatch.setattr(nova_request.random, "uniform", _bound)


def _store_set(store: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        store.pop(key, None)
    else:
        store[key] = value


def _seed(store: dict[str, Any], policy: TTLPolicy, token_age_sec: float) -> None:
    """Learned TTL present, token of the given age, probe disarmed."""
    store[policy.k_issued] = time.time() - token_age_sec
    store[policy.k_bestttl] = _BEST_TTL_SEC
    store[policy.k_startleft] = 0
    store[policy.k_probenext] = time.time() + 86400
    store[policy.k_armed] = 0


class _Refresh:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return "fresh-token"


def _make_sync_policy(
    logger_name: str,
) -> tuple[TTLPolicy, dict[str, Any], _Refresh, list[str]]:
    store: dict[str, Any] = {}
    refresh = _Refresh()
    headers: list[str] = []
    policy = TTLPolicy(
        username=_USERNAME,
        logger=logging.getLogger(logger_name),
        get_value=store.get,
        set_value=lambda key, value: _store_set(store, key, value),
        refresh_fn=refresh,
        set_auth_header_fn=headers.append,
        ns_prefix=_NAMESPACE,
    )
    return policy, store, refresh, headers


def _make_async_policy(
    logger_name: str,
) -> tuple[AsyncTTLPolicy, dict[str, Any], _Refresh, list[str]]:
    store: dict[str, Any] = {}
    refresh = _Refresh()
    headers: list[str] = []

    async def _get(key: str) -> Any:
        return store.get(key)

    async def _set(key: str, value: Any) -> None:
        _store_set(store, key, value)

    async def _refresh() -> str:
        return refresh()

    policy = AsyncTTLPolicy(
        username=_USERNAME,
        logger=logging.getLogger(logger_name),
        get_value=_get,
        set_value=_set,
        refresh_fn=_refresh,
        set_auth_header_fn=headers.append,
        ns_prefix=_NAMESPACE,
    )
    return policy, store, refresh, headers


def _records(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


def _assert_refreshed_with_one_debug_record(
    records: list[logging.LogRecord],
    store: dict[str, Any],
    policy: TTLPolicy,
    refresh: _Refresh,
    headers: list[str],
    seeded_issued: float,
    text: str,
) -> None:
    assert refresh.calls == 1
    assert headers == ["Bearer fresh-token"]
    assert store[policy.k_issued] > seeded_issued
    matching = [record for record in records if record.getMessage() == text]
    assert len(matching) == 1
    assert matching[0].levelno == logging.DEBUG
    assert not [record for record in records if record.levelno >= logging.INFO]


def test_sync_policy_refreshes_past_threshold_and_logs_at_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the threshold the sync policy refreshes and logs once at DEBUG."""

    name = "test_nova_ttl_threshold.sync_past"
    policy, store, refresh, headers = _make_sync_policy(name)
    _pin_jitter(monkeypatch, upper=True)
    _seed(store, policy, _AGE_PAST_THRESHOLD_SEC)
    seeded_issued = store[policy.k_issued]

    with caplog.at_level(logging.DEBUG, logger=name):
        policy.pre_request()

    _assert_refreshed_with_one_debug_record(
        _records(caplog, name),
        store,
        policy,
        refresh,
        headers,
        seeded_issued,
        _SYNC_TEXT,
    )


def test_sync_policy_below_threshold_neither_refreshes_nor_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the threshold the sync policy leaves token, cache and log alone."""

    name = "test_nova_ttl_threshold.sync_below"
    policy, store, refresh, headers = _make_sync_policy(name)
    _pin_jitter(monkeypatch, upper=False)
    _seed(store, policy, _AGE_BELOW_THRESHOLD_SEC)
    before = dict(store)

    with caplog.at_level(logging.DEBUG, logger=name):
        policy.pre_request()

    assert refresh.calls == 0
    assert headers == []
    assert store == before
    assert _records(caplog, name) == []


@pytest.mark.asyncio
async def test_async_policy_refreshes_past_threshold_and_logs_at_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the threshold the async policy refreshes and logs once at DEBUG."""

    name = "test_nova_ttl_threshold.async_past"
    policy, store, refresh, headers = _make_async_policy(name)
    _pin_jitter(monkeypatch, upper=True)
    _seed(store, policy, _AGE_PAST_THRESHOLD_SEC)
    seeded_issued = store[policy.k_issued]

    with caplog.at_level(logging.DEBUG, logger=name):
        await policy.async_pre_request()

    _assert_refreshed_with_one_debug_record(
        _records(caplog, name),
        store,
        policy,
        refresh,
        headers,
        seeded_issued,
        _ASYNC_TEXT,
    )


@pytest.mark.asyncio
async def test_async_policy_below_threshold_neither_refreshes_nor_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the threshold the async policy leaves token, cache and log alone."""

    name = "test_nova_ttl_threshold.async_below"
    policy, store, refresh, headers = _make_async_policy(name)
    _pin_jitter(monkeypatch, upper=False)
    _seed(store, policy, _AGE_BELOW_THRESHOLD_SEC)
    before = dict(store)

    with caplog.at_level(logging.DEBUG, logger=name):
        await policy.async_pre_request()

    assert refresh.calls == 0
    assert headers == []
    assert store == before
    assert _records(caplog, name) == []
