#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from custom_components.googlefindmy.Auth.token_retrieval import request_token
from custom_components.googlefindmy.Auth.username_provider import get_username
from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set

def _generate_spot_token(username):
    return request_token(username, "spot", True)

def get_spot_token(username):
    return get_cached_value_or_set(f'spot_token_{username}', lambda: _generate_spot_token(username))

if __name__ == '__main__':
    print(get_spot_token(get_username()))