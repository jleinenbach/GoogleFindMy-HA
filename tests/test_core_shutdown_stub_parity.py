# tests/test_core_shutdown_stub_parity.py
"""The ``conftest`` stub of ``DataUpdateCoordinator`` shuts down like the core.

``GoogleFindMyCoordinator.async_shutdown`` chains to ``super().async_shutdown()``,
and the suite runs every shutdown path against the stub in ``tests/conftest.py``,
never against the core. A stub that did something else would let the chain look
right while proving nothing about it. This module drives the core class and the
stub through the same shutdown on a ``__new__`` instance (no ``__init__``, so no
``hass``), seeded with the four attributes the core's ``__init__`` sets, and
compares what each did. It also pins the core's ``__init__`` to those four names,
because ``tests.helpers.core_shutdown_state`` and the stub both spell them out.

The core class is reached through ``use_real_homeassistant_modules``; the stub
through the MRO of the production coordinator, which was bound to it at import.
``coordinator.main`` is not reloaded here (the coordinator identity guard in
``conftest`` explains why no test does that).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

SEEDED_ATTRIBUTES = (
    "_shutdown_requested",
    "_unsub_refresh",
    "_unsub_shutdown",
    "_debounced_refresh",
)


def _stub_class() -> type[Any]:
    """Return the ``DataUpdateCoordinator`` the production class was bound to."""
    for klass in GoogleFindMyCoordinator.__mro__:
        if klass.__name__ == "DataUpdateCoordinator":
            return klass
    raise AssertionError("GoogleFindMyCoordinator has no DataUpdateCoordinator base")


def _core_class() -> type[Any]:
    from homeassistant.helpers import update_coordinator

    return update_coordinator.DataUpdateCoordinator


async def _drive(cls: type[Any]) -> dict[str, Any]:
    """Shut a seeded ``__new__`` instance of ``cls`` down twice; report what it did."""
    calls: list[str] = []
    c: Any = cls.__new__(cls)
    c._shutdown_requested = False
    c._unsub_refresh = lambda: calls.append("interval")
    c._unsub_shutdown = lambda: calls.append("stop-listener")
    c._debounced_refresh = SimpleNamespace(
        async_shutdown=lambda: calls.append("debouncer"),
        async_cancel=lambda: calls.append("debouncer-cancel"),
    )

    await c.async_shutdown()
    first = list(calls)
    flag_after_first = c._shutdown_requested
    removers_after_first = (c._unsub_refresh, c._unsub_shutdown)

    await c.async_shutdown()
    return {
        "first": first,
        "flag": flag_after_first,
        "removers_cleared": removers_after_first == (None, None),
        "second": calls[len(first) :],
    }


def _body_without_docstring(func: Any) -> str:
    """Return the AST dump of ``func``'s statements, docstring excluded."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
    body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
    return "\n".join(ast.dump(stmt) for stmt in body)


def test_the_stub_is_not_the_core() -> None:
    """The comparison below is only a comparison if the two classes differ."""
    assert _stub_class().__module__ == "tests.conftest"


def test_the_fixture_hands_out_the_core(use_real_homeassistant_modules: None) -> None:
    """Under the fixture, ``_core_class`` is the core, not the stub again.

    Without this pin, a fixture that failed to swap the modules would let the
    three comparisons below pass trivially, stub against stub.
    """
    core = _core_class()
    assert core is not _stub_class()
    assert core.__module__ == "homeassistant.helpers.update_coordinator"


@pytest.mark.asyncio
async def test_stub_shutdown_does_what_the_core_shutdown_does(
    use_real_homeassistant_modules: None,
) -> None:
    core = await _drive(_core_class())
    stub = await _drive(_stub_class())

    expected = {
        "first": ["interval", "stop-listener", "debouncer"],
        "flag": True,
        "removers_cleared": True,
        "second": ["debouncer"],
    }
    assert core == expected, "the core changed; update the stub and this pin"
    assert stub == expected, "the stub drifted from the core"


def test_core_shutdown_source_is_what_the_stub_mirrors(
    use_real_homeassistant_modules: None,
) -> None:
    """Statement for statement, the three mirrored methods match the core.

    Goes red on the lock bump that changes the core's shutdown, naming the
    method; the behavioural test above may stay green through a change that
    does not touch these four attributes, and this one does not.
    """
    core, stub = _core_class(), _stub_class()
    for name in ("async_shutdown", "_async_unsub_refresh", "_async_unsub_shutdown"):
        assert _body_without_docstring(getattr(stub, name)) == _body_without_docstring(
            getattr(core, name)
        ), f"{name}: stub body differs from the core"


def test_core_init_still_sets_the_seeded_attributes(
    use_real_homeassistant_modules: None,
) -> None:
    source = inspect.getsource(_core_class().__init__)
    for attribute in SEEDED_ATTRIBUTES:
        assert f"self.{attribute}" in source, (
            f"the core no longer sets {attribute}; update the seed helper and the stub"
        )
