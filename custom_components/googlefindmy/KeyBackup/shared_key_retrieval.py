#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from binascii import unhexlify

from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set

def _retrieve_shared_key():
    # Instead of using Chrome driver, read the shared_key directly from secrets.json
    from custom_components.googlefindmy.Auth.token_cache import get_cached_value

    shared_key = get_cached_value('shared_key')
    if shared_key is None:
        raise Exception("shared_key not found in secrets.json. Please ensure your secrets.json file contains the shared_key field.")

    return shared_key

def get_shared_key() -> bytes:
    return unhexlify(get_cached_value_or_set('shared_key', _retrieve_shared_key))

if __name__ == '__main__':
    print(get_shared_key())
