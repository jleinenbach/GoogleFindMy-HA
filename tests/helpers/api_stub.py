# tests/helpers/api_stub.py
"""Test fixtures for :class:`GoogleFindMyAPI` (Phase 4 AP-I).

Mirror production attribute names from :mod:`custom_components.googlefindmy.api`
verbatim. Properties and helpers read these by exact name; semantic-equivalent
renames cause AttributeError.

Why factories instead of a subclass?
- ``GoogleFindMyAPI`` is self-contained (no mixin chain like the coordinator),
  so a subclass would only obscure construction. Factories let tests assemble
  exactly the surface they need (cache shape, session loop binding, FCM
  receiver readiness) without inheriting unrelated production wiring.
- Each test can rebind module-level provider hooks
  (``_FCM_ReceiverGetter``) through ``monkeypatch.setattr`` on the imported
  ``api`` module without polluting other tests.

Stub parity (KV-10): the attribute names below mirror the production
``CacheProtocol`` and ``FcmReceiverProtocol`` 1:1. Anything renamed semantically
(e.g. ``entry`` instead of ``entry_id``) is a bug, not a refactor.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


class StubCache:
    """Minimal :class:`CacheProtocol`-shaped cache for API tests.

    Mirrors ``TokenCache`` surface used by ``GoogleFindMyAPI``:

    - ``entry_id`` and ``namespace`` attributes (read via ``getattr``)
    - ``async_get_cached_value`` / ``async_set_cached_value`` coroutines
    """

    def __init__(
        self,
        *,
        entry_id: str | None = "entry-test",
        namespace: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.namespace = namespace
        self._values: dict[str, Any] = dict(values or {})

    async def async_get_cached_value(self, key: str) -> Any:
        return self._values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self._values[key] = value


class RaisingCache(StubCache):
    """Cache whose ``entry_id`` attribute raises on access.

    Used to exercise the defensive ``try/except`` paths in
    :meth:`GoogleFindMyAPI._namespace` and :meth:`_get_fcm_token_for_action`.
    """

    @property  # type: ignore[override]
    def entry_id(self) -> str | None:  # noqa: D401 - mimic attribute name
        raise RuntimeError("entry_id unavailable")

    @entry_id.setter
    def entry_id(self, _value: str | None) -> None:
        return None


class FakeReceiver:
    """Minimal :class:`FcmReceiverProtocol`-shaped receiver.

    The production protocol only requires ``get_fcm_token(entry_id=None)``.
    Optional attributes (``is_ready``/``ready``, ``pc``) are exposed for the
    push-readiness heuristic in :meth:`GoogleFindMyAPI.is_push_ready`.
    """

    def __init__(
        self,
        *,
        token: str | None = "token-1234567890",
        accepts_entry: bool = True,
        is_ready: bool | None = None,
        pc: Any | None = None,
        raise_on_get: BaseException | None = None,
        raise_on_get_legacy: BaseException | None = None,
    ) -> None:
        self._token = token
        self._accepts_entry = accepts_entry
        self.is_ready = is_ready
        self.pc = pc
        self._raise_on_get = raise_on_get
        self._raise_on_get_legacy = raise_on_get_legacy
        self.calls: list[tuple[Any, ...]] = []

    def get_fcm_token(self, *args: Any) -> str | None:
        self.calls.append(args)
        if args and not self._accepts_entry:
            raise TypeError("legacy receiver: entry_id not supported")
        if args:
            if self._raise_on_get is not None:
                raise self._raise_on_get
        else:
            if self._raise_on_get_legacy is not None:
                raise self._raise_on_get_legacy
            if self._raise_on_get is not None and self._raise_on_get_legacy is None:
                raise self._raise_on_get
        return self._token


def make_pc(
    *, run_state_name: str = "STARTED", do_listen: bool = True
) -> SimpleNamespace:
    """Build a push-client double for :meth:`GoogleFindMyAPI.is_push_ready`.

    ``run_state`` exposes a ``.name`` attribute, matching the production
    heuristic that tolerates enum or string values.
    """

    return SimpleNamespace(
        run_state=SimpleNamespace(name=run_state_name),
        do_listen=do_listen,
    )


def install_receiver_provider(
    monkeypatch: Any,
    receiver: FakeReceiver | None,
    *,
    accepts_entry: bool = True,
    raise_on_call: BaseException | None = None,
    raise_on_legacy_call: BaseException | None = None,
) -> list[tuple[Any, ...]]:
    """Install a fake ``_FCM_ReceiverGetter`` and return a call-log list.

    The call log captures positional args passed to the provider, so tests can
    assert entry-scoped vs. legacy invocation.
    """

    calls: list[tuple[Any, ...]] = []

    def _provider(*args: Any) -> FakeReceiver | None:
        calls.append(args)
        if args and not accepts_entry:
            raise TypeError("legacy provider: entry_id not supported")
        if args:
            if raise_on_call is not None:
                raise raise_on_call
        else:
            if raise_on_legacy_call is not None:
                raise raise_on_legacy_call
            if raise_on_call is not None and raise_on_legacy_call is None:
                raise raise_on_call
        return receiver

    monkeypatch.setattr(
        "custom_components.googlefindmy.api._FCM_ReceiverGetter", _provider
    )
    return calls


def run_coro(coro: Any) -> Any:
    """Drive a coroutine to completion on a fresh loop (no outer loop active)."""

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
