# custom_components/googlefindmy/vendor/openlocationcode/__init__.py
"""Vendored Open Location Code (Plus Code) encoder.

Apache-2.0, vendored from https://github.com/google/open-location-code; see the
module header in ``openlocationcode.py`` and the bundled ``LICENSE`` file.
Only the encode path is vendored; consumers import :func:`encode` from here.
"""

from .openlocationcode import encode

__all__ = ["encode"]
