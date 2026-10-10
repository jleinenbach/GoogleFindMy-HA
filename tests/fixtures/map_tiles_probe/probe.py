# tests/fixtures/map_tiles_probe/probe.py
"""Print the ``map_tiles`` constants the integration mirrors, as one JSON line.

Exits non-zero with the import error on stderr when the installed Core has no
``map_tiles`` component (Core < 2026.9); the caller turns that into a skip.
Every other failure is a real failure.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    """Import the component and report the values the contract test compares."""
    from homeassistant.components.map_tiles import const, views

    print(
        json.dumps(
            {
                "key": str(const.DATA_ACCESS_TOKENS),
                "attribution": const.ATTRIBUTION,
                "raster_max_zoom": const.RASTER_MAX_ZOOM,
                "raster_url": views.MapTilesRasterView.url,
                "token_size": const.TOKEN_SIZE,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
