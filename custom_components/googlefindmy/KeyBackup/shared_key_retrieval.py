#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from binascii import unhexlify
import base64
import re
import logging

from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set
from custom_components.googlefindmy.KeyBackup.shared_key_flow import request_shared_key_flow

_LOGGER = logging.getLogger(__name__)


def _retrieve_shared_key():
    """Attempt to retrieve the shared key, interactively or via FCM fallback."""
    print(
        """[SharedKeyRetrieval] You need to log in again to access end-to-end encrypted keys to decrypt location reports.
> This script will now open Google Chrome on your device. 
> Make that you allow Python (or PyCharm) to control Chrome (macOS only).
    """
    )

    # Check if we're running in a non-interactive environment (like Home Assistant)
    import sys

    try:
        if not sys.stdin.isatty():
            raise EOFError("Non-interactive environment detected")
        # Press enter to continue
        input("[SharedKeyRetrieval] Press 'Enter' to continue...")
    except EOFError:
        # Running in non-interactive mode, look for alternative keys
        from custom_components.googlefindmy.Auth.token_cache import get_all_cached_values

        all_cached = get_all_cached_values()
        _LOGGER.debug(f"[SharedKeyRetrieval] Available cached keys: {list(all_cached.keys())}")

        # Try to find any key that might work as a shared key
        # Look for keys in FCM credentials that might serve as shared keys
        fcm_creds = all_cached.get("fcm_credentials", {})
        if isinstance(fcm_creds, str):
            import json

            try:
                fcm_creds = json.loads(fcm_creds)
            except (json.JSONDecodeError, TypeError):
                fcm_creds = {}

        if "keys" in fcm_creds and "private" in fcm_creds["keys"]:
            print("[SharedKeyRetrieval] Using FCM private key as shared key fallback")
            private_key_b64 = str(fcm_creds["keys"]["private"]).strip()

            # Remove accidental PEM headers/whitespace and normalize padding
            private_key_b64 = re.sub(r"-{5}BEGIN[^-]+-{5}|-{5}END[^-]+-{5}", "", private_key_b64)
            private_key_b64 = re.sub(r"\s+", "", private_key_b64)
            pad = (-len(private_key_b64)) % 4
            private_key_b64_padded = private_key_b64 + ("=" * pad)

            # IMPORTANT: FCM keys are often base64url ('-' and '_'); use urlsafe decode first
            try:
                private_key_der = base64.urlsafe_b64decode(private_key_b64_padded)
            except (ValueError, TypeError):
                # fallback to standard base64 if needed
                try:
                    private_key_der = base64.b64decode(private_key_b64_padded)
                except (ValueError, TypeError) as decode_error:
                    print(
                        f"[SharedKeyRetrieval] Failed to decode FCM private key (base64/url): {decode_error}"
                    )
                    raise RuntimeError(
                        f"Failed to decode FCM private key for shared key fallback: {decode_error}"
                    )

            if len(private_key_der) < 32:
                raise RuntimeError(
                    f"Decoded FCM private key is too short ({len(private_key_der)} bytes); "
                    "cannot derive 32-byte shared key."
                )
            # Deterministic 32-byte material: keep the original "last 32 bytes" approach
            return private_key_der[-32:].hex()

        raise RuntimeError("No suitable shared key found in cache")

    shared_key = request_shared_key_flow()
    return shared_key


def get_shared_key() -> bytes:
    """Get the shared key, from cache or by generating it."""
    # First try to get the cached shared key directly
    from custom_components.googlefindmy.Auth.token_cache import get_cached_value, get_all_cached_values

    shared_key_hex = get_cached_value("shared_key")
    if shared_key_hex:
        print(f"[SharedKeyRetrieval] Found cached shared key: {len(shared_key_hex)} chars")
        return unhexlify(shared_key_hex)

    print("[SharedKeyRetrieval] No shared_key found in cache, debugging...")

    # Debug: check what keys are actually available
    all_cached = get_all_cached_values()
    available_keys = list(all_cached.keys())
    print(f"[SharedKeyRetrieval] Available keys in cache: {available_keys}")

    if "shared_key" in all_cached:
        shared_key_hex = all_cached["shared_key"]
        print(
            f"[SharedKeyRetrieval] Found shared_key via get_all_cached_values: {len(shared_key_hex)} chars"
        )
        return unhexlify(shared_key_hex)

    print("[SharedKeyRetrieval] shared_key definitely not found, trying to generate...")
    return unhexlify(get_cached_value_or_set("shared_key", _retrieve_shared_key))


if __name__ == "__main__":
    print(get_shared_key())
