# custom_components/__init__.py
"""Home Assistant custom integrations namespace for typing support."""

from __future__ import annotations

from pkgutil import extend_path

__all__: list[str] = []
__path__ = extend_path(__path__, __name__)
