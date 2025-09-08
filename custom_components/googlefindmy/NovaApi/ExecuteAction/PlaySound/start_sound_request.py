#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
# FcmReceiver imported only when needed to avoid protobuf conflicts
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import create_sound_request
from custom_components.googlefindmy.NovaApi.nova_request import nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.example_data_provider import get_example_data


def start_sound_request(canonic_device_id, gcm_registration_id):
    from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
    request_uuid = generate_random_uuid()
    return create_sound_request(True, canonic_device_id, gcm_registration_id, request_uuid)


if __name__ == '__main__':
    from custom_components.googlefindmy.Auth.fcm_receiver import FcmReceiver
    
    sample_canonic_device_id = get_example_data("sample_canonic_device_id")

    fcm_token = FcmReceiver().register_for_location_updates( lambda x: print(x) )

    hex_payload = start_sound_request(sample_canonic_device_id, fcm_token)
    nova_request(NOVA_ACTION_API_SCOPE, hex_payload)