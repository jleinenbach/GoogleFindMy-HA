#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Standalone CLI entry point for GoogleFindMy functionality.

This module provides the same standalone functionality as the original
GoogleFindMyTools main.py, allowing users to interact with the Google Find My
API outside of Home Assistant.

Usage:
    python -m custom_components.googlefindmy.main [--entry ENTRY_ID]

Or alternatively:
    python custom_components/googlefindmy/main.py [--entry ENTRY_ID]

Prerequisites:
    - A valid token cache must be registered (typically from a prior HA session
      or manual authentication flow).
    - Set the GOOGLEFINDMY_ENTRY_ID environment variable or use the --entry flag
      to specify the target config entry ID.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (
    _async_cli_main,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "GoogleFindMyTools CLI - Interact with the Google Find My API. "
            "List devices, fetch locations, or register new trackers."
        )
    )
    parser.add_argument(
        "--entry",
        help=(
            "Config entry ID to target. "
            "Alternatively set the GOOGLEFINDMY_ENTRY_ID environment variable."
        ),
    )
    return parser.parse_args(argv)


def list_devices(entry_id: str | None = None) -> None:
    """Interactive CLI to list devices and fetch locations.

    This function provides the same experience as the original GoogleFindMyTools:
    - Lists all available trackers
    - Allows fetching location data for a specific tracker
    - Supports registering new ESP32/Zephyr-based trackers

    Args:
        entry_id: Optional config entry ID. If not provided, uses the
            GOOGLEFINDMY_ENTRY_ID environment variable.
    """
    resolved_entry = entry_id or os.environ.get("GOOGLEFINDMY_ENTRY_ID")

    try:
        asyncio.run(_async_cli_main(resolved_entry))
    except KeyboardInterrupt:
        print("\nExiting.")
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Main entry point for the CLI."""
    args = _parse_args()
    list_devices(args.entry)


if __name__ == "__main__":
    main()
