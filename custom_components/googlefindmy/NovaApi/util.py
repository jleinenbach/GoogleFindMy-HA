# custom_components/googlefindmy/NovaApi/util.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import uuid


def generate_random_uuid() -> str:
    return str(uuid.uuid4())
