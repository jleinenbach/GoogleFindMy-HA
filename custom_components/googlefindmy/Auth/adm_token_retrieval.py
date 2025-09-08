#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from custom_components.googlefindmy.Auth.token_retrieval import request_token
from custom_components.googlefindmy.Auth.username_provider import get_username
from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set

def _generate_adm_token(username):
    """Generate a new ADM token."""
    return request_token(username, "android_device_manager")

def get_adm_token(username):
    """Get ADM token from cache or generate a new one."""
    return get_cached_value_or_set(f'adm_token_{username}', lambda: _generate_adm_token(username))


if __name__ == '__main__':
    print(get_adm_token(get_username()))