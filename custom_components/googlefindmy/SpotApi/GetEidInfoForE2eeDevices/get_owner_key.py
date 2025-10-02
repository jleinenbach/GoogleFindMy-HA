#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from binascii import unhexlify, Error as BinasciiError

from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import decrypt_owner_key
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import get_shared_key
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import get_eid_info
import logging

_LOGGER = logging.getLogger(__name__)

def _retrieve_owner_key() -> str:
    eid_info = get_eid_info()
    shared_key = get_shared_key()

    # Guards for presence and non-empty encrypted owner key
    metadata = getattr(eid_info, 'encryptedOwnerKeyAndMetadata', None)
    if metadata is None:
        raise RuntimeError("Missing 'encryptedOwnerKeyAndMetadata' in eid_info")

    encrypted_owner_key = getattr(metadata, 'encryptedOwnerKey', b'')
    if not encrypted_owner_key:
        raise RuntimeError("Missing or empty 'encryptedOwnerKey' in eid_info.encryptedOwnerKeyAndMetadata")

    owner_key = decrypt_owner_key(shared_key, encrypted_owner_key)
    owner_key_version = getattr(metadata, 'ownerKeyVersion', None)

    if not isinstance(owner_key, (bytes, bytearray)) or len(owner_key) == 0:
        raise RuntimeError("Decrypted owner_key is empty or invalid type")

    _LOGGER.warning("Retrieved owner key with version: %s (len=%s)", owner_key_version, len(owner_key))

    return owner_key.hex()


def get_owner_key() -> bytes:
    value = get_cached_value_or_set('owner_key', _retrieve_owner_key)
    try:
        return unhexlify(value)
    except BinasciiError as exc:
        raise RuntimeError("Invalid hex value for 'owner_key' from cache") from exc


if __name__ == '__main__':
    print(get_owner_key())
