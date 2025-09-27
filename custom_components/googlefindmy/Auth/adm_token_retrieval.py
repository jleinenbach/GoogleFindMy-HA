#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
#  ADM token retrieval with caching.
#
from __future__ import annotations

import asyncio
from typing import Callable

from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set
from custom_components.googlefindmy.Auth.token_retrieval import request_token, async_request_token


def get_adm_token(username: str) -> str:
    """Legacy synchronous path (may block). Only call from threads."""
    return get_cached_value_or_set(
        f"adm_token_{username}",
        lambda: request_token(username, "android_device_manager"),
    )


async def async_get_adm_token(username: str) -> str:
    """Non-blocking token fetch with cache.
    Uses the existing cache helper inside a worker thread so the generator
    (which performs network I/O) never runs on the event loop.
    """
    def _generator() -> str:
        # Run the blocking legacy function in THIS worker thread via to_thread wrapping of caller.
        # We cannot await here because get_cached_value_or_set expects a sync callable.
        return request_token(username, "android_device_manager")

    return await asyncio.to_thread(
        get_cached_value_or_set,
        f"adm_token_{username}",
        _generator,
    )


if __name__ == "__main__":
    # Minimal CLI for local testing only; has no effect inside Home Assistant.
    import argparse
    import asyncio
    import sys
    from custom_components.googlefindmy.Auth.token_retrieval import async_request_token

    parser = argparse.ArgumentParser(
        description="Fetch an Android Device Manager OAuth token (dev-only CLI)."
    )
    parser.add_argument(
        "-u", "--username", required=True, help="Google account email (username)"
    )
    parser.add_argument(
        "-s", "--scope", default="android_device_manager",
        help="OAuth scope (default: android_device_manager)"
    )
    parser.add_argument(
        "--play-services", action="store_true", default=False,
        help="Use com.google.android.gms app id instead of com.google.android.apps.adm"
    )
    parser.add_argument(
        "--only-token", action="store_true", default=False,
        help="Print token only (no labels)"
    )
    args = parser.parse_args()

    async def _main() -> int:
        try:
            token = await async_request_token(
                username=args.username,
                scope=args.scope,
                play_services=args.play_services,
            )
            if args.only_token:
                print(token)
            else:
                print(f"scope={args.scope} token={token}")
            return 0
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
