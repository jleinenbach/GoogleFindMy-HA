#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from custom_components.googlefindmy.Auth.username_provider import get_username
from custom_components.googlefindmy.Auth.token_cache import get_cached_value

def get_adm_token(username):
    """Get ADM token from cache (read-only from secrets.json)."""
    # First try exact match
    exact_key = f'adm_token_{username}'
    existing_token = get_cached_value(exact_key)
    if existing_token:
        return existing_token

    # If no exact match, look for any adm_token_ key that contains the username
    from custom_components.googlefindmy.Auth.token_cache import get_all_cached_values
    all_cached = get_all_cached_values()

    for key, value in all_cached.items():
        if key.startswith('adm_token_') and username in key and value:
            return value

    # If no existing token found, raise error instead of generating
    raise ValueError(f"No ADM token found for {username} in secrets.json. Please ensure your secrets.json contains the required 
adm_token.")

if __name__ == '__main__':
      print(get_adm_token(get_username()))
