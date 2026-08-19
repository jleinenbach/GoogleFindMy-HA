# tests/test_shared_key_retrieval.py
"""Unit and regression tests for ``KeyBackup.shared_key_retrieval``.

These tests drive the entry-scoped shared-key retrieval helper towards full
line/branch coverage and lock in three previously fixed bugs:

* **Cross-account leakage** - the helper must read/write strictly against the
  per-entry ``TokenCache`` it is handed; two account caches must never bleed
  into each other (module docstring lines 13-15).
* **"Five browser windows" guard** - the interactive browser flow may run at
  most once per process; a second attempt must fail fast instead of opening
  another Chrome window (``_browser_flow_attempted`` guard).
* **base64 -> hex self-heal** - a stored base64/base64url/PEM-like value must be
  normalised to hex on first read so later reads stay on the fast hex path.
"""

from __future__ import annotations

import base64
import sys
from collections.abc import Awaitable, Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.KeyBackup import shared_key_retrieval as skr

# A deterministic 32-byte key used across the suite.
_RAW_KEY = bytes(range(32))
_HEX_KEY = _RAW_KEY.hex()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeCache:
    """Duck-typed stand-in for ``TokenCache`` (get/set/get_or_set only).

    Mirrors the real ``is not None`` semantics of ``get_or_set`` so that a
    value stored as ``None`` is treated as absent and triggers regeneration.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})
        self.set_calls: list[tuple[str, Any]] = []

    async def get(self, name: str) -> Any:
        return self._data.get(name)

    async def set(self, name: str, value: Any | None) -> None:
        self.set_calls.append((name, value))
        self._data[name] = value

    async def get_or_set(
        self, name: str, generator: Callable[[], Awaitable[Any] | Any]
    ) -> Any:
        existing = self._data.get(name)
        if existing is not None:
            return existing
        candidate = generator()
        if hasattr(candidate, "__await__"):
            candidate = await candidate
        await self.set(name, candidate)
        return candidate


class FailingSetCache(FakeCache):
    """``FakeCache`` whose first ``set`` raises, to exercise the guard path."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        super().__init__(initial)
        self._failed = False

    async def set(self, name: str, value: Any | None) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("simulated cache failure")
        await super().set(name, value)


@pytest.fixture(autouse=True)
def _reset_browser_guard() -> Any:
    """Isolate the module-level one-shot browser-flow guard between tests."""
    skr._browser_flow_attempted = False
    yield
    skr._browser_flow_attempted = False


@pytest.fixture
def tty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we run in an interactive TTY (CLI mode)."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))


@pytest.fixture
def no_tty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we run head-less (Home Assistant / non-interactive mode)."""
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))


# ---------------------------------------------------------------------------
# _decode_hex_32
# ---------------------------------------------------------------------------


def test_decode_hex_32_plain() -> None:
    assert skr._decode_hex_32(_HEX_KEY) == _RAW_KEY


def test_decode_hex_32_accepts_prefix_whitespace_and_uppercase() -> None:
    spaced = "0X" + " ".join(
        _HEX_KEY[i : i + 2].upper() for i in range(0, len(_HEX_KEY), 2)
    )
    assert skr._decode_hex_32(spaced) == _RAW_KEY


def test_decode_hex_32_pads_odd_length() -> None:
    # 63 hex chars (odd) that pad to a valid 32-byte value: drop one leading "0".
    odd = _HEX_KEY[1:]
    assert len(odd) % 2 == 1
    assert skr._decode_hex_32(odd) == _RAW_KEY


def test_decode_hex_32_rejects_non_hex() -> None:
    with pytest.raises(ValueError, match="non-hex"):
        skr._decode_hex_32("zz" * 32)


def test_decode_hex_32_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="invalid length"):
        skr._decode_hex_32("ab" * 16)  # 16 bytes, not 32


def test_decode_hex_32_handles_none_like_empty() -> None:
    with pytest.raises(ValueError, match="invalid length"):
        skr._decode_hex_32("")


# ---------------------------------------------------------------------------
# _decode_base64_like_32
# ---------------------------------------------------------------------------


def test_decode_base64_like_32_standard() -> None:
    encoded = base64.b64encode(_RAW_KEY).decode()
    assert skr._decode_base64_like_32(encoded) == _RAW_KEY


def test_decode_base64_like_32_urlsafe_and_pem_and_whitespace() -> None:
    body = base64.urlsafe_b64encode(_RAW_KEY).decode()
    pem = f"-----BEGIN KEY-----\n{body[:20]}\n{body[20:]}\n-----END KEY-----"
    assert skr._decode_base64_like_32(pem) == _RAW_KEY


def test_decode_base64_like_32_rejects_wrong_length() -> None:
    encoded = base64.b64encode(b"short").decode()
    with pytest.raises(ValueError, match="invalid length"):
        skr._decode_base64_like_32(encoded)


# ---------------------------------------------------------------------------
# _user_scoped_key
# ---------------------------------------------------------------------------


def test_user_scoped_key_format() -> None:
    assert skr._user_scoped_key("alice") == "shared_key_alice"


# ---------------------------------------------------------------------------
# _get_or_generate_shared_key_hex
# ---------------------------------------------------------------------------


async def test_generate_requires_cache() -> None:
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        await skr._get_or_generate_shared_key_hex(cache=None, username=None)  # type: ignore[arg-type]


async def test_generate_returns_cached_entry_scoped_value() -> None:
    cache = FakeCache({skr._CACHE_KEY_BASE: _HEX_KEY})
    result = await skr._get_or_generate_shared_key_hex(cache=cache, username="alice")
    assert result == _HEX_KEY
    assert cache.set_calls == []  # pure cache hit, no writes


async def test_generate_migrates_legacy_user_scoped_key() -> None:
    cache = FakeCache({skr._user_scoped_key("alice"): _HEX_KEY})
    result = await skr._get_or_generate_shared_key_hex(cache=cache, username="alice")
    assert result == _HEX_KEY
    # Migration persists the value under the canonical entry-scoped key.
    assert (skr._CACHE_KEY_BASE, _HEX_KEY) in cache.set_calls


async def test_generate_invokes_generator_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve() -> str:
        return _HEX_KEY

    monkeypatch.setattr(skr, "_retrieve_shared_key_hex", fake_retrieve)
    cache = FakeCache()
    result = await skr._get_or_generate_shared_key_hex(cache=cache, username=None)
    assert result == _HEX_KEY


async def test_generate_rejects_non_string_generator_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve() -> Any:
        return 12345  # not a string

    monkeypatch.setattr(skr, "_retrieve_shared_key_hex", fake_retrieve)
    cache = FakeCache()
    with pytest.raises(RuntimeError, match="non-string value"):
        await skr._get_or_generate_shared_key_hex(cache=cache, username=None)


async def test_force_refresh_clears_then_regenerates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_retrieve() -> str:
        return _HEX_KEY

    monkeypatch.setattr(skr, "_retrieve_shared_key_hex", fake_retrieve)
    cache = FakeCache({skr._CACHE_KEY_BASE: "deadbeef" * 8})
    result = await skr._get_or_generate_shared_key_hex(
        cache=cache, username="alice", force_refresh=True
    )
    assert result == _HEX_KEY
    # The stale value was explicitly cleared before regeneration.
    assert (skr._CACHE_KEY_BASE, None) in cache.set_calls
    assert (skr._user_scoped_key("alice"), None) in cache.set_calls


async def test_force_refresh_tolerates_clear_failure() -> None:
    cache = FailingSetCache({skr._CACHE_KEY_BASE: _HEX_KEY})
    # The first set() raises; the helper must swallow it and fall back to the
    # still-cached value rather than propagating the error.
    result = await skr._get_or_generate_shared_key_hex(
        cache=cache, username=None, force_refresh=True
    )
    assert result == _HEX_KEY


# ---------------------------------------------------------------------------
# _retrieve_shared_key_hex (TTY gating + one-shot guard)
# ---------------------------------------------------------------------------


async def test_retrieve_raises_in_non_interactive_mode(no_tty_stdin: None) -> None:
    # The absent-key case raises the TYPED SharedKeyUnavailableError (a
    # RuntimeError subclass) so downstream owner-key classification escalates to
    # reauth on the exception type, not the message wording.
    with pytest.raises(
        skr.SharedKeyUnavailableError, match="non-interactive environment"
    ):
        await skr._retrieve_shared_key_hex()
    assert issubclass(skr.SharedKeyUnavailableError, RuntimeError)


async def test_retrieve_returns_interactive_result(
    tty_stdin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_flow() -> str:
        return _HEX_KEY

    monkeypatch.setattr(skr, "_interactive_flow_hex", fake_flow)
    assert await skr._retrieve_shared_key_hex() == _HEX_KEY
    # The guard flips so a second call in the same process is refused.
    assert skr._browser_flow_attempted is True


async def test_retrieve_guard_blocks_second_browser_attempt(
    tty_stdin: None,
) -> None:
    """Regression: the browser flow must not open a second Chrome window."""
    skr._browser_flow_attempted = True
    with pytest.raises(RuntimeError, match="already attempted"):
        await skr._retrieve_shared_key_hex()


async def test_retrieve_wraps_interactive_failure(
    tty_stdin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom() -> str:
        raise ValueError("no chrome")

    monkeypatch.setattr(skr, "_interactive_flow_hex", boom)
    with pytest.raises(RuntimeError, match="Shared key retrieval failed"):
        await skr._retrieve_shared_key_hex()


# ---------------------------------------------------------------------------
# _interactive_flow_hex
# ---------------------------------------------------------------------------


def _install_fake_flow(monkeypatch: pytest.MonkeyPatch, return_value: Any) -> None:
    """Inject a fake ``shared_key_flow`` module and a pass-through executor."""
    module = ModuleType("custom_components.googlefindmy.KeyBackup.shared_key_flow")
    module.request_shared_key_flow = lambda: return_value  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "custom_components.googlefindmy.KeyBackup.shared_key_flow",
        module,
    )

    async def passthrough(func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(skr, "_run_in_executor", passthrough)


async def test_interactive_flow_accepts_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_flow(monkeypatch, _RAW_KEY)
    assert await skr._interactive_flow_hex() == _HEX_KEY


async def test_interactive_flow_rejects_wrong_length_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_flow(monkeypatch, b"too-short")
    with pytest.raises(RuntimeError, match="invalid length"):
        await skr._interactive_flow_hex()


async def test_interactive_flow_rejects_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_flow(monkeypatch, "   ")
    with pytest.raises(RuntimeError, match="empty/invalid result"):
        await skr._interactive_flow_hex()


async def test_interactive_flow_accepts_hex_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_flow(monkeypatch, _HEX_KEY)
    assert await skr._interactive_flow_hex() == _HEX_KEY


async def test_interactive_flow_accepts_base64_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_flow(monkeypatch, base64.urlsafe_b64encode(_RAW_KEY).decode())
    assert await skr._interactive_flow_hex() == _HEX_KEY


# ---------------------------------------------------------------------------
# async_get_shared_key (public API)
# ---------------------------------------------------------------------------


async def test_async_get_shared_key_requires_cache() -> None:
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        await skr.async_get_shared_key(cache=None)  # type: ignore[arg-type]


async def test_async_get_shared_key_hex_fast_path() -> None:
    cache = FakeCache({skr._CACHE_KEY_BASE: _HEX_KEY})
    assert await skr.async_get_shared_key(cache=cache) == _RAW_KEY


async def test_async_get_shared_key_self_heals_base64_to_hex() -> None:
    """Regression: a stored base64 value is normalised to hex on first read."""
    b64_value = base64.urlsafe_b64encode(_RAW_KEY).decode()
    cache = FakeCache({skr._CACHE_KEY_BASE: b64_value})
    result = await skr.async_get_shared_key(cache=cache)
    assert result == _RAW_KEY
    # The cache now holds the normalised hex form, not the original base64.
    assert await cache.get(skr._CACHE_KEY_BASE) == _HEX_KEY


async def test_async_get_shared_key_isolates_accounts() -> None:
    """Regression: per-entry caches must not leak across accounts."""
    key_a = bytes([1]) * 32
    key_b = bytes([2]) * 32
    cache_a = FakeCache({skr._CACHE_KEY_BASE: key_a.hex()})
    cache_b = FakeCache({skr._CACHE_KEY_BASE: key_b.hex()})

    assert await skr.async_get_shared_key(cache=cache_a) == key_a
    assert await skr.async_get_shared_key(cache=cache_b) == key_b
    # Account B's read never wrote into account A's cache and vice versa.
    assert cache_a.set_calls == []
    assert cache_b.set_calls == []


async def test_retrieve_lets_the_install_hint_through(
    tty_stdin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing pip install is not a broken Chrome installation.

    The browser packages are optional (they are not in `manifest.json`), so
    "they are not installed" is an ordinary way for this to fail. The generic
    wrapper tells the user to check their Chrome installation and to re-run,
    neither of which helps; the install command must survive.
    """

    from custom_components.googlefindmy.browser_deps import (
        INSTALL_COMMAND,
        MISSING_BROWSER_PACKAGES_HINT,
    )

    async def missing_packages() -> str:
        raise RuntimeError(MISSING_BROWSER_PACKAGES_HINT)

    monkeypatch.setattr(skr, "_interactive_flow_hex", missing_packages)

    with pytest.raises(RuntimeError) as excinfo:
        await skr._retrieve_shared_key_hex()

    assert INSTALL_COMMAND in str(excinfo.value)
