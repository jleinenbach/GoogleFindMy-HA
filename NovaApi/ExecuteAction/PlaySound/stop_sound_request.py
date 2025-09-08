#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from custom_components.googlefindmy.Auth.fcm_receiver import FcmReceiver
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import create_sound_request
from custom_components.googlefindmy.NovaApi.nova_request import nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE


def stop_sound_request(canonic_device_id, gcm_registration_id):
    return create_sound_request(False, canonic_device_id, gcm_registration_id)


