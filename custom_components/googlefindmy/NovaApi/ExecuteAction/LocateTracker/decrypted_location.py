# custom_components/googlefindmy/NovaApi/ExecuteAction/LocateTracker/decrypted_location.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#


class WrappedLocation:
    def __init__(
        self,
        *,
        decrypted_location: bytes,
        time: float,
        accuracy: float,
        status: int,
        is_own_report: bool,
        name: str,
    ) -> None:
        if isinstance(decrypted_location, bytearray):
            decrypted_payload = bytes(decrypted_location)
        elif isinstance(decrypted_location, bytes):
            decrypted_payload = decrypted_location
        else:
            msg = "decrypted_location must be bytes"
            raise TypeError(msg)

        self.time: float = time
        self.status: int = status
        self.decrypted_location: bytes = decrypted_payload
        self.is_own_report: bool = is_own_report
        self.accuracy: float = accuracy
        self.name: str = name
