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
        self.time: float = time
        self.status: int = status
        self.decrypted_location: bytes = decrypted_location
        self.is_own_report: bool = is_own_report
        self.accuracy: float = accuracy
        self.name: str = name
