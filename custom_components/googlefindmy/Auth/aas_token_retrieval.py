#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import gpsoauth

from custom_components.googlefindmy.Auth.token_cache import load_oauth_token
# Removed FcmReceiver import to avoid ChromeDriver dependency
from custom_components.googlefindmy.Auth.token_cache import get_cached_value_or_set, set_cached_value
from custom_components.googlefindmy.Auth.username_provider import get_username, username_string


def _generate_aas_token():
    username = get_username()
    
    # First check if we already have existing ADM tokens we can derive from
    from custom_components.googlefindmy.Auth.token_cache import get_all_cached_values
    all_cached = get_all_cached_values()
    
    # Look for any existing ADM token that we can use as OAuth token
    oauth_token = None
    for key, value in all_cached.items():
        if key.startswith('adm_token_') and '@' in key:
            oauth_token = value
            extracted_username = key.replace('adm_token_', '')
            print(f"Using existing ADM token for {extracted_username} as OAuth token")
            username = extracted_username  # Update username to match the token
            break
    
    if not oauth_token:
        # Fall back to loading OAuth token
        oauth_token = load_oauth_token()
    
    if not oauth_token:
        raise ValueError("No OAuth token found - please configure integration with valid token")
    if not username:
        raise ValueError("No username found - please check configuration")

    # Use hardcoded android_id instead of FcmReceiver to avoid ChromeDriver
    # Android ID should be a large integer (16 hex digits)
    android_id = 0x38918a453d071993

    try:
        aas_token_response = gpsoauth.exchange_token(username, oauth_token, android_id)
        
        if not aas_token_response:
            raise ValueError("No response from gpsoauth.exchange_token")
        
        if 'Token' not in aas_token_response:
            raise KeyError(f"'Token' not found in response: {aas_token_response}")
            
        aas_token = aas_token_response['Token']

        if 'Email' in aas_token_response:
            email = aas_token_response['Email']
            set_cached_value(username_string, email)

        return aas_token
    except Exception as e:
        raise RuntimeError(f"Failed to exchange OAuth token for AAS token: {e}")


def get_aas_token():
    return get_cached_value_or_set('aas_token', _generate_aas_token)


if __name__ == '__main__':
    print(get_aas_token())