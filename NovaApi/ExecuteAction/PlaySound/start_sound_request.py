#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
# FcmReceiver imported only when needed to avoid protobuf conflicts
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import create_sound_request
from custom_components.googlefindmy.NovaApi.nova_request import nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE


def start_sound_request(canonic_device_id, gcm_registration_id):
    from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
    request_uuid = generate_random_uuid()
    return create_sound_request(True, canonic_device_id, gcm_registration_id, request_uuid)


