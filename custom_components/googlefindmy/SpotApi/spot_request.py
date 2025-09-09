#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import httpx
import h2 # required for httpx to support HTTP/2
from bs4 import BeautifulSoup

from custom_components.googlefindmy.Auth.spot_token_retrieval import get_spot_token
from custom_components.googlefindmy.Auth.username_provider import get_username
from custom_components.googlefindmy.SpotApi.grpc_parser import GrpcParser


def spot_request(api_scope: str, payload: bytes) -> bytes:
    url = "https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/" + api_scope
    
    # Try to get spot token, but fallback to ADM token if needed
    username = get_username()
    try:
        spot_oauth_token = get_spot_token(username)
    except Exception as e:
        print(f"Failed to get spot token: {e}, trying ADM token fallback")
        # Fallback to using ADM token directly
        from custom_components.googlefindmy.Auth.token_cache import get_cached_value, get_all_cached_values
        
        spot_oauth_token = get_cached_value(f'adm_token_{username}')
        if not spot_oauth_token:
            # Look for any ADM token
            all_cached = get_all_cached_values()
            for key, value in all_cached.items():
                if key.startswith('adm_token_') and '@' in key:
                    spot_oauth_token = value
                    print(f"Using ADM token as fallback for Spot API")
                    break
        
        if not spot_oauth_token:
            raise RuntimeError(f"No authentication token available for Spot API: {e}")

    headers = {
        "User-Agent": "com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT",
        "Content-Type": "application/grpc",
        "Te": "trailers",
        "Authorization": "Bearer " + spot_oauth_token,
        "Grpc-Accept-Encoding": "gzip"
    }

    payload = GrpcParser.construct_grpc(payload)

    # httpx is necessary because requests does not support the Te header
    with httpx.Client(http2=True, timeout=30.0) as client:
        response = client.post(url, headers=headers, content=payload)

        if response.status_code == 200:
            result = GrpcParser.extract_grpc_payload(response.content)
            return result
        else:
            soup = BeautifulSoup(response.text, 'html.parser')
            print("[NovaRequest] Error: ", soup.get_text())

    return b''