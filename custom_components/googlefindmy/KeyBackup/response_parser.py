# custom_components/googlefindmy/KeyBackup/response_parser.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import json
from typing import cast

from custom_components.googlefindmy.example_data_provider import get_example_data


def _transform_to_byte_array(json_object: dict[str, int]) -> bytearray:
    byte_array = bytearray(json_object[str(i)] for i in range(len(json_object)))
    return byte_array


def get_fmdn_shared_key(vault_keys: str) -> bytes:
    json_object_raw = json.loads(vault_keys)
    if not isinstance(json_object_raw, dict):
        raise ValueError("Vault keys payload must be a JSON object.")

    json_object = cast(dict[str, object], json_object_raw)

    finder_hw_value = json_object.get("finder_hw")
    if not isinstance(finder_hw_value, list):
        raise ValueError("Vault keys JSON does not contain a finder_hw list.")

    finder_hw: list[object] = finder_hw_value

    latest_epoch = -1
    latest_key: bytearray | None = None

    for item in finder_hw:
        if not isinstance(item, dict):
            continue

        epoch = item.get("epoch")
        key_data = item.get("key")

        if not isinstance(epoch, int):
            continue

        if not isinstance(key_data, dict):
            continue

        if not all(isinstance(key, str) and isinstance(value, int) for key, value in key_data.items()):
            continue

        key_dict = cast(dict[str, int], key_data)
        processed_key = _transform_to_byte_array(key_dict)

        if epoch >= latest_epoch:
            latest_epoch = epoch
            latest_key = processed_key

    if latest_key is None:
        raise ValueError("No suitable key found in the vault keys.")

    return bytes(latest_key)


if __name__ == "__main__":
    vault_keys = get_example_data("sample_vault_keys")
    print(get_fmdn_shared_key(vault_keys).hex())
