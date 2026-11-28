# tests/test_discovery_title_translation.py
"""The discovery title is rendered from the shipped texts.

``SecretsJSONWatcher._async_render_title`` reads ``config.progress`` keys, which
Home Assistant serves through the translation category ``"config"``. The test
runs the real translation layer in this process: under
``use_real_homeassistant_modules`` it imports ``discovery`` freshly, so the
module binds the real loader and translation helper instead of the stubbed ones.
``tests/test_discovery_runtime.py`` checks the title through a lookup double
that does not look at the category, so it cannot see a wrong one.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_INTEGRATION_ROOT = _ROOT / "custom_components/googlefindmy"
_PACKAGE = "custom_components.googlefindmy"
_EMAIL = "alpha@example.com"


def _fresh_import(
    monkeypatch: pytest.MonkeyPatch, name: str, prefix: str
) -> ModuleType:
    """Import ``name`` freshly and hand every new ``prefix*`` key to monkeypatch.

    Same helper as in ``tests/test_services_permissions.py``: a key the import
    creates is popped and set again through ``monkeypatch``, so ``monkeypatch``
    removes it on teardown; a key that existed before gets its old value back.
    """

    def _keys() -> set[str]:
        return {k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")}

    before = _keys()
    monkeypatch.delitem(sys.modules, name, raising=False)
    parent_name, _, child = name.rpartition(".")
    parent = importlib.import_module(parent_name)
    monkeypatch.setattr(parent, child, getattr(parent, child, None), raising=False)
    module = importlib.import_module(name)
    for key in sorted(_keys() - before):
        created = sys.modules.pop(key)
        monkeypatch.setitem(sys.modules, key, created)
    return module


@pytest.fixture
def real_discovery(
    use_real_homeassistant_modules: None, monkeypatch: pytest.MonkeyPatch
) -> ModuleType:
    """``discovery`` imported freshly against the real Home Assistant modules."""
    return _fresh_import(monkeypatch, f"{_PACKAGE}.discovery", _PACKAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "de"])
async def test_discovery_title_through_the_real_translation_layer(
    real_discovery: ModuleType, language: str
) -> None:
    """Both titles come from the shipped file, with the address filled in."""
    from homeassistant import loader  # noqa: PLC0415 - the real module, after the swap
    from homeassistant.core import HomeAssistant  # noqa: PLC0415

    hass = HomeAssistant(str(_ROOT))
    hass.config.skip_pip = True
    hass.config.language = language
    loader.async_setup(hass)
    try:
        watcher = SimpleNamespace(_hass=hass)
        render = real_discovery.SecretsJSONWatcher._async_render_title
        titles = [
            await render(watcher, _EMAIL, is_update=False),
            await render(watcher, _EMAIL, is_update=True),
        ]
    finally:
        await hass.async_stop(force=True)

    texts = json.loads(
        (_INTEGRATION_ROOT / "translations" / f"{language}.json").read_text(
            encoding="utf-8"
        )
    )["config"]["progress"]
    assert titles == [
        texts["discovery_secrets_new"].format(email=_EMAIL),
        texts["discovery_secrets_update"].format(email=_EMAIL),
    ]
