# tests/test_exception_stub_translation_parity.py
"""The ``conftest`` exception stubs accept translation metadata like the core.

The integration raises ``UpdateFailed`` with ``translation_domain``,
``translation_key`` and ``translation_placeholders``, and the setup wrapper
re-raises ``ConfigEntryNotReady`` from it. The suite runs almost every test
against the lightweight stubs in ``tests/conftest.py``, never against the core.
A stub that rejected those keyword arguments would fail every test that
builds one of them with a ``TypeError``; a stub that accepted them but rendered ``str()`` differently
would let a wrong setup message look right. This module therefore builds the
same exceptions from the stub and from the core and compares what each stores
and renders.

Comparison rule: no ``inspect.signature`` comparison. The core signature of
``UpdateFailed`` is ``(*args, retry_after=None, **kwargs)``, the translation
arguments live only in ``HomeAssistantError.__init__``, and
``ConfigEntryNotReady`` has no ``__init__`` of its own. The tests construct
instead and compare attributes and ``str()``.

The stub classes are bound at import time of this module, which is before the
``use_real_homeassistant_modules`` fixture swaps ``sys.modules``; the core
classes are imported inside the tests, after the swap.

``str()`` of a core exception that carries a key but no message looks the key
up in the translation cache of the running instance, so that case runs under
the ``hass`` fixture. No translations are loaded there, which is the least
favourable case of the core (the raw key comes back). The stub reproduces that
case on purpose: a wrapper that builds its text from such an exception then
shows the raw key in the stub world as well, and its test fails.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.exceptions import ConfigEntryNotReady as StubConfigEntryNotReady
from homeassistant.exceptions import HomeAssistantError as StubHomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed as StubUpdateFailed

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DOMAIN = "googlefindmy"
KEY = "parity_probe_key"
PLACEHOLDERS = {"count": "3"}
TRANSLATION_ATTRIBUTES = (
    "translation_domain",
    "translation_key",
    "translation_placeholders",
)

_HAS_PLUGIN = (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
)


def _core_classes() -> tuple[type[Any], type[Any], type[Any]]:
    """Return ``HomeAssistantError``, ``UpdateFailed``, ``ConfigEntryNotReady`` of the core."""
    from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
    from homeassistant.helpers.update_coordinator import UpdateFailed

    return HomeAssistantError, UpdateFailed, ConfigEntryNotReady


def _stub_classes() -> tuple[type[Any], type[Any], type[Any]]:
    return StubHomeAssistantError, StubUpdateFailed, StubConfigEntryNotReady


def _translation_kwargs() -> dict[str, Any]:
    return {
        "translation_domain": DOMAIN,
        "translation_key": KEY,
        "translation_placeholders": dict(PLACEHOLDERS),
    }


def _stored(err: BaseException, *, with_retry: bool) -> dict[str, Any]:
    names = (
        *TRANSLATION_ATTRIBUTES,
        "generate_message",
        *(("retry_after",) if with_retry else ()),
    )
    return {name: getattr(err, name) for name in names}


def test_bound_classes_are_the_conftest_stubs() -> None:
    """Guard the premise: the module-level names are the stubs, not the core."""
    for klass in _stub_classes():
        assert not klass.__module__.startswith("homeassistant"), klass


def test_stub_hierarchy_matches_the_core(use_real_homeassistant_modules: None) -> None:
    """``UpdateFailed`` and ``ConfigEntryNotReady`` derive from ``HomeAssistantError``."""
    core_error, core_update_failed, core_not_ready = _core_classes()
    assert issubclass(core_update_failed, core_error)
    assert issubclass(core_not_ready, core_error)
    assert issubclass(StubUpdateFailed, StubHomeAssistantError)
    assert issubclass(StubConfigEntryNotReady, StubHomeAssistantError)


@pytest.mark.parametrize(
    ("index", "with_retry"),
    [(0, False), (1, True), (2, False)],
    ids=["HomeAssistantError", "UpdateFailed", "ConfigEntryNotReady"],
)
def test_stub_stores_translation_arguments_like_the_core(
    use_real_homeassistant_modules: None, index: int, with_retry: bool
) -> None:
    """All translation arguments (and ``retry_after``) are accepted and stored alike."""
    extra = {"retry_after": 30.0} if with_retry else {}
    core = _core_classes()[index]("text", **_translation_kwargs(), **extra)
    stub = _stub_classes()[index]("text", **_translation_kwargs(), **extra)

    assert _stored(stub, with_retry=with_retry) == _stored(core, with_retry=with_retry)
    assert stub.args == core.args


@pytest.mark.parametrize(
    "index",
    [0, 1, 2],
    ids=["HomeAssistantError", "UpdateFailed", "ConfigEntryNotReady"],
)
def test_stub_defaults_match_the_core(
    use_real_homeassistant_modules: None, index: int
) -> None:
    """Without translation arguments every attribute is ``None`` on both sides."""
    core = _core_classes()[index]("text")
    stub = _stub_classes()[index]("text")

    with_retry = index == 1
    assert _stored(stub, with_retry=with_retry) == _stored(core, with_retry=with_retry)
    assert str(stub) == str(core) == "text"


def test_class_defaults_match_the_core(use_real_homeassistant_modules: None) -> None:
    """A subclass that skips ``super().__init__`` reads the same defaults."""
    core_error = _core_classes()[0]

    class _CoreBare(core_error):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            Exception.__init__(self)

    class _StubBare(StubHomeAssistantError):
        def __init__(self) -> None:
            Exception.__init__(self)

    assert _stored(_StubBare(), with_retry=False) == _stored(
        _CoreBare(), with_retry=False
    )


def test_update_failed_text_wins_over_the_key(
    use_real_homeassistant_modules: None,
) -> None:
    """(i) With a text argument ``str()`` is the text, key or not."""
    _, core_update_failed, _ = _core_classes()
    core = core_update_failed("server said no", **_translation_kwargs())
    stub = StubUpdateFailed("server said no", **_translation_kwargs())

    assert str(stub) == str(core) == "server said no"


def test_config_entry_not_ready_falls_back_to_its_cause(
    use_real_homeassistant_modules: None,
) -> None:
    """(iii) ``ConfigEntryNotReady`` without text renders ``str(__cause__)``."""
    _, core_update_failed, core_not_ready = _core_classes()
    core = core_not_ready()
    core.__cause__ = core_update_failed("cold start")
    stub = StubConfigEntryNotReady()
    stub.__cause__ = StubUpdateFailed("cold start")

    assert str(stub) == str(core) == "cold start"


@pytest.mark.skipif(
    not _HAS_PLUGIN, reason="needs pytest-homeassistant-custom-component"
)
@pytest.mark.asyncio
async def test_key_without_text_renders_the_key_without_cached_translations(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(ii) Key but no text: the core returns the raw key when no translation is cached.

    ``use_real_homeassistant_modules`` re-imports ``homeassistant.core``, whose
    thread-local ``_hass`` is then empty, while the ``hass`` fixture registered
    itself in the module the plugin imported first. ``async_get_hass`` of the
    re-imported module would raise, so the instance is registered there too.
    Its translation cache is created on first use and holds nothing for this
    domain, which is the case under test.
    """
    from homeassistant import core as real_core

    monkeypatch.setattr(real_core._hass, "hass", hass)
    _, core_update_failed, _ = _core_classes()
    core = core_update_failed(**_translation_kwargs())
    stub = StubUpdateFailed(**_translation_kwargs())

    assert str(stub) == str(core) == KEY
